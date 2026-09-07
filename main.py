from pathlib import Path
import asyncio
import re
import uuid
import threading
import shutil
import tempfile
import zipfile

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask


BASE = Path(__file__).resolve().parent

FFMPEG = shutil.which("ffmpeg")
if not FFMPEG:
    print("WARNING: FFmpeg not found. Install it with:")
    print("sudo apt install ffmpeg")

app = FastAPI(title="Media Downloader")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

# In-memory job store for the local version.
jobs = {}
jobs_lock = threading.Lock()


class AnalyzeRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality: str
    file_type: str


def valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://", url.strip(), re.I))


def clean_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "download")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:180] or "download"


def get_info(url: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def update_job(job_id: str, **values):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(values)


def get_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        return dict(job) if job else None


def format_bytes(value):
    if not value:
        return 0
    return int(value)


def make_item_from_info(entry, index):
    title = clean_filename(entry.get("title") or f"Video {index}")
    return {
        "index": index,
        "title": title,
        "thumbnail": entry.get("thumbnail"),
        "status": "queued",
        "percent": 0,
        "size": 0,
        "filename": None,
        "message": "Queued",
    }


def progress_hook(job_id):
    def hook(data):
        state = data.get("status")
        info = data.get("info_dict") or {}

        # Playlist position. yt-dlp normally provides playlist_index here.
        playlist_index = info.get("playlist_index") or data.get("playlist_index")

        if state == "downloading":
            total = (
                data.get("total_bytes")
                or data.get("total_bytes_estimate")
                or 0
            )
            downloaded = data.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else 0

            speed = data.get("speed") or 0
            eta = data.get("eta")

            update_job(
                job_id,
                status="downloading",
                percent=max(0, min(99, percent)),
                downloaded=downloaded,
                total=total,
                speed=speed,
                eta=eta,
                current_title=info.get("title") or "Downloading...",
                current_index=playlist_index,
                message="Downloading video/audio...",
            )

            if playlist_index:
                with jobs_lock:
                    job = jobs.get(job_id)
                    if job and isinstance(job.get("items"), list):
                        for item in job["items"]:
                            if item.get("index") == playlist_index:
                                item["status"] = "downloading"
                                item["percent"] = round(max(0, min(99, percent)), 1)
                                item["size"] = format_bytes(downloaded)
                                item["message"] = "Downloading..."
                                if info.get("thumbnail"):
                                    item["thumbnail"] = info["thumbnail"]
                                break

        elif state == "finished":
            update_job(
                job_id,
                current_index=playlist_index,
                current_title=info.get("title") or "Processing...",
                message="Download received. Processing audio/video...",
            )

    return hook


def choose_format(quality: str, file_type: str) -> str:
    # MP3 = audio only.
    if file_type == "mp3":
        return "bestaudio[acodec!=none]/best"

    if quality == "best":
        video = "bestvideo[vcodec!=none]"
        fallback = "best[vcodec!=none]"
    else:
        height = int(quality.rstrip("p"))
        video = f"bestvideo[height<={height}][vcodec!=none]"
        fallback = f"best[height<={height}][vcodec!=none]"

    # Explicit video + audio, with a combined-stream fallback.
    return f"{video}+bestaudio[acodec!=none]/{fallback}"


def find_downloaded_file(folder: Path, stem: str | None = None):
    files = [
        p for p in folder.rglob("*")
        if p.is_file()
        and not p.name.endswith((".part", ".ytdl"))
        and p.suffix.lower() not in {".json", ".description", ".jpg", ".jpeg", ".webp"}
    ]

    if stem:
        matching = [p for p in files if p.stem == stem]
        if matching:
            return max(matching, key=lambda p: p.stat().st_size)

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

        completed = sum(1 for item in items if item.get("status") == "complete")
        failed = sum(1 for item in items if item.get("status") == "error")

        job["completed_count"] = completed
        job["failed_count"] = failed
        job["total_count"] = len(items)

        if items:
            # Completed videos count fully; current video contributes its percentage.
            current = next(
                (item for item in items if item.get("status") == "downloading"),
                None,
            )
            current_percent = float(current.get("percent", 0)) if current else 0
            overall = ((completed + current_percent / 100) / len(items)) * 100
            job["percent"] = round(min(99, overall), 1)


def playlist_progress_hook(job_id):
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
    """
    Read playlist entries without downloading media.
    Flat extraction keeps this step reasonably fast and avoids downloading twice.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = list(info.get("entries") or [])
    items = []

    for i, entry in enumerate(entries, start=1):
        if not entry:
            continue

        item = make_item_from_info(entry, i)

        if not item.get("thumbnail"):
            item["thumbnail"] = entry.get("thumbnails", [{}])[-1].get("url") if entry.get("thumbnails") else None

        items.append(item)

    return info, items


def download_single(
    job_id: str,
    url: str,
    quality: str,
    file_type: str,
    job_dir: Path,
):
    with yt_dlp.YoutubeDL(
        {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }
    ) as ydl:
        info = ydl.extract_info(url, download=False)

    title = clean_filename(info.get("title", "download"))

    base_output = job_dir / f"{job_id}.%(ext)s"

    opts = {
        "format": choose_format(quality, file_type),
        "outtmpl": str(base_output),
        "noplaylist": True,
        "progress_hooks": [progress_hook(job_id)],
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
    }

    if file_type in {"mp4", "mkv"}:
        opts["merge_output_format"] = file_type

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
        uploader=info.get("uploader") or info.get("channel"),
        status="downloading",
        percent=0,
        message="Downloading video/audio...",
    )

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    source = find_downloaded_file(job_dir)

    if not source:
        raise RuntimeError("Download finished but output file was not found.")

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

    playlist_title = clean_filename(
        playlist_info.get("title") or "Playlist"
    )

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

    opts = {
        "format": choose_format(quality, file_type),
        "outtmpl": str(playlist_dir / "%(playlist_index)03d - %(title)s.%(ext)s"),
        "noplaylist": False,
        "progress_hooks": [playlist_progress_hook(job_id)],
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "ignoreerrors": True,
        "writethumbnail": False,
        "writeinfojson": False,
    }

    if file_type in {"mp4", "mkv"}:
        opts["merge_output_format"] = file_type

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

    # Inspect actual files after yt-dlp has finished each entry.
    # This also catches entries whose progress hook did not expose all metadata.
    downloaded_files = [
        p for p in playlist_dir.rglob("*")
        if p.is_file()
        and not p.name.endswith((".part", ".ytdl"))
        and p.suffix.lower() in {".mp3", ".mp4", ".mkv", ".webm", ".m4a", ".opus"}
    ]

    used = set()

    for item in items:
        index = item["index"]
        prefix = f"{index:03d} - "

        candidates = [
            p for p in downloaded_files
            if p.name.startswith(prefix)
        ]

        if candidates:
            path = max(candidates, key=lambda p: p.stat().st_size)
            used.add(path)

            update_playlist_item(
                job_id,
                index,
                status="complete",
                percent=100,
                size=path.stat().st_size,
                filename=path.name,
                message="Completed",
            )
        elif item.get("status") not in {"complete"}:
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

    # Put the playlist folder itself inside the ZIP.
    with zipfile.ZipFile(
        zip_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as zf:
        for path in playlist_dir.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(job_dir))

    zip_size = zip_path.stat().st_size

    if failed:
        message = f"Playlist complete: {completed} downloaded, {failed} failed."
    else:
        message = f"Playlist complete: {completed}/{len(items)} downloaded."

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
        completed_count=completed,
        failed_count=failed,
    )


def run_download(job_id: str, url: str, quality: str, file_type: str):
    job_dir = None

    try:
        if not FFMPEG:
            raise RuntimeError(
                "FFmpeg is not installed. Run: sudo apt install ffmpeg"
            )

        job_dir = Path(
            tempfile.mkdtemp(prefix=f"media_{job_id}_")
        )

        update_job(
            job_id,
            status="starting",
            percent=0,
            message="Reading URL...",
        )

        # Detect whether the URL resolves to a playlist/channel collection.
        # We only use this for classification; actual playlist preparation
        # happens inside download_playlist().
        try:
            probe_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "extract_flat": "in_playlist",
                "noplaylist": False,
            }

            with yt_dlp.YoutubeDL(probe_opts) as ydl:
                probe = ydl.extract_info(url, download=False)

            entries = list(probe.get("entries") or [])

        except Exception:
            # If probing fails, let the single-download path produce the
            # original yt-dlp error message.
            entries = []

        is_playlist = bool(entries) and len(entries) > 1

        if is_playlist:
            download_playlist(
                job_id,
                url,
                quality,
                file_type,
                job_dir,
            )
        else:
            update_job(job_id, kind="single")
            download_single(
                job_id,
                url,
                quality,
                file_type,
                job_dir,
            )

    except Exception as exc:
        if job_dir and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)

        update_job(
            job_id,
            status="error",
            percent=0,
            message=str(exc),
        )


@app.get("/")
def home():
    return FileResponse(BASE / "static" / "index.html")


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    url = req.url.strip()

    if not valid_url(url):
        raise HTTPException(400, "Please enter a valid http/https URL.")

    try:
        info = await asyncio.to_thread(get_info, url)

        entries = list(info.get("entries") or [])
        is_playlist = bool(entries)

        if is_playlist:
            # For playlists, expose the playlist title/count and first
            # available thumbnail without returning every stream format.
            first = next((e for e in entries if e), {}) or {}

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
                int(f["height"])
                for f in formats
                if f.get("height") and f.get("vcodec") != "none"
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
            int(quality.rstrip("p"))
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

        # Remove the complete temporary job directory too.
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

    return FileResponse(
        path,
        media_type="application/zip" if path.suffix.lower() == ".zip" else "application/octet-stream",
        filename=job["filename"],
        background=BackgroundTask(cleanup_download, path),
    )

