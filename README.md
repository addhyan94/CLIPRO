# CLIPRO
A fast and simple media downloader built with FastAPI, yt-dlp and FFmpeg, supporting video, audio, quality selection, and playlist downloads with ZIP export.

# Media Downloader

Local FastAPI + yt-dlp + FFmpeg based media downloader.

## Requirements

- Python 3.10+
- FFmpeg installed and available in PATH

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```
```bash
docker build -t clipro-test .
docker run --rm -p 8000:8000 clipro-test


docker ps
docker exec -it <CONTAINER_ID> bash
python --version
yt-dlp --version
deno --version
ffmpeg -version
ffprobe -version
python -c "import yt_dlp; print('yt-dlp OK')"
python -c "import yt_dlp_ejs; print('yt-dlp-ejs OK')"
```
http://127.0.0.1:8000/healthz

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload

df -h
```

Open: https://clipro.onrender.com/

Use only URLs/content you are authorized to download. This starter does not implement DRM bypass or private-content authentication.
