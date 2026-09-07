from pathlib import Path
import asyncio
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
STATIC_DIR = BASE / "static"

# yt-dlp uses an external JS runtime for modern YouTube extraction.
# Works both locally and inside the Docker image.
DENO = shutil.which("deno")
if not DENO:
    candidate = Path("/root/.deno/bin/deno")
    if candidate.exists():
        DENO = str(candidate)

FFMPEG = shutil.which("ffmpeg")

app = FastAPI(title="CLIPRO", version="1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# This is intentionally in-memory for the single-process deployment.
# For multi-instance production, move jobs to Redis/Key Value + a worker.
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


class AnalyzeRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality: str
    file_type: str


def valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://", url.strip(), re.IGNORECASE))


def clean_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "download")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:180] or "download"


def ytdlp_base_options() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    if DENO:
        opts["js_runtimes"] = {"deno": {"path": DENO}}

    return opts


def ytdlp_download_options() -> dict:
    opts = ytdlp_base_options()
    opts.update(
        {
            "continuedl": True,
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 3,
            "concurrent_fragment_downloads": 4,
            "ffmpeg_location": FFMPEG,
        }
    )
    return opts


def get_info(url: str):
    opts = ytdlp_base_options()
    opts.update(
        {
            "skip_download": True,
            "noplaylist": False,
        }
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def update_job(job_id: str, **values):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(values)


def get_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        return dict(job) if job else None


def format_bytes(value) -> int:
    return int(value or 0)


def make_item_from_info(entry, index: int):
    title = clean_filename(entry.get("title") or f"Video {index}")
    thumbnail = entry.get("thumbnail")

    if not thumbnail and entry.get("thumbnails"):
        thumbnail = entry["thumbnails"][-1].get("url")

    return {
        "index": index,
        "title": title,
        "thumbnail": thumbnail,
        "status": "queued",
        "percent": 0,
        "size": 0,
        "filename": None,
        "message": "Queued",
    }


def progress_hook(job_id: str):
    def hook(data):
        state = data.get("status")
        info = data.get("info_dict") or {}
        playlist_index = info.get("playlist_index") or data.get("playlist_index")

        if state == "downloading":
            total = (
                data.get("total_bytes")
                or data.get("total_bytes_estimate")
                or 0
            )
            downloaded = data.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else 0

            update_job(
                job_id,
                status="downloading",
                percent=max(0, min(99, percent)),
                downloaded=downloaded,
                total=total,
                speed=data.get("speed") or 0,
                eta=data.get("eta"),
                current_title=info.get("title") or "Downloading...",
                current_index=playlist_index,
                message="Downloading video/audio...",
            )

        elif state == "finished":
            update_job(
                job_id,
                current_index=playlist_index,
                current_title=info.get("title") or "Processing...",
                message="Download received. Processing audio/video...",
            )

    return hook


def choose_format(quality: str, file_type: str) -> str:
    if file_type == "mp3":
        return "bestaudio[acodec!=none]/best"

    if quality == "best":
        video = "bestvideo[vcodec!=none]"
        fallback = "best[vcodec!=none]"
    else:
        height = int(quality.rstrip("p"))
        video = f"bestvideo[height<={height}][vcodec!=none]"
        fallback = f"best[height<={height}][vcodec!=none]"

    return f"{video}+bestaudio[acodec!=none]/{fallback}"


def find_downloaded_file(folder: Path):
    ignored = {
        ".json",
        ".description",
        ".jpg",
        ".jpeg",
        ".webp",
        ".png",
    }

    files = [
        p
        for p in folder.rglob("*")
        if p.is_file()
        and not p.name.endswith((".part", ".ytdl"))
        and p.suffix.lower() not in ignored
    ]

    return max(files, key=lambda p: p.stat().st_size) if files else None


def update_playlist_item(job_id: str, index: int, **values):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return

        items = job.get("items")
        if not isinstance(items, list):
            return

        for item in items:
            if item.get("index") == index:
                item.update(values)
                break

        completed = sum(item.get("status") == "complete" for item in items)
        failed = sum(item.get("status") == "error" for item in items)
        current = next(
            (item for item in items if item.get("status") in {"downloading", "processing"}),
            None,
        )

        job["completed_count"] = completed
        job["failed_count"] = failed
        job["total_count"] = len(items)

        if items:
            current_percent = float(current.get("percent", 0)) if current else 0
            job["percent"] = round(
                min(99, ((completed + current_percent / 100) / len(items)) * 100),
                1,
            )


def playlist_progress_hook(job_id: str):
    def hook(data):
        state = data.get("status")
        info = data.get("info_dict") or {}
        index = info.get("playlist_index") or data.get("playlist_index")

        if not index:
            return

        title = clean_filename(info.get("title") or f"Video {index}")

        if state == "downloading":
            total = (
                data.get("total_bytes")
                or data.get("total_bytes_estimate")
                or 0
            )
            downloaded = data.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else 0

            update_playlist_item(
                job_id,
                index,
                title=title,
                thumbnail=info.get("thumbnail"),
                status="downloading",
                percent=round(max(0, min(99, percent)), 1),
                size=format_bytes(downloaded),
                message="Downloading...",
            )

            update_job(
                job_id,
                current_index=index,
                current_title=title,
                downloaded=downloaded,
                total=total,
                speed=data.get("speed") or 0,
                eta=data.get("eta"),
                message=f"Downloading {index}...",
            )

        elif state == "finished":
            update_playlist_item(
                job_id,
                index,
                title=title,
                thumbnail=info.get("thumbnail"),
                status="processing",
                percent=99,
                message="Processing...",
            )

    return hook


def prepare_playlist(url: str):
    opts = ytdlp_base_options()
    opts.update(
        {
            "skip_download": True,
            "extract_flat": "in_playlist",
            "noplaylist": False,
        }
    )

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = list(info.get("entries") or [])
    items = []

    for index, entry in enumerate(entries, start=1):
        if entry:
            items.append(make_item_from_info(entry, index))

    return info, items


def download_single(
    job_id: str,
    url: str,
    quality: str,
    file_type: str,
    job_dir: Path,
):
    probe_opts = ytdlp_base_options()
    probe_opts["noplaylist"] = True

    with yt_dlp.YoutubeDL(probe_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = clean_filename(info.get("title", "download"))
    base_output = job_dir / f"{job_id}.%(ext)s"

    opts = ytdlp_download_options()
    opts.update(
        {
            "format": choose_format(quality, file_type),
            "outtmpl": str(base_output),
            "noplaylist": True,
            "progress_hooks": [progress_hook(job_id)],
            "merge_output_format": file_type if file_type in {"mp4", "mkv"} else None,
        }
    )

    if file_type == "mp3":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    update_job(
        job_id,
        title=title,
        thumbnail=info.get("thumbnail"),
        uploader=info.get("uploader") or info.get("channel") or "Unknown",
        status="downloading",
        percent=0,
        message="Downloading video/audio...",
    )

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    source = find_downloaded_file(job_dir)
    if not source:
        raise RuntimeError("Download finished but the output file was not found.")

    final_path = job_dir / f"{title}.{file_type}"

    if source.resolve() != final_path.resolve():
        if final_path.exists():
            final_path = job_dir / f"{title}_{job_id[:6]}.{file_type}"
        source.replace(final_path)

    file_size = final_path.stat().st_size

    update_job(
        job_id,
        status="complete",
        percent=100,
        message="Download complete.",
        file=str(final_path),
        filename=final_path.name,
        total=file_size,
        downloaded=file_size,
        speed=0,
        eta=0,
    )


def download_playlist(
    job_id: str,
    url: str,
    quality: str,
    file_type: str,
    job_dir: Path,
):
    update_job(
        job_id,
        status="preparing",
        percent=0,
        message="Reading playlist...",
    )

    playlist_info, items = prepare_playlist(url)

    if not items:
        raise RuntimeError("No downloadable videos were found in this playlist.")

    playlist_title = clean_filename(playlist_info.get("title") or "Playlist")

    update_job(
        job_id,
        kind="playlist",
        playlist_title=playlist_title,
        items=items,
        total_count=len(items),
        completed_count=0,
        failed_count=0,
        status="downloading",
        percent=0,
        message=f"Playlist found: {len(items)} videos.",
    )

    playlist_dir = job_dir / playlist_title
    playlist_dir.mkdir(parents=True, exist_ok=True)

    opts = ytdlp_download_options()
    opts.update(
        {
            "format": choose_format(quality, file_type),
            "outtmpl": str(playlist_dir / "%(playlist_index)03d - %(title)s.%(ext)s"),
            "noplaylist": False,
            "progress_hooks": [playlist_progress_hook(job_id)],
            "ignoreerrors": True,
            "writethumbnail": False,
            "writeinfojson": False,
            "windowsfilenames": True,
            "merge_output_format": file_type if file_type in {"mp4", "mkv"} else None,
        }
    )

    if file_type == "mp3":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    allowed = {".mp3", ".mp4", ".mkv", ".webm", ".m4a", ".opus"}
    downloaded_files = [
        p for p in playlist_dir.rglob("*")
        if p.is_file()
        and not p.name.endswith((".part", ".ytdl"))
        and p.suffix.lower() in allowed
    ]

    for item in items:
        index = item["index"]
        prefix = f"{index:03d} - "
        candidates = [p for p in downloaded_files if p.name.startswith(prefix)]

        if candidates:
            path = max(candidates, key=lambda p: p.stat().st_size)
            update_playlist_item(
                job_id,
                index,
                status="complete",
                percent=100,
                size=path.stat().st_size,
                filename=path.name,
                message="Completed",
            )
        else:
            update_playlist_item(
                job_id,
                index,
                status="error",
                percent=0,
                message="Could not download this video.",
            )

    job = get_job(job_id) or {}
    completed = job.get("completed_count", 0)
    failed = job.get("failed_count", 0)

    if completed == 0:
        raise RuntimeError("No videos could be downloaded from this playlist.")

    update_job(
        job_id,
        status="zipping",
        percent=99,
        message="Creating ZIP file...",
        current_title="Creating ZIP...",
    )

    zip_name = f"{playlist_title}.zip"
    zip_path = job_dir / zip_name

    with zipfile.ZipFile(
        zip_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for path in playlist_dir.rglob("*"):
            if path.is_file():
                archive.write(path, arcname=path.relative_to(job_dir))

    zip_size = zip_path.stat().st_size
    message = (
        f"Playlist complete: {completed} downloaded, {failed} failed."
        if failed
        else f"Playlist complete: {completed}/{len(items)} downloaded."
    )

    update_job(
        job_id,
        status="complete",
        percent=100,
        message=message,
        file=str(zip_path),
        filename=zip_name,
        total=zip_size,
        downloaded=zip_size,
        speed=0,
        eta=0,
    )


def run_download(
    job_id: str,
    url: str,
    quality: str,
    file_type: str,
):
    job_dir = None

    try:
        if not FFMPEG:
            raise RuntimeError("FFmpeg is not installed.")

        job_dir = Path(tempfile.mkdtemp(prefix=f"clipro_{job_id}_"))

        update_job(
            job_id,
            status="starting",
            percent=0,
            message="Reading URL...",
        )

        probe_opts = ytdlp_base_options()
        probe_opts.update(
            {
                "extract_flat": "in_playlist",
                "noplaylist": False,
                "skip_download": True,
            }
        )

        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            probe = ydl.extract_info(url, download=False)

        entries = list(probe.get("entries") or [])
        is_playlist = len(entries) > 1

        if is_playlist:
            download_playlist(job_id, url, quality, file_type, job_dir)
        else:
            update_job(job_id, kind="single")
            download_single(job_id, url, quality, file_type, job_dir)

    except Exception as exc:
        if job_dir and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)

        update_job(
            job_id,
            status="error",
            percent=0,
            message=str(exc),
        )


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "yt_dlp": getattr(yt_dlp.version, "__version__", "unknown"),
        "deno": bool(DENO),
        "ffmpeg": bool(FFMPEG),
    }


@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    url = req.url.strip()

    if not valid_url(url):
        raise HTTPException(400, "Please enter a valid http/https URL.")

    try:
        info = await asyncio.to_thread(get_info, url)
        entries = list(info.get("entries") or [])

        if entries:
            first = next((entry for entry in entries if entry), {}) or {}
            return {
                "kind": "playlist",
                "title": info.get("title", "Playlist"),
                "uploader": info.get("uploader") or info.get("channel") or "Unknown",
                "source": info.get("webpage_url_domain")
                or info.get("extractor_key")
                or "Unknown",
                "duration": None,
                "thumbnail": first.get("thumbnail") or info.get("thumbnail"),
                "playlist_count": len(entries),
                "qualities": [],
            }

        formats = info.get("formats") or []
        heights = sorted(
            {
                int(fmt["height"])
                for fmt in formats
                if fmt.get("height") and fmt.get("vcodec") != "none"
            }
        )

        return {
            "kind": "single",
            "title": info.get("title", "Unknown"),
            "uploader": info.get("uploader") or info.get("channel") or "Unknown",
            "source": info.get("webpage_url_domain")
            or info.get("extractor_key")
            or "Unknown",
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "qualities": heights,
        }

    except Exception as exc:
        raise HTTPException(400, f"Could not analyze URL: {exc}")


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    url = req.url.strip()
    file_type = req.file_type.lower()
    quality = req.quality.lower()

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

    update_job(
        job_id,
        kind="detecting",
        status="queued",
        percent=0,
        message="Queued...",
        items=[],
        total_count=0,
        completed_count=0,
        failed_count=0,
    )

    asyncio.create_task(
        asyncio.to_thread(
            run_download,
            job_id,
            url,
            quality,
            file_type,
        )
    )

    return {"job_id": job_id}


@app.get("/api/progress/{job_id}")
def progress(job_id: str):
    job = get_job(job_id)

    if not job:
        raise HTTPException(404, "Download job not found.")

    return job


def cleanup_download(path: Path):
    try:
        if path.exists():
            path.unlink()

        if path.parent.exists():
            shutil.rmtree(path.parent, ignore_errors=True)
    except Exception:
        pass


@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    job = get_job(job_id)

    if not job or job.get("status") != "complete":
        raise HTTPException(404, "File is not ready yet.")

    path = Path(job["file"])

    if not path.exists():
        raise HTTPException(404, "File not found.")

    media_type = (
        "application/zip"
        if path.suffix.lower() == ".zip"
        else "application/octet-stream"
    )

    return FileResponse(
        path,
        media_type=media_type,
        filename=job["filename"],
        background=BackgroundTask(cleanup_download, path),
    )
