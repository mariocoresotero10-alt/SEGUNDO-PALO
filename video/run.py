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


def commons_images(query, need=4, minw=1400, minh=1000):
    """Varias fotos libres y grandes de Commons para una busqueda. Devuelve [(url, credito)]."""
    d = api_commons({"action": "query", "generator": "search", "gsrnamespace": 6, "gsrlimit": 30,
                     "gsrsearch": query + " filetype:bitmap", "prop": "imageinfo",
                     "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600, "format": "json",
                     "formatversion": 2})
    pages = d.get("query", {}).get("pages", [])
    pages.sort(key=lambda p: p.get("index", 99))
    surname = query.split()[-1].lower()
    out = []
    for pg in pages:
        ii = (pg.get("imageinfo") or [{}])[0]
        title = pg.get("title", "").lower()
        if ii.get("mime") != "image/jpeg" or any(b in title for b in BAD):
            continue
        w, h = ii.get("width", 0), ii.get("height", 0)
        if w < minw or h < minh or surname not in title:
            continue
        ext = ii.get("extmetadata", {})
        lic = ext.get("LicenseShortName", {}).get("value", "")
        if not lic or "fair" in lic.lower() or "nc" in lic.lower().replace("nc-", "nc "):
            continue
        artist = strip_html(ext.get("Artist", {}).get("value", "")) or "autor desconocido"
        out.append((ii.get("thumburl") or ii.get("url"), f"{query}: {artist} / Wikimedia Commons ({lic})"))
        if len(out) >= need:
            break
    return out


def api_commons(params):
    q = urllib.parse.urlencode(params)
    raw = http_get(f"https://commons.wikimedia.org/w/api.php?{q}")
    return json.loads(raw) if raw else {}


def pixabay_clips(dest, key, n=6):
    """Descarga n clips de futbol libres de derechos de Pixabay. Devuelve cuantos ha bajado."""
    import random
    os.makedirs(dest, exist_ok=True)
    qs = ["soccer", "football stadium", "soccer ball", "soccer goal", "soccer fans", "soccer match", "soccer training"]
    cands = []
    for q in qs:
        raw = http_get("https://pixabay.com/api/videos/?" + urllib.parse.urlencode(
            {"key": key, "q": q, "per_page": 20, "safesearch": "true"}))
        if not raw:
            continue
        try:
            for h in json.loads(raw).get("hits", []):
                v = h.get("videos", {})
                pick = v.get("medium") or v.get("large") or v.get("small")
                if pick and pick.get("url") and h.get("duration", 0) >= 4 and pick.get("size", 0) < 40_000_000:
                    cands.append((h["id"], pick["url"]))
        except Exception as e:  # noqa
            print("  pixabay:", e)
    seen, uniq = set(), []
    for i, u in cands:
        if i not in seen:
            seen.add(i)
            uniq.append((i, u))
    random.shuffle(uniq)
    got = 0
    for i, u in uniq:
        if got >= n:
            break
        b = http_get(u, binary=True)
        if b and len(b) > 100_000:
            open(os.path.join(dest, f"pix_{i}.mp4"), "wb").write(b)
            got += 1
    print("  clips:", got)
    return got


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
            got = commons_images(name, need=4)
            if len(got) < 2:
                got += commons_images(name, need=4, minw=900, minh=700)[: 4 - len(got)]
            n = 0
            for url, cr in got:
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
    if pk and not os.listdir(clips_dir) and not a.skip_fetch:
        if pixabay_clips(clips_dir, pk):
            credits.append("Clips de video: Pixabay (Pixabay Content License)")

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
