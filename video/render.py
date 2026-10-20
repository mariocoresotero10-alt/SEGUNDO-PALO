#!/usr/bin/env python3
"""Segundo Palo - renderizador de Shorts (PIL + ffmpeg, sin servicios de pago).

Uso:
  python render.py --data data.json --audio voz.mp3 --images imgs/ --out salida.mp4 [--scale 0.5]
  python render.py --data data.json --audio voz.mp3 --images imgs/ --stills 0.5,3,10 --outdir stills/
data.json: {"gancho","guion","jugadores":[...3],"etiqueta"}
imgs/: img_estadio.jpg, img_1.jpg, img_2.jpg, img_3.jpg (opcionales)
"""
import argparse, json, math, os, re, subprocess, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(HERE, "fonts", "Poppins-Bold.ttf")
W, H, FPS = 1080, 1920, 30
YELLOW = (250, 204, 21)
GREEN = (11, 42, 28)
GREEN2 = (15, 54, 36)
WHITE = (255, 255, 255)


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease_out(t):
    t = clamp(t)
    return 1 - (1 - t) ** 3


def ease_back(t):
    t = clamp(t)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def ease_io(t):
    t = clamp(t)
    return t * t * (3 - 2 * t)


class Ctx:
    def __init__(self, scale):
        self.k = scale
        self.W, self.H = int(round(W * scale)), int(round(H * scale))
        self._fonts = {}

    def s(self, v):
        return int(round(v * self.k))

    def font(self, px):
        px = max(6, self.s(px))
        if px not in self._fonts:
            self._fonts[px] = ImageFont.truetype(FONT, px)
        return self._fonts[px]


def audio_duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "default=nw=1:nk=1", path]).decode().strip()
    return float(out)


def word_timings(text, dur):
    words = text.split()
    wts = []
    for w in words:
        base = len(re.sub(r"\W", "", w)) + 1.5
        if w.endswith((",", ";", ":")):
            base += 3
        if w.endswith((".", "?", "!", "…")):
            base += 6
        wts.append(base)
    start, end = 0.12, max(1.0, dur - 0.25)
    total = sum(wts)
    t = start
    out = []
    for w, wt in zip(words, wts):
        d = (end - start) * wt / total
        out.append({"w": w, "s": t, "e": t + d})
        t += d
    return out


def detect_pauses(path):
    try:
        r = subprocess.run(["ffmpeg", "-i", path, "-af", "silencedetect=noise=-30dB:d=0.12", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
    except Exception:
        return []
    st = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r)]
    en = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r)]
    return list(zip(st, en))


def align_timings(words, pauses, dur):
    """Ajusta los tiempos estimados anclando los signos de puntuacion a las pausas reales de la voz."""
    punct = [i for i, w in enumerate(words) if w["w"].endswith((",", ";", ":", ".", "?", "!", "…")) and i < len(words) - 1]
    used = -1
    anchors = []
    for i in punct:
        est = words[i]["e"]
        best = None
        for k in range(used + 1, len(pauses)):
            ps, pe = pauses[k]
            if ps < est - 1.6:
                continue
            if ps > est + 1.6:
                break
            d = abs(ps - est)
            if best is None or d < best[0]:
                best = (d, k)
        if best:
            used = best[1]
            anchors.append((i, pauses[used][0], pauses[used][1]))
    if not anchors:
        return words
    out = [dict(w) for w in words]
    bounds = []
    cur_i, cur_t = 0, 0.12
    for idx, ps, pe in anchors:
        bounds.append((cur_i, idx, cur_t, ps))
        cur_i, cur_t = idx + 1, pe
    bounds.append((cur_i, len(words) - 1, cur_t, max(cur_t + 0.5, dur - 0.25)))
    for a, b, t0, t1 in bounds:
        if b < a or t1 <= t0:
            continue
        ws = [max(1, len(re.sub(r"\W", "", words[j]["w"]))) + 1.5 for j in range(a, b + 1)]
        tot = sum(ws)
        t = t0
        for j, wt in zip(range(a, b + 1), ws):
            d = (t1 - t0) * wt / tot
            out[j]["s"], out[j]["e"] = t, t + d
            t += d
    return out


def whisper_words(path):
    """Palabras reconocidas con su tiempo real (faster-whisper). Devuelve [] si no esta disponible."""
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        print("whisper no disponible:", e)
        return []
    try:
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segs, _ = model.transcribe(path, language="es", word_timestamps=True, vad_filter=False,
                                   condition_on_previous_text=False)
        out = []
        for sg in segs:
            for w in (sg.words or []):
                out.append((w.word.strip(), float(w.start), float(w.end)))
        print("whisper: palabras reconocidas:", len(out))
        return out
    except Exception as e:
        print("whisper fallo:", e)
        return []


def whisper_align(words, heard, dur):
    """Pone a cada palabra del guion el tiempo real de la voz; las no reconocidas se interpolan."""
    import difflib
    if not heard:
        return None
    a = [norm(w["w"]) for w in words]
    b = [norm(h[0]) for h in heard]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    t_s = [None] * len(words)
    t_e = [None] * len(words)
    hit = 0
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            i, j = blk.a + k, blk.b + k
            if a[i]:
                t_s[i], t_e[i] = heard[j][1], heard[j][2]
                hit += 1
    print("alineacion whisper: %d/%d palabras" % (hit, len(words)))
    if hit < len(words) * 0.5:
        return None
    # interpolar huecos
    n = len(words)
    known = [i for i in range(n) if t_s[i] is not None]
    out = [dict(w) for w in words]
    for i in known:
        out[i]["s"], out[i]["e"] = t_s[i], t_e[i]
    prev_end, prev_i = 0.12, -1
    for idx, i in enumerate(known + [n]):
        nxt_start = t_s[i] if i < n else max(prev_end + 0.3, dur - 0.25)
        gap = list(range(prev_i + 1, i))
        if gap:
            ws = [max(1, len(re.sub(r"\W", "", words[g]["w"]))) + 1.0 for g in gap]
            tot = sum(ws)
            span = max(0.05 * len(gap), nxt_start - prev_end)
            t = prev_end
            for g, wt in zip(gap, ws):
                d = span * wt / tot
                out[g]["s"], out[g]["e"] = t, t + d
                t += d
        if i < n:
            prev_end, prev_i = t_e[i], i
    # asegurar orden creciente
    last = 0.0
    for w in out:
        if w["s"] < last:
            w["s"] = last
        if w["e"] < w["s"] + 0.04:
            w["e"] = w["s"] + 0.04
        last = w["s"]
    return out


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("á", "a").replace("é", "e").replace("í", "i")
                  .replace("ó", "o").replace("ú", "u").replace("ñ", "n").replace("ü", "u"))


def build_scenes(data, words, dur, images_dir, clips_dir=None):
    """Plano nuevo cada ~2.4 s. Cada jugador usa sus fotos en rotacion; entre medias, clips libres."""
    import glob, random
    jug = [re.sub(r"\s*\(.*?\)", "", j).strip() for j in data.get("jugadores", [])][:3]
    # menciones de cada jugador a lo largo del guion (todas, no solo la primera)
    keys_of = [[norm(p) for p in name.split() if len(p) > 2] for name in jug]
    segs = [(0.0, 0)]
    for wd in words:
        nw = norm(wd["w"])
        for i, ks in enumerate(keys_of):
            if any(k and k in nw for k in ks):
                if segs[-1][1] != i:
                    segs.append((max(0.0, wd["s"] - 0.15), i))
                break
    marks = []
    for i in range(len(jug)):
        ts = [st for st, sj in segs if sj == i]
        marks.append(ts[0] if ts else None)

    def subject_at(t):
        cur = segs[0][1]
        for st, sj in segs:
            if st <= t:
                cur = sj
        return cur

    def imgs_of(key):
        return sorted(glob.glob(os.path.join(images_dir, f"{key}_*.jpg")))

    pools = {i: imgs_of(f"p{i+1}") or [p for p in [os.path.join(images_dir, f"img_{i+1}.jpg")] if os.path.exists(p)]
             for i in range(len(jug))}
    # cuantas fotos usar de cada jugador segun cuanto se habla de el (las mejores van primero)
    spk = {i: 0.0 for i in range(len(jug))}
    for k_, (st_, sj_) in enumerate(segs):
        en_ = segs[k_ + 1][0] if k_ + 1 < len(segs) else dur
        spk[sj_] = spk.get(sj_, 0.0) + max(0.0, en_ - st_)
    for i in list(pools):
        pl = pools[i]
        if len(pl) > 1:
            need = int(round(spk.get(i, 0.0) / 4.5))
            need = max(need, int(math.ceil(spk.get(i, 0.0) / 6.0)), 1)
            pools[i] = pl[:min(len(pl), min(need, 3))]
            print("fotos de", jug[i], ":", len(pools[i]), "de", len(pl), "(habla %.0fs)" % spk.get(i, 0.0))
    est = []  # nunca fotos de estadio genericas
    rng = random.Random(int(dur * 1000))
    clips = []
    if clips_dir and os.path.isdir(clips_dir):
        clips = sorted(glob.glob(os.path.join(clips_dir, "*.mp4")) + glob.glob(os.path.join(clips_dir, "*.mov"))
                       + glob.glob(os.path.join(clips_dir, "*.webm")))
        rng.shuffle(clips)
    used = {i: 0 for i in pools}
    scenes = []
    t = 0.0
    k = 0
    clip_i = 0
    # fin del tramo de cada sujeto (para repartir sus fotos sin repetirlas)
    def seg_end(t):
        nxt = [st for st, sj in segs if st > t + 0.05]
        return nxt[0] if nxt else dur
    final_pool = sorted(glob.glob(os.path.join(images_dir, "final_*.jpg")))[:1]
    ctx_all = sorted(glob.glob(os.path.join(images_dir, "ctx_*.jpg")))
    # fotos de contexto con palabras clave en el nombre (ej. multa.jpg, descenso.jpg): salen cuando se dice esa palabra
    GEN = {"images", "image", "descarga", "foto", "img", "captura", "screenshot", "jpeg", "unnamed"}
    topic = []
    for pth in ctx_all:
        stem = os.path.basename(pth)[:-4].split("__", 1)[-1]
        toks = [t_ for t_ in re.split(r"[-_ ]+", stem) if len(t_) > 3 and not t_.isdigit() and t_ not in GEN]
        hit = None
        for wd in words:
            nw = norm(wd["w"])
            if any(norm(t_)[:5] and nw.startswith(norm(t_)[:max(5, len(norm(t_)) - 2)]) for t_ in toks):
                hit = wd["s"]
                break
        if hit is not None:
            topic.append((hit, pth))
    topic_paths = {p_ for _, p_ in topic}
    ctx_pool = [p_ for p_ in ctx_all if p_ not in topic_paths]
    ctx_i = 0
    shown = set()
    cnt = {}
    final_done = False
    final_from = dur * 0.66
    KW = ("alineac", "delantera", "once", "plantilla", "equipo", "imparable")
    for wd in words:
        if wd["s"] >= dur * 0.45 and any(norm(wd["w"]).startswith(k_) for k_ in KW):
            final_from = max(0.0, wd["s"] - 0.1)  # la alineacion sale justo cuando se habla de ella
            break
    while t < dur - 0.05:
        subj = subject_at(t + 0.1)
        pool = pools.get(subj) or []
        in_final = bool(final_pool) and not final_done and t >= final_from
        if in_final:
            pool = final_pool
        if not pool:
            pool = ctx_pool or next((v for v in pools.values() if v), [])
        n = max(1, len(pool))
        remaining = max(1.0, (dur - t) if in_final else (seg_end(t) - t))
        L = min(max(remaining / n, 2.8), 5.2)
        if in_final:
            L = min(L, 6.5, max(2.5, seg_end(t) - t))
            final_done = True
        if dur - t - L < 1.6:
            L = dur - t
        L = min(L, dur - t)
        kind, path, label = "foto", None, None
        def _pick(pl):
            for q_ in pl:
                if q_ not in shown:
                    return q_
            return None
        if clips and k % 3 == 2 and t > 3.0:
            kind = "clip"
            path = clips[clip_i % len(clips)]
            clip_i += 1
        elif in_final:
            path = final_pool[0]
        elif k < 2 and ctx_i < len(ctx_pool):
            path = ctx_pool[ctx_i]  # primero las fotos de portada / contexto
            ctx_i += 1
        else:
            own = pools.get(subj) or []
            path = _pick(own)
            if path:
                label = jug[subj] if subj < len(jug) else None
            else:
                path = _pick(ctx_pool) or _pick([x for v in pools.values() for x in v])
            if not path:  # todo visto: se reutiliza la menos usada, nunca la anterior
                last_ = scenes[-1]["img"] if scenes else None
                cands = [x for x in (own + ctx_pool + [y for v in pools.values() for y in v]) if x != last_ and x != (final_pool[0] if final_pool else None)]
                path = min(cands, key=lambda x: cnt.get(x, 0)) if cands else (own or ctx_pool or [None])[0]
                if own and path in own:
                    label = jug[subj] if subj < len(jug) else None
        if path and kind == "foto":
            shown.add(path)
            cnt[path] = cnt.get(path, 0) + 1
        sc = {"img": path if kind == "foto" else None, "clip": path if kind == "clip" else None,
              "label": label, "s": t, "e": t + L, "first": False, "idx": len(scenes), "subj": subj,
              "kind": kind, "rng": rng.random()}
        scenes.append(sc)
        t += L
        k += 1
    # fusionar planos consecutivos con la misma foto (un solo plano largo en vez de repetirla)
    merged = []
    for sc in scenes:
        if merged and sc["img"] and merged[-1]["img"] == sc["img"]:
            merged[-1]["e"] = sc["e"]
        else:
            merged.append(sc)
    for i, sc in enumerate(merged):
        sc["idx"] = i
    scenes = merged
    # fotos de tema: se cortan justo cuando se dice su palabra clave
    fin_rng = [(sc["s"], sc["e"]) for sc in scenes if sc["img"] and "final_" in os.path.basename(sc["img"])]
    for hit, pth in sorted(topic):
        s0 = max(0.0, hit - 0.1)
        e0 = min(dur, s0 + 3.2)
        if e0 - s0 < 2.0 or any(a_ < e0 and b_ > s0 for a_, b_ in fin_rng):
            continue
        for sc in scenes:  # ajustar a los limites de plano cercanos para no dejar huecos ni trozos de <0.4 s
            if sc["s"] < s0 <= sc["s"] + 1.2:
                s0 = sc["s"]
            if sc["s"] < e0 < sc["e"] and sc["e"] - e0 <= 1.0:
                e0 = sc["e"]
        if e0 - s0 < 1.5:
            continue
        new_ = []
        for sc in scenes:
            if sc["e"] <= s0 or sc["s"] >= e0:
                new_.append(sc)
                continue
            if sc["s"] < s0 - 0.01:
                lft = dict(sc); lft["e"] = s0; new_.append(lft)
            if sc["e"] > e0 + 0.01:
                rgt = dict(sc); rgt["s"] = e0; rgt["label"] = None; new_.append(rgt)
        new_.append({"img": pth, "clip": None, "label": None, "s": s0, "e": e0, "first": False, "idx": 0,
                     "subj": subject_at(s0), "kind": "foto", "rng": rng.random()})
        scenes = sorted(new_, key=lambda z: z["s"])
    for i, sc in enumerate(scenes):
        sc["idx"] = i
    # etiqueta solo en el primer plano de cada jugador
    seen = set()
    for sc in scenes:
        if sc["label"] and sc["subj"] not in seen:
            seen.add(sc["subj"])
            sc["first"] = True
    return scenes


class ClipReader:
    """Lee un clip como flujo de frames W x H (recorte tipo cover), en orden."""
    def __init__(self, path, ctx, start=0.0):
        self.ctx = ctx
        self.n = 0
        self.last = None
        cmd = ["ffmpeg", "-loglevel", "error", "-ss", f"{start:.2f}", "-stream_loop", "-1", "-i", path, "-an",
               "-vf", f"fps={FPS},scale={ctx.W}:{ctx.H}:force_original_aspect_ratio=increase,crop={ctx.W}:{ctx.H}",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        self.p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        self.size = ctx.W * ctx.H * 3

    def frame(self, k):
        while self.n <= k:
            b = self.p.stdout.read(self.size)
            if len(b) < self.size:
                break
            self.last = Image.frombytes("RGB", (self.ctx.W, self.ctx.H), b)
            self.n += 1
        return self.last

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass


_img_cache = {}


def load_img(path, ctx):
    """Base W x H lista para zoom. Si la foto es apaisada y habria que ampliarla mucho, se muestra entera
    sobre su propio fondo desenfocado (nitida, sin pixelar); si es vertical, recorte tipo cover."""
    key = (path, ctx.k)
    if key in _img_cache:
        return _img_cache[key]
    if path is None:
        _img_cache[key] = None
        return None
    im = Image.open(path).convert("RGB")
    W, H = ctx.W, ctx.H
    cover = max(W / im.width, H / im.height)
    if cover <= 1.45 and im.width / im.height < 0.9:
        sc = cover * 1.12  # margen para zoom
        base = im.resize((int(im.width * sc), int(im.height * sc)), Image.LANCZOS)
        mode = "cover"
    else:
        bg = im.resize((int(im.width * cover), int(im.height * cover)), Image.BILINEAR)
        bx, by = (bg.width - W) // 2, (bg.height - H) // 2
        bg = bg.crop((bx, by, bx + W, by + H)).filter(ImageFilter.GaussianBlur(ctx.s(38)))
        bg = Image.blend(bg, Image.new("RGB", (W, H), (8, 20, 14)), 0.45)
        fw = min(W, int(im.width * (3.0 if im.width < 450 else 1.7)))
        fh = int(im.height * fw / im.width)
        fg = im.resize((fw, fh), Image.LANCZOS).filter(ImageFilter.UnsharpMask(radius=2, percent=70, threshold=2))
        bg.paste(fg, ((W - fw) // 2, int(H * 0.40 - fh / 2)))
        base = bg
        mode = "contain"
    _img_cache[key] = (base, mode)
    return _img_cache[key]


def pitch_bg(ctx, t):
    im = Image.new("RGB", (ctx.W, ctx.H), GREEN)
    d = ImageDraw.Draw(im)
    n = 12
    sh = ctx.H / n
    off = (t * 20) % (2 * sh)
    for i in range(-2, n + 2):
        if i % 2 == 0:
            y0 = i * sh + off
            d.rectangle([0, y0, ctx.W, y0 + sh], fill=GREEN2)
    # círculo central tenue
    r = ctx.s(300)
    d.ellipse([ctx.W / 2 - r, ctx.H * 0.5 - r, ctx.W / 2 + r, ctx.H * 0.5 + r], outline=(40, 90, 62), width=ctx.s(6))
    d.line([0, ctx.H * 0.5, ctx.W, ctx.H * 0.5], fill=(40, 90, 62), width=ctx.s(6))
    return im


KB = [
    (1.00, 1.14, 0.5, 0.5, 0.30, 0.28),
    (1.14, 1.00, 0.42, 0.55, 0.32, 0.32),
    (1.02, 1.16, 0.58, 0.46, 0.28, 0.32),
    (1.15, 1.02, 0.5, 0.5, 0.30, 0.36),
]
_readers = {}


def photo_frame(ctx, sc, t):
    if sc.get("clip"):
        r = _readers.get(sc["idx"])
        if r is None:
            r = _readers[sc["idx"]] = ClipReader(sc["clip"], ctx, start=sc.get("rng", 0) * 2.0)
        k = max(0, int((t - sc["s"]) * FPS))
        fr = r.frame(k)
        return fr if fr is not None else pitch_bg(ctx, t)
    got = load_img(sc["img"], ctx)
    if got is None:
        return pitch_bg(ctx, t)
    im, mode = got
    dur = max(0.01, sc["e"] - sc["s"])
    p = clamp((t - sc["s"]) / dur)
    z0, z1, cx0, cx1, cy0, cy1 = KB[sc["idx"] % len(KB)]
    if mode == "contain":
        z0, z1 = 1.0 + (z0 - 1.0) * 0.5, 1.0 + (z1 - 1.0) * 0.5
    elif z0 < 1.04 and z1 < 1.04:
        z0, z1 = 1.0, 1.12
    z = z0 + (z1 - z0) * ease_io(p)
    z *= 1.0 + 0.045 * (1 - ease_out((t - sc["s"]) / 0.35))
    ar = ctx.W / ctx.H
    ch = im.height / (z if mode == "cover" else z)
    cw = ch * ar
    if cw > im.width:
        cw = im.width
        ch = cw / ar
    if mode == "cover":
        cw, ch = im.width / (1.12 * z), im.height / (1.12 * z)
        if cw / ch > ar:
            cw = ch * ar
        else:
            ch = cw / ar
    cx = cx0 + (cx1 - cx0) * p
    cy = cy0 + (cy1 - cy0) * p
    x0 = clamp(cx * im.width - cw / 2, 0, im.width - cw)
    y0 = clamp(cy * im.height - ch / 2, 0, im.height - ch)
    return im.resize((ctx.W, ctx.H), Image.BICUBIC, box=(x0, y0, x0 + cw, y0 + ch))


def scene_at(scenes, t):
    for sc in scenes:
        if sc["s"] <= t < sc["e"]:
            return sc
    return scenes[-1]


def make_overlay(ctx):
    """Velo casi nulo: las fotos se ven luminosas; solo un toque arriba para la marca."""
    h, w = ctx.H, ctx.W
    y = np.linspace(0, 1, h)[:, None]
    a = 0.04 + 0.30 * np.clip((0.12 - y) / 0.12, 0, 1) + 0.10 * np.clip((y - 0.62) / 0.38, 0, 1)
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., 3] = (np.clip(a, 0, 0.5) * 255).astype(np.uint8)[:, 0:1].repeat(w, axis=1)
    return Image.fromarray(arr, "RGBA")


def text_size(d, txt, font, stroke=0):
    b = d.textbbox((0, 0), txt, font=font, stroke_width=stroke)
    return b[2] - b[0], b[3] - b[1], b


def draw_text_c(layer, cx, cy, txt, font, fill, stroke=0, stroke_fill=(0, 0, 0), alpha=1.0):
    d = ImageDraw.Draw(layer)
    w, h, b = text_size(d, txt, font, stroke)
    x = cx - w / 2 - b[0]
    y = cy - h / 2 - b[1]
    d.text((x, y), txt, font=font, fill=fill + (int(255 * alpha),), stroke_width=stroke,
           stroke_fill=stroke_fill + (int(255 * alpha),))


def wrap_lines(d, text, font, maxw, stroke=0):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if text_size(d, trial, font, stroke)[0] <= maxw or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def paste_scaled(base, layer, scale, cx, cy):
    """Pega un layer RGBA reescalado alrededor de (cx,cy)."""
    if scale <= 0.01:
        return
    lw, lh = layer.size
    nl = layer.resize((max(1, int(lw * scale)), max(1, int(lh * scale))), Image.BILINEAR)
    base.alpha_composite(nl, (int(cx - nl.width / 2), int(cy - nl.height / 2)))


def build_caption_groups(words):
    groups, cur = [], []
    for i, wd in enumerate(words):
        cur.append(i)
        punct = wd["w"].endswith((",", ".", "?", "!", ";", ":"))
        if len(cur) >= 3 or (punct and len(cur) >= 2):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def render_caption(ctx, words, groups, t):
    """Subtitulos limpios: 1-2 lineas en el tercio inferior, la palabra que suena en amarillo."""
    layer = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    gi = None
    for k, g in enumerate(groups):
        gs = words[g[0]]["s"]
        ge = words[g[-1]]["e"]
        nxt = words[groups[k + 1][0]]["s"] if k + 1 < len(groups) else ge + 0.4
        if gs - 0.05 <= t < nxt:
            gi = k
            break
    if gi is None:
        return layer
    g = groups[gi]
    d = ImageDraw.Draw(layer)
    font = ctx.font(84)
    gap = ctx.s(34)
    maxw = ctx.s(930)
    txts = [words[i]["w"].upper().strip(",.;:¡!¿?\"") or words[i]["w"] for i in g]
    # el signo de pregunta se conserva al final de la ultima palabra de la frase
    for n, i in enumerate(g):
        w0 = words[i]["w"]
        if w0.endswith("?") and not txts[n].endswith("?"):
            txts[n] += "?"
        if w0.endswith("!") and not txts[n].endswith("!"):
            txts[n] += "!"
    lines, cur, curw = [], [], 0
    for i, tx in zip(g, txts):
        wpx = text_size(d, tx, font)[0]
        if cur and curw + gap + wpx > maxw:
            lines.append(cur)
            cur, curw = [], 0
        cur.append((i, tx, wpx))
        curw += wpx + (gap if len(cur) > 1 else 0)
    if cur:
        lines.append(cur)
    lh = ctx.s(104)
    y0 = ctx.H * 0.775 - (len(lines) - 1) * lh / 2
    gp = ease_out((t - (words[g[0]]["s"] - 0.05)) / 0.16)
    shadow = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    stroke = ctx.s(8)
    for li, line in enumerate(lines):
        total = sum(x[2] for x in line) + gap * (len(line) - 1)
        x = ctx.W / 2 - total / 2
        cy = y0 + li * lh
        for (i, tx, wpx) in line:
            active = words[i]["s"] <= t < words[i]["e"]
            w_, h_, b_ = text_size(d, tx, font, stroke)
            pos = (x - b_[0], cy - h_ / 2 - b_[1])
            sd.text((pos[0] + ctx.s(2), pos[1] + ctx.s(7)), tx, font=font, fill=(0, 0, 0, 120),
                    stroke_width=stroke, stroke_fill=(0, 0, 0, 120))
            if active:
                pop = 1.0 + 0.06 * math.sin(clamp((t - words[i]["s"]) / 0.14) * math.pi)
                d.text(pos, tx, font=font, fill=YELLOW + (255,), stroke_width=int(stroke * pop),
                       stroke_fill=(0, 0, 0, 255))
            else:
                d.text(pos, tx, font=font, fill=(255, 255, 255, 255), stroke_width=stroke,
                       stroke_fill=(0, 0, 0, 255))
            x += wpx + gap
    shadow = shadow.filter(ImageFilter.GaussianBlur(ctx.s(7)))
    shadow.alpha_composite(layer)
    layer = shadow
    if gp < 1.0:
        sh = ctx.s(18) * (1 - clamp(gp))
        layer = layer.transform(layer.size, Image.AFFINE, (1, 0, 0, 0, 1, sh))
    return layer


def render_header(ctx, label, t):
    layer = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    r = ctx.s(30)
    cx, cy = ctx.s(78), ctx.s(112)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE + (255,), outline=(0, 0, 0, 255), width=ctx.s(4))
    f = ctx.font(30)
    w_, h_, b_ = text_size(d, "SP", f)
    d.text((cx - w_ / 2 - b_[0], cy - h_ / 2 - b_[1]), "SP", font=f, fill=(0, 0, 0, 255))
    ft = ctx.font(34)
    d.text((cx + r + ctx.s(18), cy - ctx.s(22)), "SEGUNDO PALO", font=ft, fill=WHITE + (255,),
           stroke_width=ctx.s(4), stroke_fill=(0, 0, 0, 255))
    return layer


def render_hook(ctx, text, t):
    t0, t1 = 0.10, 2.6
    if not (t0 <= t < t1):
        return None
    layer = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = ctx.font(104)
    stroke = ctx.s(8)
    lines = wrap_lines(d, text.upper(), font, ctx.s(900), stroke)
    lh = ctx.s(124)
    y0 = ctx.H * 0.70 - (len(lines) - 1) * lh / 2
    for i, ln in enumerate(lines):
        draw_text_c(layer, ctx.W / 2, y0 + i * lh, ln, font, WHITE if i % 2 == 0 else YELLOW, stroke)
    pop = 0.55 + 0.45 * ease_back((t - t0) / 0.38)
    out_a = 1.0 - ease_io((t - (t1 - 0.3)) / 0.3)
    if out_a < 1:
        a = layer.split()[3].point(lambda v: int(v * out_a))
        layer.putalpha(a)
    full = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    paste_scaled(full, layer, pop, ctx.W / 2, ctx.H / 2)
    return full


def render_nametag(ctx, label, sc_t):
    if not label or sc_t > 2.4:
        return None
    p = ease_out(sc_t / 0.4)
    out = 1.0 - ease_io((sc_t - 2.0) / 0.4)
    layer = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = ctx.font(46)
    txt = label.upper()
    w_, h_, b_ = text_size(d, txt, font)
    x = ctx.s(70) - (1 - p) * ctx.s(60)
    y = ctx.H * 0.655
    d.rectangle([x, y - h_ / 2 - ctx.s(4), x + ctx.s(8), y + h_ / 2 + ctx.s(6)], fill=YELLOW + (255,))
    d.text((x + ctx.s(26) - b_[0], y - h_ / 2 - b_[1]), txt, font=font, fill=(0, 0, 0, 255),
           stroke_width=ctx.s(5), stroke_fill=(255, 255, 255, 255))
    al = clamp(p) * clamp(out)
    layer.putalpha(layer.split()[3].point(lambda v: int(v * al)))
    return layer


def render_progress(ctx, p):
    layer = Image.new("RGBA", (ctx.W, ctx.s(6)), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rectangle([0, 0, ctx.W, ctx.s(6)], fill=(255, 255, 255, 60))
    d.rectangle([0, 0, ctx.W * clamp(p), ctx.s(6)], fill=YELLOW + (255,))
    return layer


def render_endcard(ctx, t, dur):
    """Final limpio: solo el boton de suscribirse, sobre los subtitulos."""
    t0 = dur - 3.0
    if t < t0:
        return None
    p = ease_out((t - t0) / 0.45)
    pulse = 1.0 + 0.04 * math.sin((t - t0) * 7)
    layer = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    d3 = ImageDraw.Draw(layer)
    fb = ctx.font(52)
    txt = "SUSCRÍBETE"
    w_, h_, b_ = text_size(d3, txt, fb)
    px, py = ctx.s(54), ctx.s(24)
    cy = ctx.H * 0.575
    rect = [ctx.W / 2 - w_ / 2 - px, cy - h_ / 2 - py, ctx.W / 2 + w_ / 2 + px, cy + h_ / 2 + py]
    d3.rounded_rectangle(rect, radius=ctx.s(40), fill=(220, 30, 30, 255))
    d3.text((ctx.W / 2 - w_ / 2 - b_[0], cy - h_ / 2 - b_[1]), txt, font=fb, fill=WHITE + (255,))
    full = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    paste_scaled(full, layer, pulse * (0.7 + 0.3 * p), ctx.W / 2, cy)
    al = clamp(p)
    full.putalpha(full.split()[3].point(lambda v: int(v * al)))
    return full


def find_stats(words):
    """Cifras del guion (12 goles, 5-0, 2-1...) para destacarlas en pantalla cuando se dicen."""
    out = []
    for i, w in enumerate(words):
        tok = w["w"].strip(",.;:¡!¿?\"")
        if re.search(r"\d", tok) and len(tok) <= 6:
            label = tok
            if i + 1 < len(words):
                nx = words[i + 1]["w"].strip(",.;:¡!¿?\"")
                if nx.isalpha() and len(nx) <= 12 and not words[i]["w"].endswith((",", ".", "?", "!")):
                    label = tok + " " + nx
            if not out or w["s"] - out[-1][0] > 2.5:
                out.append((w["s"], label.upper()))
    return out


def render_stat(ctx, stats, t):
    for t0, label in stats:
        if t0 - 0.05 <= t < t0 + 1.5:
            lt = t - (t0 - 0.05)
            p = ease_back(lt / 0.32)
            out_a = 1.0 - ease_io(clamp((lt - 1.2) / 0.3))
            layer = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            font = ctx.font(96)
            w_, h_, b_ = text_size(d, label, font)
            px, py = ctx.s(46), ctx.s(22)
            cy = ctx.H / 2  # se dibuja centrado y se coloca arriba al pegarlo
            rect = [ctx.W / 2 - w_ / 2 - px, cy - h_ / 2 - py, ctx.W / 2 + w_ / 2 + px, cy + h_ / 2 + py]
            d.rounded_rectangle(rect, radius=ctx.s(30), fill=YELLOW + (255,))
            d.text((ctx.W / 2 - w_ / 2 - b_[0], cy - h_ / 2 - b_[1]), label, font=font, fill=(0, 0, 0, 255))
            full = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
            paste_scaled(full, layer, max(0.01, 0.6 + 0.4 * p), ctx.W / 2, ctx.H * 0.135)
            full.putalpha(full.split()[3].point(lambda v: int(v * clamp(out_a) * clamp(lt / 0.12))))
            return full
    return None


def build_fx(path, dur, cuts, hits):
    """Efectos de sonido originales (sintetizados): colchon suave, 'whoosh' en cada corte y golpe grave en los datos."""
    sr = 44100
    n = int(dur * sr) + sr
    rng = np.random.default_rng(7)
    t = np.arange(n) / sr
    bed = np.zeros(n)
    for f, a in ((55.0, 0.5), (82.4, 0.35), (110.0, 0.3), (164.8, 0.18), (220.0, 0.1)):
        bed += a * np.sin(2 * np.pi * f * t + rng.random() * 6)
    bed *= 0.55 + 0.45 * np.sin(2 * np.pi * 0.11 * t)
    bed *= np.clip(t / 1.5, 0, 1) * np.clip((dur - t) / 1.5, 0, 1)
    fx = 0.10 * bed / max(1e-6, np.abs(bed).max())

    def add(sig, t0):
        i0 = int(t0 * sr)
        if i0 < 0 or i0 >= n:
            return
        m = min(len(sig), n - i0)
        fx[i0:i0 + m] += sig[:m]

    # whoosh en cada corte
    L = int(0.30 * sr)
    tt = np.linspace(0, 1, L)
    env = np.sin(np.pi * tt) ** 2
    for c in cuts:
        nz = rng.standard_normal(L)
        y = np.zeros(L)
        a_prev = 0.0
        for i in range(L):
            cut = 0.02 + 0.30 * tt[i]
            a_prev += cut * (nz[i] - a_prev)
            y[i] = a_prev
        y *= env
        y /= max(1e-6, np.abs(y).max())
        add(0.16 * y, max(0.0, c - 0.12))
    # golpe grave
    L2 = int(0.55 * sr)
    t2 = np.arange(L2) / sr
    boom = np.sin(2 * np.pi * (70 - 35 * t2) * t2) * np.exp(-t2 * 6.5)
    boom[:200] *= np.linspace(0, 1, 200)
    for h in hits:
        add(0.50 * boom, h)
    fx = np.clip(fx, -0.95, 0.95)
    import wave
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((fx * 32767).astype(np.int16).tobytes())
    return path


class Renderer:
    def __init__(self, data, audio, images_dir, scale=1.0, clips_dir=None):
        self.ctx = Ctx(scale)
        self.data = data
        self.dur = audio_duration(audio)
        self.words = word_timings(data["guion"], self.dur)
        wa = None
        try:
            wa = whisper_align(self.words, whisper_words(audio), self.dur)
        except Exception as e:
            print("alineacion whisper fallida:", e)
        if wa:
            self.words = wa
        else:
            try:
                self.words = align_timings(self.words, detect_pauses(audio), self.dur)
            except Exception as e:
                print("alineacion fallida, uso estimacion:", e)
        self.groups = build_caption_groups(self.words)
        self.scenes = build_scenes(data, self.words, self.dur, images_dir, clips_dir)
        self.stats = find_stats(self.words)
        print("cifras destacadas:", self.stats)
        self.overlay = make_overlay(self.ctx)
        self.label = data.get("etiqueta", "DEBATE DEL DÍA")
        self.header = render_header(self.ctx, self.label, 0)

    def bg(self, t):
        sc = scene_at(self.scenes, t)
        cur = photo_frame(self.ctx, sc, t)
        local = t - sc["s"]
        if False:  # corte limpio: el fundido dejaba fotos dobles superpuestas
            pass
        return cur.convert("RGBA"), sc

    def frame(self, t):
        ctx = self.ctx
        im, sc = self.bg(t)
        im.alpha_composite(self.overlay)
        im.alpha_composite(self.header)
        im.alpha_composite(render_progress(ctx, t / self.dur), (0, 0))
        nt_t = t - max(sc["s"], 2.7 if self.data.get("gancho") else 0.0)
        nt = render_nametag(ctx, sc["label"], nt_t) if (sc["label"] and sc["first"] and nt_t >= 0) else None
        if nt is not None:
            im.alpha_composite(nt)
        st = render_stat(ctx, self.stats, t)
        if st is not None:
            im.alpha_composite(st)
        hk = render_hook(ctx, self.data.get("gancho", ""), t)
        if hk is not None:
            im.alpha_composite(hk)
        if not (t < 2.6 and self.data.get("gancho")):
            im.alpha_composite(render_caption(ctx, self.words, self.groups, t))
        ec = render_endcard(ctx, t, self.dur)
        if ec is not None:
            im.alpha_composite(ec)
        return im.convert("RGB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--images", default="imgs")
    ap.add_argument("--clips", default="")
    ap.add_argument("--out", default="salida.mp4")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--stills", default="")
    ap.add_argument("--outdir", default="stills")
    ap.add_argument("--seconds", type=float, default=0)
    a = ap.parse_args()
    data = json.load(open(a.data, encoding="utf-8"))
    r = Renderer(data, a.audio, a.images, a.scale, a.clips or None)
    if a.stills:
        os.makedirs(a.outdir, exist_ok=True)
        for ts in a.stills.split(","):
            t = float(ts)
            r.frame(t).save(os.path.join(a.outdir, f"f_{t:05.2f}.png"))
        print("ok stills; dur", r.dur)
        return
    total = int((a.seconds or r.dur) * FPS)
    fxp = None
    try:
        cuts = [sc["s"] for sc in r.scenes if sc["s"] > 0.5]
        hits = [0.1] + [x[0] - 0.05 for x in r.stats]
        fxp = build_fx(os.path.join(os.path.dirname(os.path.abspath(a.out)) or ".", "fx.wav"), r.dur, cuts, hits)
    except Exception as e:
        print("sin efectos de sonido:", e)
    base = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{r.ctx.W}x{r.ctx.H}", "-r", str(FPS), "-i", "-", "-i", a.audio]
    if fxp:
        cmd = base + ["-i", fxp, "-filter_complex",
                      "[1:a]volume=1.0[a1];[2:a]volume=0.5[a2];[a1][a2]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]",
                      "-map", "0:v", "-map", "[a]"]
    else:
        cmd = base
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", a.out]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for n in range(total):
        p.stdin.write(r.frame(n / FPS).tobytes())
        if n % 150 == 0:
            print(f"{n}/{total}", flush=True)
    p.stdin.close()
    p.wait()
    for rd in _readers.values():
        rd.close()
    print("listo", a.out)


if __name__ == "__main__":
    main()
