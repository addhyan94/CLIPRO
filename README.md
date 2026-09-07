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

Open:

http://127.0.0.1:8000

Use only URLs/content you are authorized to download. This starter does not implement DRM bypass or private-content authentication.
