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
        todo = [("img_estadio.jpg", data.get("estadio", ""))]
        for i, j in enumerate(data.get("jugadores", [])[:3]):
            todo.append((f"img_{i+1}.jpg", j))
        for fn, title in todo:
            if title:
                c = fetch_image(title.strip(), os.path.join(imgs, fn))
                if c:
                    credits.append(c)

    out = os.path.join(wd, "salida.mp4")
    here = os.path.dirname(os.path.abspath(__file__))
    rc = subprocess.call([sys.executable, os.path.join(here, "render.py"), "--data", a.data, "--audio", audio,
                          "--images", imgs, "--out", out, "--scale", str(a.scale)])
    if rc != 0:
        sys.exit("fallo el render")
    json.dump({"credits": credits}, open(os.path.join(wd, "info.json"), "w"), ensure_ascii=False)
    print("OK", out)


if __name__ == "__main__":
    main()
