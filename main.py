from pathlib import Path
import asyncio
import os
import re
import shutil
import tempfile
import threading
import uuid
import zipfile

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
JOB_ROOT = Path(os.getenv("CLIPRO_JOB_ROOT", tempfile.gettempdir())) / "clipro_jobs"
JOB_ROOT.mkdir(parents=True, exist_ok=True)
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
DENO = shutil.which("deno") or ("/root/.deno/bin/deno" if Path("/root/.deno/bin/deno").exists() else None)
POT_PROVIDER_URL = os.getenv("CLIPRO_POT_PROVIDER_URL", "http://127.0.0.1:4416")
MAX_JOBS = max(1, int(os.getenv("CLIPRO_MAX_JOBS", "2")))

app = FastAPI(title="CLIPRO")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

jobs = {}
jobs_lock = threading.Lock()
job_semaphore = threading.Semaphore(MAX_JOBS)


class AnalyzeRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality: str
    file_type: str


def valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://", url.strip(), re.I))


def ytdlp_base_options() -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "retries": 10,
        "fragment_retries": 20,
        "file_access_retries": 10,
        "continuedl": True,
        "concurrent_fragment_downloads": 1,
        "socket_timeout": 30,
        "extractor_retries": 5,
        "remote_components": ["ejs:github"],
        "extractor_args": {
            "youtubepot-bgutilhttp": {"base_url": POT_PROVIDER_URL}
        },
    }
    if DENO:
        options["js_runtimes"] = {"deno": {"path": DENO}}
    return options


def extract_info(url: str, **overrides):
    options = ytdlp_base_options()
    options.update(overrides)
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


def safe_word(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", value or "")
    value = re.sub(r"\s+", " ", value).strip().strip(".")
    value = re.sub(r"[^\w\-]+", "", value, flags=re.UNICODE)
    return value[:32]


def clipro_filename(title: str, extension: str) -> str:
    raw_words = re.findall(r"\S+", title or "download")
    words = [safe_word(word) for word in raw_words]
    words = [word for word in words if word]
    fillers = ["Video", "Media", "Download"]
    while len(words) < 3:
        filler = fillers[min(len(words), len(fillers) - 1)]
        if filler not in words:
            words.append(filler)
        else:
            words.append("File")
    stem = "_".join(words[:3])
    name = f"CLIPRO_{stem}.{extension.lower().lstrip('.') }"
    while len(name.encode("utf-8")) > 120:
        parts = name.rsplit(".", 1)
        stem = stem[:-1]
        name = f"CLIPRO_{stem}.{parts[1]}"
    return name


def safe_folder_name(title: str) -> str:
    words = re.findall(r"\S+", title or "Playlist")
    cleaned = [safe_word(word) for word in words]
    cleaned = [word for word in cleaned if word]
    return "_".join(cleaned[:8])[:100] or "Playlist"


def update_job(job_id: str, **values):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(values)


def get_job(job_id: str):
    with jobs_lock:
        value = jobs.get(job_id)
        return dict(value) if value else None


def bytes_value(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def progress_hook(job_id: str):
    def hook(data):
        state = data.get("status")
        info = data.get("info_dict") or {}
        index = info.get("playlist_index") or data.get("playlist_index")
        if state == "downloading":
            total = bytes_value(data.get("total_bytes") or data.get("total_bytes_estimate"))
            downloaded = bytes_value(data.get("downloaded_bytes"))
            percent = (downloaded / total * 100) if total else 0
            update_job(
                job_id,
                status="downloading",
                percent=round(min(99, max(0, percent)), 1),
                downloaded=downloaded,
                total=total,
                speed=bytes_value(data.get("speed")),
                eta=data.get("eta"),
                current_index=index,
                current_title=info.get("title") or "Downloading...",
                message="Downloading video/audio...",
            )
            if index:
                update_playlist_item(
                    job_id,
                    int(index),
                    status="downloading",
                    percent=round(min(99, max(0, percent)), 1),
                    size=downloaded,
                    message="Downloading...",
                    title=info.get("title") or f"Video {index}",
                    thumbnail=info.get("thumbnail"),
                )
        elif state == "finished":
            update_job(
                job_id,
                current_index=index,
                current_title=info.get("title") or "Processing...",
                message="Download received. Processing audio/video...",
            )
            if index:
                update_playlist_item(job_id, int(index), status="processing", percent=99, message="Processing...")
    return hook


def choose_format(quality: str, file_type: str) -> str:
    if file_type == "mp3":
        return "bestaudio[acodec!=none]/best"
    if quality == "best":
        return "bestvideo[vcodec!=none]+bestaudio[acodec!=none]/best[vcodec!=none]"
    height = int(quality.rstrip("p"))
    return f"bestvideo[height<={height}][vcodec!=none]+bestaudio[acodec!=none]/best[height<={height}][vcodec!=none]"


def media_files(folder: Path):
    allowed = {".mp3", ".mp4", ".mkv", ".webm", ".m4a", ".opus", ".aac", ".flac", ".mov", ".avi"}
    return [
        path
        for path in folder.rglob("*")
        if path.is_file()
        and not path.name.endswith((".part", ".ytdl"))
        and path.suffix.lower() in allowed
    ]


def find_downloaded_file(folder: Path, preferred_extension: str | None = None):
    files = media_files(folder)
    if preferred_extension:
        preferred = [p for p in files if p.suffix.lower() == f".{preferred_extension.lower()}"]
        if preferred:
            return max(preferred, key=lambda p: p.stat().st_size)
    return max(files, key=lambda p: p.stat().st_size) if files else None


def estimate_size(info: dict, quality: str, file_type: str) -> int:
    formats = info.get("formats") or []
    if file_type == "mp3":
        sizes = [bytes_value(f.get("filesize") or f.get("filesize_approx")) for f in formats if f.get("vcodec") == "none"]
        return max(sizes or [0])
    videos = [f for f in formats if f.get("vcodec") not in (None, "none")]
    audios = [f for f in formats if f.get("acodec") not in (None, "none")]
    if quality != "best":
        limit = int(quality.rstrip("p"))
        videos = [f for f in videos if not f.get("height") or int(f.get("height") or 0) <= limit]
    if not videos:
        return 0
    max_height = max(int(f.get("height") or 0) for f in videos)
    top_videos = [f for f in videos if int(f.get("height") or 0) == max_height]
    video_size = max([bytes_value(f.get("filesize") or f.get("filesize_approx")) for f in top_videos] or [0])
    audio_size = max([bytes_value(f.get("filesize") or f.get("filesize_approx")) for f in audios] or [0])
    return video_size + audio_size


def disk_preflight(job_dir: Path, estimated_size: int):
    if estimated_size <= 0:
        return
    free = shutil.disk_usage(job_dir).free
    required = int(estimated_size * 1.75) + 512 * 1024 * 1024
    if free < required:
        free_gb = free / (1024 ** 3)
        required_gb = required / (1024 ** 3)
        raise RuntimeError(f"Not enough temporary disk space. Available: {free_gb:.1f} GB, required: about {required_gb:.1f} GB.")


def make_item_from_info(entry, index):
    return {
        "index": index,
        "title": entry.get("title") or f"Video {index}",
        "thumbnail": entry.get("thumbnail"),
        "status": "queued",
        "percent": 0,
        "size": 0,
        "filename": None,
        "message": "Queued",
    }


def update_playlist_item(job_id: str, index: int, **values):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or not isinstance(job.get("items"), list):
            return
        for item in job["items"]:
            if item.get("index") == index:
                item.update(values)
                break
        completed = sum(1 for item in job["items"] if item.get("status") == "complete")
        failed = sum(1 for item in job["items"] if item.get("status") == "error")
        current = next((item for item in job["items"] if item.get("status") in {"downloading", "processing"}), None)
        current_percent = float(current.get("percent", 0)) if current else 0
        total = len(job["items"])
        job["completed_count"] = completed
        job["failed_count"] = failed
        job["total_count"] = total
        if total:
            job["percent"] = round(min(99, ((completed + current_percent / 100) / total) * 100), 1)


def prepare_playlist(url: str):
    info = extract_info(url, skip_download=True, extract_flat="in_playlist", noplaylist=False)
    entries = [entry for entry in (info.get("entries") or []) if entry]
    items = []
    for index, entry in enumerate(entries, 1):
        item = make_item_from_info(entry, index)
        if not item["thumbnail"] and entry.get("thumbnails"):
            item["thumbnail"] = entry["thumbnails"][-1].get("url")
        items.append(item)
    return info, items


def download_single(job_id: str, url: str, quality: str, file_type: str, job_dir: Path):
    info = extract_info(url, skip_download=True, noplaylist=True)
    title = info.get("title") or "download"
    estimated = estimate_size(info, quality, file_type)
    disk_preflight(job_dir, estimated)
    output = job_dir / f"{job_id}.%(ext)s"
    options = ytdlp_base_options()
    options.update({
        "format": choose_format(quality, file_type),
        "outtmpl": str(output),
        "noplaylist": True,
        "progress_hooks": [progress_hook(job_id)],
        "ffmpeg_location": FFMPEG,
        "merge_output_format": file_type if file_type in {"mp4", "mkv"} else None,
        "postprocessors": [],
    })
    if file_type == "mp3":
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    update_job(job_id, title=title, thumbnail=info.get("thumbnail"), uploader=info.get("uploader") or info.get("channel"), status="downloading", percent=0, message="Downloading video/audio...")
    with yt_dlp.YoutubeDL(options) as ydl:
        result = ydl.download([url])
    if result not in (None, 0):
        raise RuntimeError(f"yt-dlp exited with code {result}.")
    source = find_downloaded_file(job_dir, file_type)
    if not source:
        raise RuntimeError("Download finished but output file was not found.")
    final_name = clipro_filename(title, file_type)
    final_path = job_dir / final_name
    source.replace(final_path) if source.resolve() != final_path.resolve() else None
    size = final_path.stat().st_size
    update_job(job_id, status="complete", percent=100, message="Download complete.", file=str(final_path), filename=final_path.name, total=size, downloaded=size, speed=0, eta=0)


def download_playlist(job_id: str, url: str, quality: str, file_type: str, job_dir: Path):
    update_job(job_id, status="preparing", percent=0, message="Reading playlist...")
    playlist_info, items = prepare_playlist(url)
    if not items:
        raise RuntimeError("No downloadable videos were found in this playlist.")
    playlist_dir = job_dir / safe_folder_name(playlist_info.get("title") or "Playlist")
    playlist_dir.mkdir(parents=True, exist_ok=True)
    update_job(job_id, kind="playlist", playlist_title=playlist_info.get("title") or "Playlist", items=items, total_count=len(items), completed_count=0, failed_count=0, status="downloading", percent=0, message=f"Playlist found: {len(items)} videos.")
    options = ytdlp_base_options()
    options.update({
        "format": choose_format(quality, file_type),
        "outtmpl": str(playlist_dir / "%(playlist_index)03d.%(id)s.%(ext)s"),
        "noplaylist": False,
        "progress_hooks": [progress_hook(job_id)],
        "ffmpeg_location": FFMPEG,
        "merge_output_format": file_type if file_type in {"mp4", "mkv"} else None,
        "ignoreerrors": True,
        "postprocessors": [],
    })
    if file_type == "mp3":
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])
    downloaded = media_files(playlist_dir)
    for item in items:
        index = item["index"]
        prefix = f"{index:03d}."
        candidates = [p for p in downloaded if p.name.startswith(prefix)]
        if not candidates:
            update_playlist_item(job_id, index, status="error", percent=0, message="Could not download this video.")
            continue
        source = max(candidates, key=lambda p: p.stat().st_size)
        title = item.get("title") or f"Video {index}"
        final_name = f"{index:03d}_{clipro_filename(title, file_type)}"
        final_path = playlist_dir / final_name
        source.replace(final_path) if source.resolve() != final_path.resolve() else None
        update_playlist_item(job_id, index, status="complete", percent=100, size=final_path.stat().st_size, filename=final_path.name, message="Completed")
    job = get_job(job_id) or {}
    completed = job.get("completed_count", 0)
    failed = job.get("failed_count", 0)
    if completed == 0:
        raise RuntimeError("No videos could be downloaded from this playlist.")
    zip_path = job_dir / f"{clipro_filename(playlist_info.get('title') or 'Playlist', 'zip')}"
    update_job(job_id, status="zipping", percent=99, message="Creating ZIP file...", current_title="Creating ZIP...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in playlist_dir.rglob("*"):
            if path.is_file():
                archive.write(path, arcname=path.relative_to(playlist_dir))
    size = zip_path.stat().st_size
    message = f"Playlist complete: {completed}/{len(items)} downloaded."
    if failed:
        message = f"Playlist complete: {completed} downloaded, {failed} failed."
    update_job(job_id, status="complete", percent=100, message=message, file=str(zip_path), filename=zip_path.name, total=size, downloaded=size, speed=0, eta=0)


def run_download(job_id: str, url: str, quality: str, file_type: str):
    job_dir = None
    acquired = False
    try:
        if not FFMPEG:
            raise RuntimeError("FFmpeg is not installed.")
        job_semaphore.acquire()
        acquired = True
        job_dir = Path(tempfile.mkdtemp(prefix=f"media_{job_id}_", dir=JOB_ROOT))
        update_job(job_id, status="starting", percent=0, message="Reading URL...")
        probe = extract_info(url, skip_download=True, extract_flat="in_playlist", noplaylist=False)
        entries = [entry for entry in (probe.get("entries") or []) if entry]
        if len(entries) > 1:
            download_playlist(job_id, url, quality, file_type, job_dir)
        else:
            update_job(job_id, kind="single")
            download_single(job_id, url, quality, file_type, job_dir)
    except Exception as exc:
        if job_dir and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        message = str(exc).strip() or exc.__class__.__name__
        update_job(job_id, status="error", percent=0, message=message)
    finally:
        if acquired:
            job_semaphore.release()


def cleanup_download(path: Path):
    try:
        parent = path.parent
        if path.exists():
            path.unlink()
        if parent.exists():
            shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass


def cleanup_stale_jobs():
    for path in JOB_ROOT.glob("media_*"):
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass


@app.on_event("startup")
async def startup():
    cleanup_stale_jobs()


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "ffmpeg": bool(FFMPEG),
        "ffprobe": bool(FFPROBE),
        "deno": bool(DENO),
        "pot_provider": POT_PROVIDER_URL,
        "yt_dlp": yt_dlp.version.__version__,
    }


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    url = req.url.strip()
    if not valid_url(url):
        raise HTTPException(400, "Please enter a valid http/https URL.")
    try:
        info = await asyncio.to_thread(extract_info, url, skip_download=True, noplaylist=False)
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if entries:
            first = entries[0]
            return {
                "kind": "playlist",
                "title": info.get("title", "Playlist"),
                "uploader": info.get("uploader") or info.get("channel") or "Unknown",
                "source": info.get("webpage_url_domain") or info.get("extractor_key") or "Unknown",
                "duration": None,
                "thumbnail": first.get("thumbnail") or info.get("thumbnail"),
                "playlist_count": len(entries),
                "qualities": [],
            }
        formats = info.get("formats") or []
        heights = sorted({int(f["height"]) for f in formats if f.get("height") and f.get("vcodec") != "none"})
        return {
            "kind": "single",
            "title": info.get("title", "Unknown"),
            "uploader": info.get("uploader") or info.get("channel") or "Unknown",
            "source": info.get("webpage_url_domain") or info.get("extractor_key") or "Unknown",
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "qualities": heights,
        }
    except Exception as exc:
        raise HTTPException(400, f"Could not analyze URL: {exc}")


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    url = req.url.strip()
    file_type = req.file_type.lower().strip()
    quality = req.quality.lower().strip()
    if not valid_url(url):
        raise HTTPException(400, "Invalid URL.")
    if file_type not in {"mp3", "mp4", "mkv"}:
        raise HTTPException(400, "Unsupported file type.")
    if quality != "best":
        try:
            height = int(quality.rstrip("p"))
            if height <= 0:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "Invalid quality.")
    job_id = uuid.uuid4().hex
    update_job(job_id, kind="detecting", status="queued", percent=0, message="Queued...", items=[], total_count=0, completed_count=0, failed_count=0)
    asyncio.create_task(asyncio.to_thread(run_download, job_id, url, quality, file_type))
    return {"job_id": job_id}


@app.get("/api/progress/{job_id}")
def progress(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Download job not found.")
    return job


@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    job = get_job(job_id)
    if not job or job.get("status") != "complete":
        raise HTTPException(404, "File is not ready yet.")
    path = Path(job["file"])
    if not path.exists() or JOB_ROOT not in path.parents:
        raise HTTPException(404, "File not found.")
    media_type = "application/zip" if path.suffix.lower() == ".zip" else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=job["filename"], background=BackgroundTask(cleanup_download, path))
