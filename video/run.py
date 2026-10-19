#!/usr/bin/env python3
"""Orquestador: descarga audio + fotos libres (Wikipedia/Commons), renderiza y escribe resultados.

Uso (lo llama GitHub Actions):
  python run.py --data data.json --audio-url URL --workdir work
Escribe: work/salida.mp4 y work/info.json (créditos de las fotos).
"""
import argparse, html, json, os, re, shutil, subprocess, sys, urllib.parse, urllib.request

UA = "SegundoPalo/1.0 (canal de futbol; https://github.com/mariocoresotero10-alt/SEGUNDO-PALO)"
API = "https://en.wikipedia.org/w/api.php"


def http_get(url, binary=False, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = r.read()
                return d if binary else d.decode("utf-8")
        except Exception as e:  # noqa
            last = e
    print("  fallo al descargar", url[:90], last)
    return None


def api(params):
    q = urllib.parse.urlencode(params)
    raw = http_get(f"{API}?{q}")
    return json.loads(raw) if raw else {}


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def fetch_image(title, dest):
    """Foto principal de un artículo, SOLO si tiene licencia libre. Devuelve crédito o None."""
    d = api({"action": "query", "prop": "pageimages", "piprop": "thumbnail|name", "pithumbsize": 1280,
             "pilicense": "free", "redirects": 1, "format": "json", "formatversion": 2, "titles": title})
    pages = d.get("query", {}).get("pages", [])
    if not pages or "thumbnail" not in pages[0]:
        print("  sin foto libre:", title)
        return None
    pg = pages[0]
    url = pg["thumbnail"]["source"]
    data = http_get(url, binary=True)
    if not data:
        return None
    open(dest, "wb").write(data)
    credit = None
    fname = pg.get("pageimage")
    if fname:
        m = api({"action": "query", "prop": "imageinfo", "iiprop": "extmetadata", "format": "json",
                 "formatversion": 2, "titles": "File:" + fname})
        try:
            ext = m["query"]["pages"][0]["imageinfo"][0]["extmetadata"]
            artist = strip_html(ext.get("Artist", {}).get("value", "")) or "autor desconocido"
            lic = ext.get("LicenseShortName", {}).get("value", "licencia libre")
            credit = f"{title}: {artist} / Wikimedia Commons ({lic})"
        except Exception:
            credit = f"{title}: Wikimedia Commons"
    print("  foto ok:", title, "->", credit)
    return credit or f"{title}: Wikimedia Commons"



BAD = ("logo", "signature", "firma", "map", "flag", "bandera", "escudo", "crest", "badge", "stamp",
       "autograph", "kit", "shirt", "jersey", "svg", "poster", "cartel", "card", "figurine", "wax", "statue")


def commons_images(query, need=7, minw=1400, minh=1000, extra=()):
    """Fotos libres y grandes de Commons para una persona, mezclando varias busquedas (club, seleccion...)."""
    seen, out = set(), []
    qs = [query] + [f"{query} {e}" for e in extra]
    surname = query.split()[-1].lower()
    for q in qs:
        d = api_commons({"action": "query", "generator": "search", "gsrnamespace": 6, "gsrlimit": 30,
                         "gsrsearch": q + " filetype:bitmap", "prop": "imageinfo",
                         "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600, "format": "json",
                         "formatversion": 2})
        for pg in d.get("query", {}).get("pages", []):
            ii = (pg.get("imageinfo") or [{}])[0]
            title = pg.get("title", "").lower()
            url = ii.get("thumburl") or ii.get("url")
            if not url or url in seen or ii.get("mime") != "image/jpeg" or any(b in title for b in BAD):
                continue
            w, h = ii.get("width", 0), ii.get("height", 0)
            if w < minw or h < minh or surname not in title:
                continue
            ext = ii.get("extmetadata", {})
            lic = ext.get("LicenseShortName", {}).get("value", "")
            if not lic or "fair" in lic.lower() or "nc" in lic.lower().replace("nc-", "nc "):
                continue
            artist = strip_html(ext.get("Artist", {}).get("value", "")) or "autor desconocido"
            seen.add(url)
            out.append((w * h, url, f"{query}: {artist} / Wikimedia Commons ({lic})"))
    out.sort(key=lambda x: -x[0])
    return [(u, c) for _, u, c in out[:need]]


def commons_category_images(name, need=7, minw=1400, minh=1000):
    """Fotos de la categoria de Commons de la persona y de sus subcategorias recientes (club, seleccion)."""
    cat = "Category:" + name.replace(" ", "_")
    sub = api_commons({"action": "query", "list": "categorymembers", "cmtitle": cat, "cmtype": "subcat",
                       "cmlimit": 100, "format": "json", "formatversion": 2})
    subs = [m["title"] for m in sub.get("query", {}).get("categorymembers", [])]
    pref = ("paris saint-germain", "psg", "france", "spain", "barcelona", "2025", "2026", "2024", "champions league")
    cats = [t for t in subs if any(p in t.lower() for p in pref)]
    cats = sorted(cats, key=lambda t: 0 if ("paris" in t.lower() or "2025" in t or "2026" in t) else 1)[:6]
    out, seen = [], set()
    for c in cats + [cat]:
        d = api_commons({"action": "query", "generator": "categorymembers", "gcmtitle": c, "gcmtype": "file",
                         "gcmlimit": 60, "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
                         "iiurlwidth": 1600, "format": "json", "formatversion": 2})
        bonus = 1 if c != cat else 0
        for pg in d.get("query", {}).get("pages", []):
            ii = (pg.get("imageinfo") or [{}])[0]
            title = pg.get("title", "").lower()
            url = ii.get("thumburl") or ii.get("url")
            if not url or url in seen or ii.get("mime") != "image/jpeg" or any(b in title for b in BAD):
                continue
            w, h = ii.get("width", 0), ii.get("height", 0)
            if w < minw or h < minh:
                continue
            ext = ii.get("extmetadata", {})
            lic = ext.get("LicenseShortName", {}).get("value", "")
            if not lic or "fair" in lic.lower() or "nc" in lic.lower().replace("nc-", "nc "):
                continue
            artist = strip_html(ext.get("Artist", {}).get("value", "")) or "autor desconocido"
            seen.add(url)
            out.append((bonus * 10_000_000 + w * h, url, f"{name}: {artist} / Wikimedia Commons ({lic})"))
    out.sort(key=lambda x: -x[0])
    print("  categoria", cat, "->", len(out), "fotos candidatas;", len(cats), "subcategorias")
    return [(u, c) for _, u, c in out[:need]]


def slug(name):
    import unicodedata
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def own_photos(name):
    """Fotos propias subidas al repo en fotos/<nombre-del-jugador>/ (tienen prioridad sobre Wikimedia).
    Casa la carpeta aunque el nombre venga incompleto (p. ej. 'Dembele' o 'Lamine')."""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fotos")
    if not os.path.isdir(base):
        return []
    want = set(t for t in slug(name).split("-") if len(t) > 3)
    d = None
    for folder in sorted(os.listdir(base)):
        if not os.path.isdir(os.path.join(base, folder)):
            continue
        if folder == slug(name) or want & set(t for t in folder.split("-") if len(t) > 3):
            d = os.path.join(base, folder)
            break
    if not d:
        print("  sin carpeta de fotos propias para:", name)
        return []
    fs = sorted(f for f in os.listdir(d) if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
    print("  carpeta propia:", os.path.basename(d), len(fs), "fotos")
    return [os.path.join(d, f) for f in fs]


def api_commons(params):
    q = urllib.parse.urlencode(params)
    raw = http_get(f"https://commons.wikimedia.org/w/api.php?{q}")
    return json.loads(raw) if raw else {}


GOOD_TAGS = ("soccer", "football", "stadium", "goal", "penalty", "pitch", "ball")
BAD_TAGS = ("american", "rugby", "kid", "child", "children", "cartoon", "illustration", "animation", "drawing",
            "logo", "toy", "game", "video game", "gamer", "baby", "girl", "woman", "flag", "ads", "advert",
            "text", "abstract", "3d", "render", "green screen", "greenscreen", "green", "chroma", "isolated", "alpha", "transparent", "4k ball", "table", "foosball", "fussball", "basketball", "baseball", "tennis")


def blocked_ids():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "clips_bloqueados.txt")
    if not os.path.exists(p):
        return set()
    return {l.split("#")[0].strip() for l in open(p) if l.split("#")[0].strip()}


def pixabay_clips(dest, key, n=6):
    """Clips de futbol de Pixabay, solo los que pasan filtros estrictos de calidad y tema."""
    import random
    os.makedirs(dest, exist_ok=True)
    bad_ids = blocked_ids()
    qs = ["soccer match", "soccer stadium", "soccer ball", "soccer goal", "soccer fans", "soccer players", "football pitch"]
    cands = {}
    for q in qs:
        raw = http_get("https://pixabay.com/api/videos/?" + urllib.parse.urlencode(
            {"key": key, "q": q, "per_page": 30, "order": "popular", "safesearch": "true"}))
        if not raw:
            continue
        try:
            hits = json.loads(raw).get("hits", [])
        except Exception as e:  # noqa
            print("  pixabay:", e)
            continue
        for h in hits:
            hid = str(h.get("id"))
            tags = [t.strip().lower() for t in h.get("tags", "").split(",")]
            tagtxt = " ".join(tags)
            v = h.get("videos", {})
            pick = v.get("large") if (v.get("large") or {}).get("width", 0) >= 1280 else v.get("medium")
            if not pick or not pick.get("url") or pick.get("width", 0) < 1280:
                continue
            if hid in bad_ids or h.get("duration", 0) < 5 or pick.get("size", 0) > 45_000_000:
                continue
            if not any(g in tagtxt for g in ("soccer", "football")):
                continue
            if any(b in tags or (len(b) > 4 and b in tagtxt) for b in BAD_TAGS):
                continue
            if h.get("downloads", 0) < 300 and h.get("likes", 0) < 15:
                continue
            cands[hid] = (h.get("downloads", 0) + 20 * h.get("likes", 0), pick["url"])
    ranked = sorted(cands.items(), key=lambda kv: -kv[1][0])[:25]
    random.shuffle(ranked)
    got = 0
    for hid, (_, url) in ranked:
        if got >= n:
            break
        b = http_get(url, binary=True)
        if b and len(b) > 200_000:
            open(os.path.join(dest, f"pix_{hid}.mp4"), "wb").write(b)
            got += 1
    print("  clips:", got, "de", len(cands), "candidatos")
    return got


def contact_sheet(clips_dir, out_png):
    """Una imagen con un fotograma de cada clip, para revisar que son buenos."""
    from PIL import Image, ImageDraw
    fr = []
    for f in sorted(os.listdir(clips_dir)):
        p = os.path.join(clips_dir, f)
        t = os.path.join(clips_dir, f + ".jpg")
        subprocess.call(["ffmpeg", "-v", "error", "-y", "-ss", "2", "-i", p, "-frames:v", "1", "-vf", "scale=480:-1", t])
        if os.path.exists(t):
            fr.append((f, Image.open(t)))
    if not fr:
        return
    cols = 3
    rows = (len(fr) + cols - 1) // cols
    cw, ch = 480, max(i.height for _, i in fr) + 24
    sheet = Image.new("RGB", (cols * cw, rows * ch), (20, 20, 20))
    d = ImageDraw.Draw(sheet)
    for k, (name, im) in enumerate(fr):
        x, y = (k % cols) * cw, (k // cols) * ch
        sheet.paste(im, (x, y + 24))
        d.text((x + 6, y + 6), name, fill=(255, 255, 255))
    sheet.save(out_png)
    for f in os.listdir(clips_dir):
        if f.endswith(".jpg"):
            os.remove(os.path.join(clips_dir, f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--audio-url", default="")
    ap.add_argument("--audio", default="")
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--scale", default="1.0")
    ap.add_argument("--skip-fetch", action="store_true")
    a = ap.parse_args()

    data = json.load(open(a.data, encoding="utf-8"))
    wd = a.workdir
    imgs = os.path.join(wd, "imgs")
    os.makedirs(imgs, exist_ok=True)

    audio = a.audio or os.path.join(wd, "voz.mp3")
    if a.audio_url:
        print("descargando audio")
        b = http_get(a.audio_url, binary=True)
        if not b or len(b) < 2000:
            sys.exit("no se pudo descargar el audio")
        open(audio, "wb").write(b)

    credits = []
    if not a.skip_fetch:
        subjects = [(f"p{i+1}", j) for i, j in enumerate(data.get("jugadores", [])[:3])]
        for key, title in subjects:
            name = re.sub(r"\s*\(.*?\)", "", title).strip()
            try:
                got = commons_category_images(name, need=7)
            except Exception as e:  # noqa
                print('  categoria fallo:', e)
                got = []
            if len(got) < 4:
                got += [g for g in commons_images(name, need=7, extra=("Paris Saint-Germain", "Barcelona", "2025", "2026", "Spain", "France")) if g not in got][: 7 - len(got)]
            if len(got) < 2:
                got = commons_images(name, need=7, minw=1000, minh=700, extra=("Paris Saint-Germain", "Barcelona", "2025", "Spain", "France"))
            n = 0
            from PIL import Image as _I
            cand = []
            for pth in own_photos(name):
                try:
                    im0 = _I.open(pth).convert('RGB')
                    if im0.width < 450:
                        print('  foto propia demasiado pequena, se descarta:', os.path.basename(pth), im0.size)
                        continue
                    cand.append((im0.width * im0.height, pth, im0))
                except Exception as e:  # noqa
                    print('  foto propia ilegible:', pth, e)
            cand.sort(key=lambda c: -c[0])
            for _, pth, im0 in cand[:7]:  # solo las mejores (mas resolucion)
                im0.save(os.path.join(imgs, f'{key}_{n+1}.jpg'), quality=95)
                n += 1
                print('  foto propia usada:', os.path.basename(pth), im0.size)
            if n:
                print('  fotos propias:', n)
            if n >= 3:
                got = []  # con 3+ fotos propias no se mezcla nada de Wikimedia
            for url, cr in got[: max(0, 7 - n)]:
                b = http_get(url, binary=True)
                if b and len(b) > 20000:
                    n += 1
                    open(os.path.join(imgs, f"{key}_{n}.jpg"), "wb").write(b)
                    credits.append(cr)
            if n == 0:  # plan B: foto principal de la Wikipedia
                c = fetch_image(title.strip(), os.path.join(imgs, f"{key}_1.jpg"))
                if c:
                    credits.append(c)
            print("  ", name, "->", n, "fotos")
        est = data.get("estadio", "").strip()
        if est:
            c = fetch_image(est, os.path.join(imgs, "estadio_1.jpg"))
            if c:
                credits.append(c)

    clips_dir = os.path.join(wd, "clips")
    os.makedirs(clips_dir, exist_ok=True)
    repo_clips = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "clips")
    if os.path.isdir(repo_clips):
        for f in os.listdir(repo_clips):
            if f.lower().endswith((".mp4", ".mov", ".webm")):
                shutil.copy(os.path.join(repo_clips, f), clips_dir)
    pk = os.environ.get("PIXABAY_KEY", "")
    if pk and os.environ.get("USAR_STOCK") == "1" and not os.listdir(clips_dir) and not a.skip_fetch:
        if pixabay_clips(clips_dir, pk):
            credits.append("Clips de video: Pixabay (Pixabay Content License)")
            contact_sheet(clips_dir, os.path.join(wd, "clips.jpg"))

    out = os.path.join(wd, "salida.mp4")
    here = os.path.dirname(os.path.abspath(__file__))
    rc = subprocess.call([sys.executable, os.path.join(here, "render.py"), "--data", a.data, "--audio", audio,
                          "--images", imgs, "--clips", clips_dir, "--out", out, "--scale", str(a.scale)])
    if rc != 0:
        sys.exit("fallo el render")
    json.dump({"credits": credits}, open(os.path.join(wd, "info.json"), "w"), ensure_ascii=False)
    print("OK", out)


if __name__ == "__main__":
    main()
