# Segundo Palo - generador de videos

Renderiza los Shorts de Segundo Palo (gancho, subtitulos palabra a palabra, fotos libres de Wikipedia con zoom,
transiciones y cierre) sin servicios de pago. Make lanza el workflow `Render video` y recibe el video por webhook.

- `video/render.py`: renderizador (PIL + ffmpeg)
- `video/run.py`: descarga audio y fotos libres, renderiza y guarda los creditos
- `.github/workflows/render.yml`: ejecucion en GitHub Actions

Fuente tipografica: Poppins Bold (SIL Open Font License).
