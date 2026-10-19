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


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("á", "a").replace("é", "e").replace("í", "i")
                  .replace("ó", "o").replace("ú", "u").replace("ñ", "n").replace("ü", "u"))


def build_scenes(data, words, dur, images_dir, clips_dir=None):
    """Plano nuevo cada ~2.4 s. Cada jugador usa sus fotos en rotacion; entre medias, clips libres."""
    import glob, random
    jug = [re.sub(r"\s*\(.*?\)", "", j).strip() for j in data.get("jugadores", [])][:3]
    marks = []
    for name in jug:
        keys = [norm(p) for p in name.split() if len(p) > 2][::-1]
        t = None
        for wd in words:
            nw = norm(wd["w"])
            if any(k and k in nw for k in keys):
                t = wd["s"]
                break
        marks.append(t)
    # quien habla en cada momento: el ultimo jugador mencionado (el primero antes de la primera mencion)
    def subject_at(t):
        cur = 0
        for i, m in enumerate(marks):
            if m is not None and m - 0.2 <= t:
                cur = i
        return cur

    def imgs_of(key):
        return sorted(glob.glob(os.path.join(images_dir, f"{key}_*.jpg")))

    pools = {i: imgs_of(f"p{i+1}") or [p for p in [os.path.join(images_dir, f"img_{i+1}.jpg")] if os.path.exists(p)]
             for i in range(len(jug))}
    est = imgs_of("estadio") or [p for p in [os.path.join(images_dir, "img_estadio.jpg")] if os.path.exists(p)]
    rng = random.Random(int(dur * 1000))
    clips = []
    if clips_dir and os.path.isdir(clips_dir):
        clips = sorted(glob.glob(os.path.join(clips_dir, "*.mp4")) + glob.glob(os.path.join(clips_dir, "*.mov"))
                       + glob.glob(os.path.join(clips_dir, "*.webm")))
        rng.shuffle(clips)
    used = {i: 0 for i in pools}
    scenes = []
    t = 0.0
    end_card = dur - 3.4
    k = 0
    clip_i = 0
    est_done = False
    while t < dur - 0.05:
        L = min(rng.uniform(2.1, 2.9), dur - t)
        if dur - t - L < 1.2:
            L = dur - t
        subj = subject_at(t + 0.2)
        kind = "foto"
        path, label = None, None
        # cada 3er plano un clip libre (si hay); un plano de estadio una vez a mitad
        if clips and k % 3 == 2 and t > 3.0 and t < end_card - 2.0:
            kind = "clip"
            path = clips[clip_i % len(clips)]
            clip_i += 1
        elif est and not est_done and t > dur * 0.45 and t < end_card - 2.0:
            path = est[0]
            est_done = True
        else:
            pool = pools.get(subj) or []
            if pool:
                path = pool[used[subj] % len(pool)]
                used[subj] += 1
                label = jug[subj]
        sc = {"img": path if kind == "foto" else None, "clip": path if kind == "clip" else None,
              "label": label, "s": t, "e": t + L, "first": False, "idx": len(scenes), "subj": subj,
              "kind": kind, "rng": rng.random()}
        scenes.append(sc)
        t += L
        k += 1
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
    if cover <= 1.45:
        sc = cover * 1.12  # margen para zoom
        base = im.resize((int(im.width * sc), int(im.height * sc)), Image.LANCZOS)
        mode = "cover"
    else:
        bg = im.resize((int(im.width * cover), int(im.height * cover)), Image.BILINEAR)
        bx, by = (bg.width - W) // 2, (bg.height - H) // 2
        bg = bg.crop((bx, by, bx + W, by + H)).filter(ImageFilter.GaussianBlur(ctx.s(38)))
        bg = Image.blend(bg, Image.new("RGB", (W, H), (8, 20, 14)), 0.45)
        fw = W
        fh = int(im.height * fw / im.width)
        fg = im.resize((fw, fh), Image.LANCZOS)
        bg.paste(fg, (0, int(H * 0.40 - fh / 2)))
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
    """Degradado suave: algo arriba (marca) y mas abajo (subtitulos). La foto se ve limpia en medio."""
    h, w = ctx.H, ctx.W
    y = np.linspace(0, 1, h)[:, None]
    a = 0.10 + 0.40 * np.clip((0.14 - y) / 0.14, 0, 1) + 0.72 * np.clip((y - 0.58) / 0.42, 0, 1) ** 1.3
    a = np.clip(a, 0, 0.88)
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., 3] = (a * 255).astype(np.uint8)[:, 0:1].repeat(w, axis=1)
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
    gap = ctx.s(24)
    maxw = ctx.s(940)
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
    for li, line in enumerate(lines):
        total = sum(x[2] for x in line) + gap * (len(line) - 1)
        x = ctx.W / 2 - total / 2
        cy = y0 + li * lh
        for (i, tx, wpx) in line:
            active = words[i]["s"] <= t < words[i]["e"]
            w_, h_, b_ = text_size(d, tx, font)
            pos = (x - b_[0], cy - h_ / 2 - b_[1])
            sd.text((pos[0] + ctx.s(4), pos[1] + ctx.s(6)), tx, font=font, fill=(0, 0, 0, 200))
            d.text(pos, tx, font=font, fill=(YELLOW if active else WHITE) + (255,))
            x += wpx + gap
    shadow = shadow.filter(ImageFilter.GaussianBlur(ctx.s(6)))
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
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=YELLOW + (255,))
    f = ctx.font(30)
    w_, h_, b_ = text_size(d, "SP", f)
    d.text((cx - w_ / 2 - b_[0], cy - h_ / 2 - b_[1]), "SP", font=f, fill=GREEN + (255,))
    ft = ctx.font(34)
    d.text((cx + r + ctx.s(18), cy - ctx.s(22)), "SEGUNDO PALO", font=ft, fill=WHITE + (235,))
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
    y0 = ctx.H * 0.40 - (len(lines) - 1) * lh / 2
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
    d.text((x + ctx.s(26) - b_[0], y - h_ / 2 - b_[1]), txt, font=font, fill=WHITE + (255,))
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


class Renderer:
    def __init__(self, data, audio, images_dir, scale=1.0, clips_dir=None):
        self.ctx = Ctx(scale)
        self.data = data
        self.dur = audio_duration(audio)
        self.words = word_timings(data["guion"], self.dur)
        self.groups = build_caption_groups(self.words)
        self.scenes = build_scenes(data, self.words, self.dur, images_dir, clips_dir)
        self.overlay = make_overlay(self.ctx)
        self.label = data.get("etiqueta", "DEBATE DEL DÍA")
        self.header = render_header(self.ctx, self.label, 0)

    def bg(self, t):
        sc = scene_at(self.scenes, t)
        cur = photo_frame(self.ctx, sc, t)
        local = t - sc["s"]
        if sc["idx"] > 0 and local < 0.28:
            prev = self.scenes[sc["idx"] - 1]
            old = photo_frame(self.ctx, prev, min(t, prev["e"] - 1e-3))
            p = ease_io(local / 0.28)
            cur = Image.blend(old.convert("RGB"), cur.convert("RGB"), p)
        return cur.convert("RGBA"), sc

    def frame(self, t):
        ctx = self.ctx
        im, sc = self.bg(t)
        im.alpha_composite(self.overlay)
        im.alpha_composite(self.header)
        im.alpha_composite(render_progress(ctx, t / self.dur), (0, 0))
        nt = render_nametag(ctx, sc["label"], t - sc["s"]) if (sc["label"] and sc["first"]) else None
        if nt is not None:
            im.alpha_composite(nt)
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
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{r.ctx.W}x{r.ctx.H}", "-r", str(FPS), "-i", "-", "-i", a.audio,
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
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
