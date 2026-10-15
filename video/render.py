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


def build_scenes(data, words, dur, images_dir):
    jug = [re.sub(r"\s*\(.*?\)", "", j).strip() for j in data.get("jugadores", [])][:3]
    # primera mención de cada jugador (por apellido / última palabra)
    marks = []
    for i, name in enumerate(jug):
        keys = [norm(p) for p in name.split() if len(p) > 2]
        keys = keys[::-1]  # apellido primero
        t = None
        for wd in words:
            nw = norm(wd["w"])
            if any(k and k in nw for k in keys):
                t = wd["s"]
                break
        marks.append(t)
    # asegurar orden y huecos razonables
    starts = []
    last = 3.5
    for i, t in enumerate(marks):
        if t is None:
            t = last + (dur - last) / (len(jug) - i + 1)
        t = max(t - 0.25, last + 2.0)
        t = min(t, dur - 2.0 * (len(jug) - i))
        starts.append(t)
        last = t
    bounds = [0.0] + starts + [dur]

    def img(name):
        p = os.path.join(images_dir, name)
        return p if os.path.exists(p) else None

    base = [("img_estadio.jpg", None)] + [(f"img_{i+1}.jpg", jug[i]) for i in range(len(jug))]
    scenes = []
    for i, (fn, label) in enumerate(base):
        a, b = bounds[i], bounds[i + 1]
        p = img(fn)
        # trocear escenas largas en planos de ~4.5 s con encuadres distintos
        n = max(1, int(round((b - a) / 4.6)))
        for j in range(n):
            sa = a + (b - a) * j / n
            sb = a + (b - a) * (j + 1) / n
            scenes.append({"img": p, "label": label, "s": sa, "e": sb, "first": j == 0, "idx": len(scenes)})
    return scenes


_img_cache = {}


def load_img(path, ctx):
    key = (path, ctx.k)
    if key in _img_cache:
        return _img_cache[key]
    if path is None:
        im = None
    else:
        im = Image.open(path).convert("RGB")
        # cover-fit con margen para zoom
        sc = max(ctx.W / im.width, ctx.H / im.height) * 1.0
        im = im.resize((int(im.width * sc), int(im.height * sc)), Image.LANCZOS)
    _img_cache[key] = im
    return im


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


# direcciones de Ken Burns (zoom_ini, zoom_fin, cx_ini, cx_fin, cy_ini, cy_fin) en 0..1 del recorrido sobrante
KB = [
    (1.06, 1.30, 0.5, 0.5, 0.35, 0.30),
    (1.30, 1.08, 0.35, 0.55, 0.40, 0.40),
    (1.10, 1.32, 0.6, 0.45, 0.30, 0.35),
    (1.28, 1.08, 0.5, 0.5, 0.30, 0.45),
]


def photo_frame(ctx, sc, t):
    im = load_img(sc["img"], ctx)
    if im is None:
        return pitch_bg(ctx, t)
    dur = max(0.01, sc["e"] - sc["s"])
    p = clamp((t - sc["s"]) / dur)
    z0, z1, cx0, cx1, cy0, cy1 = KB[sc["idx"] % len(KB)]
    z = z0 + (z1 - z0) * p
    cw, ch = im.width / z, im.height / z
    # relación objetivo
    ar = ctx.W / ctx.H
    if cw / ch > ar:
        cw = ch * ar
    else:
        ch = cw / ar
    cx = cx0 + (cx1 - cx0) * p
    cy = cy0 + (cy1 - cy0) * p
    x0 = clamp(cx * im.width - cw / 2, 0, im.width - cw)
    y0 = clamp(cy * im.height - ch / 2, 0, im.height - ch)
    return im.resize((ctx.W, ctx.H), Image.BILINEAR, box=(x0, y0, x0 + cw, y0 + ch))


def scene_at(scenes, t):
    for sc in scenes:
        if sc["s"] <= t < sc["e"]:
            return sc
    return scenes[-1]


def make_overlay(ctx):
    """Degradado oscuro arriba/abajo + velo general (precalculado)."""
    h, w = ctx.H, ctx.W
    y = np.linspace(0, 1, h)[:, None]
    a = 0.30 + 0.45 * np.clip((0.22 - y) / 0.22, 0, 1) + 0.50 * np.clip((y - 0.55) / 0.45, 0, 1)
    a = np.clip(a, 0, 0.9)
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
        if len(cur) >= 4 or (punct and len(cur) >= 2):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def render_caption(ctx, words, groups, t):
    """Devuelve layer RGBA con el grupo de palabras activo, la palabra actual resaltada en pastilla amarilla."""
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
    font = ctx.font(86)
    stroke = ctx.s(9)
    maxw = ctx.s(930)
    gap = ctx.s(26)
    txts = [words[i]["w"].upper() for i in g]
    # partir en líneas
    lines, cur, curw = [], [], 0
    for i, tx in zip(g, txts):
        wpx = text_size(d, tx, font, stroke)[0]
        if cur and curw + gap + wpx > maxw:
            lines.append(cur)
            cur, curw = [], 0
        cur.append((i, tx, wpx))
        curw += wpx + (gap if len(cur) > 1 else 0)
    if cur:
        lines.append(cur)
    lh = ctx.s(120)
    y0 = ctx.H * 0.60 - (len(lines) - 1) * lh / 2
    # pop de entrada del grupo
    gp = ease_back((t - (words[g[0]]["s"] - 0.05)) / 0.18)
    for li, line in enumerate(lines):
        total = sum(x[2] for x in line) + gap * (len(line) - 1)
        x = ctx.W / 2 - total / 2
        cy = y0 + li * lh
        for (i, tx, wpx) in line:
            active = words[i]["s"] <= t < words[i]["e"]
            if active:
                pop = 1.0 + 0.10 * math.sin(clamp((t - words[i]["s"]) / 0.14) * math.pi)
                pad_x, pad_y = ctx.s(22), ctx.s(10)
                w_, h_, b_ = text_size(d, tx, font, 0)
                rect = [x - pad_x, cy - h_ / 2 - pad_y - ctx.s(4), x + wpx + pad_x, cy + h_ / 2 + pad_y + ctx.s(4)]
                cxr, cyr = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
                rw, rh = rect[2] - rect[0], rect[3] - rect[1]
                rect = [cxr - rw * pop / 2, cyr - rh * pop / 2, cxr + rw * pop / 2, cyr + rh * pop / 2]
                d.rounded_rectangle(rect, radius=ctx.s(26), fill=YELLOW + (255,))
                d.text((x - b_[0], cy - h_ / 2 - b_[1]), tx, font=font, fill=GREEN + (255,))
            else:
                w_, h_, b_ = text_size(d, tx, font, stroke)
                d.text((x - b_[0], cy - h_ / 2 - b_[1]), tx, font=font, fill=WHITE + (255,),
                       stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            x += wpx + gap
    if gp < 1.0:
        sh = ctx.s(26) * (1 - clamp(gp))
        layer = layer.transform(layer.size, Image.AFFINE, (1, 0, 0, 0, 1, sh))
    return layer


def render_header(ctx, label, t):
    layer = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # insignia SP
    r = ctx.s(46)
    cx, cy = ctx.s(80), ctx.s(120)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GREEN + (255,), outline=YELLOW + (255,), width=ctx.s(5))
    f = ctx.font(46)
    wS = text_size(d, "S", f)[0]
    wP = text_size(d, "P", f)[0]
    xx = cx - (wS + wP) / 2
    bS = text_size(d, "S", f)[2]
    d.text((xx - bS[0], cy - text_size(d, "S", f)[1] / 2 - bS[1]), "S", font=f, fill=WHITE + (255,))
    bP = text_size(d, "P", f)[2]
    d.text((xx + wS - bP[0], cy - text_size(d, "P", f)[1] / 2 - bP[1]), "P", font=f, fill=YELLOW + (255,))
    ft = ctx.font(44)
    d.text((cx + r + ctx.s(22), cy - ctx.s(30)), "SEGUNDO PALO", font=ft, fill=YELLOW + (255,),
           stroke_width=ctx.s(2), stroke_fill=(0, 0, 0, 255))
    # pastilla
    fp = ctx.font(34)
    w_, h_, b_ = text_size(d, label, fp)
    px, py = ctx.s(26), ctx.s(12)
    x1 = ctx.W - ctx.s(46)
    rect = [x1 - w_ - 2 * px, cy - h_ / 2 - py, x1, cy + h_ / 2 + py]
    d.rounded_rectangle(rect, radius=ctx.s(30), fill=YELLOW + (255,))
    d.text((rect[0] + px - b_[0], cy - h_ / 2 - b_[1]), label, font=fp, fill=GREEN + (255,))
    return layer


def render_hook(ctx, text, t):
    t0, t1 = 0.15, 2.9
    if not (t0 <= t < t1):
        return None
    layer = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = ctx.font(128)
    stroke = ctx.s(12)
    lines = wrap_lines(d, text.upper(), font, ctx.s(900), stroke)
    lh = ctx.s(150)
    y0 = ctx.H * 0.33 - (len(lines) - 1) * lh / 2
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
    if not label:
        return None
    p = ease_out(sc_t / 0.45)
    layer = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = ctx.font(62)
    txt = label.upper()
    w_, h_, b_ = text_size(d, txt, font)
    px, py = ctx.s(38), ctx.s(20)
    rect_w = w_ + 2 * px
    x_final = ctx.W / 2 - rect_w / 2
    x = x_final - (1 - p) * (ctx.W / 2 + rect_w / 2)
    cy = ctx.H * 0.80
    rect = [x, cy - h_ / 2 - py, x + rect_w, cy + h_ / 2 + py]
    d.rounded_rectangle(rect, radius=ctx.s(22), fill=YELLOW + (255,))
    d.rectangle([rect[0], rect[1], rect[0] + ctx.s(14), rect[3]], fill=GREEN + (255,))
    d.text((rect[0] + px - b_[0], cy - h_ / 2 - b_[1]), txt, font=font, fill=GREEN + (255,))
    return layer


def render_progress(ctx, p):
    layer = Image.new("RGBA", (ctx.W, ctx.s(10)), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rectangle([0, 0, ctx.W, ctx.s(10)], fill=(255, 255, 255, 60))
    d.rectangle([0, 0, ctx.W * clamp(p), ctx.s(10)], fill=YELLOW + (255,))
    return layer


def render_endcard(ctx, t, dur):
    t0 = dur - 3.4
    if t < t0:
        return None
    p = ease_out((t - t0) / 0.5)
    layer = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    veil = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, int(120 * p)))
    layer.alpha_composite(veil)
    f1 = ctx.font(96)
    stroke = ctx.s(10)
    lay2 = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    draw_text_c(lay2, ctx.W / 2, ctx.H * 0.27, "¿DE QUÉ LADO", f1, WHITE, stroke)
    draw_text_c(lay2, ctx.W / 2, ctx.H * 0.27 + ctx.s(115), "ESTÁS TÚ?", f1, YELLOW, stroke)
    paste_scaled(layer, lay2, 0.7 + 0.3 * p, ctx.W / 2, ctx.H / 2)
    # botón suscribirse
    pulse = 1.0 + 0.05 * math.sin((t - t0) * 7)
    lay3 = Image.new("RGBA", (ctx.W, ctx.H), (0, 0, 0, 0))
    d3 = ImageDraw.Draw(lay3)
    fb = ctx.font(64)
    txt = "SUSCRÍBETE"
    w_, h_, b_ = text_size(d3, txt, fb)
    px, py = ctx.s(60), ctx.s(30)
    cy = ctx.H * 0.80
    rect = [ctx.W / 2 - w_ / 2 - px, cy - h_ / 2 - py, ctx.W / 2 + w_ / 2 + px, cy + h_ / 2 + py]
    d3.rounded_rectangle(rect, radius=ctx.s(46), fill=(220, 30, 30, 255))
    d3.text((ctx.W / 2 - w_ / 2 - b_[0], cy - h_ / 2 - b_[1]), txt, font=fb, fill=WHITE + (255,))
    paste_scaled(layer, lay3, pulse * (0.6 + 0.4 * p), ctx.W / 2, ctx.H / 2)
    return layer


class Renderer:
    def __init__(self, data, audio, images_dir, scale=1.0):
        self.ctx = Ctx(scale)
        self.data = data
        self.dur = audio_duration(audio)
        self.words = word_timings(data["guion"], self.dur)
        self.groups = build_caption_groups(self.words)
        self.scenes = build_scenes(data, self.words, self.dur, images_dir)
        self.overlay = make_overlay(self.ctx)
        self.label = data.get("etiqueta", "DEBATE DEL DÍA")
        self.header = render_header(self.ctx, self.label, 0)

    def bg(self, t):
        sc = scene_at(self.scenes, t)
        cur = photo_frame(self.ctx, sc, t).convert("RGBA")
        local = t - sc["s"]
        if sc["idx"] > 0 and local < 0.34:
            prev = self.scenes[sc["idx"] - 1]
            old = photo_frame(self.ctx, prev, min(t, prev["e"] - 1e-3)).convert("RGBA")
            p = ease_io(local / 0.34)
            kind = sc["idx"] % 3
            canvas = old
            if kind == 0:  # deslizamiento horizontal
                off = int(self.ctx.W * (1 - p))
                canvas = old.copy()
                canvas.paste(cur, (off, 0))
            elif kind == 1:  # fundido con zoom
                canvas = Image.blend(old, cur, p)
            else:  # deslizamiento vertical
                off = int(self.ctx.H * (1 - p))
                canvas = old.copy()
                canvas.paste(cur, (0, off))
            cur = canvas
            # destello blanco al corte
            fl = clamp(1 - local / 0.16) * 0.55
            if fl > 0:
                cur = Image.blend(cur, Image.new("RGBA", cur.size, (255, 255, 255, 255)), fl)
        return cur, sc

    def frame(self, t):
        ctx = self.ctx
        im, sc = self.bg(t)
        im.alpha_composite(self.overlay)
        im.alpha_composite(self.header)
        im.alpha_composite(render_progress(ctx, t / self.dur), (0, 0))
        nt = render_nametag(ctx, sc["label"], t - sc["s"]) if (sc["label"] and t < self.dur - 3.4) else None
        if nt is not None:
            im.alpha_composite(nt)
        hk = render_hook(ctx, self.data.get("gancho", ""), t)
        if hk is not None:
            im.alpha_composite(hk)
        if not (t < 2.9 and self.data.get("gancho")):
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
    ap.add_argument("--out", default="salida.mp4")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--stills", default="")
    ap.add_argument("--outdir", default="stills")
    ap.add_argument("--seconds", type=float, default=0)
    a = ap.parse_args()
    data = json.load(open(a.data, encoding="utf-8"))
    r = Renderer(data, a.audio, a.images, a.scale)
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
    print("listo", a.out)


if __name__ == "__main__":
    main()
