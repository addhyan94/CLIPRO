const $ = (id) => document.getElementById(id);

let analyzedUrl = "";
let progressTimer = null;
let analyzedKind = "single";

function status(text, error = false) {
  $("status").textContent = text;
  $("status").style.color = error ? "#c62828" : "#555";
}

function formatBytes(bytes) {
  if (!bytes) return "0 MB";

  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = Number(bytes);
  let i = 0;

  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }

  return `${n.toFixed(n >= 100 ? 0 : 1)} ${units[i]}`;
}

function formatSpeed(bytes) {
  return bytes ? `${formatBytes(bytes)}/s` : "--";
}

function formatEta(seconds) {
  if (seconds === null || seconds === undefined || seconds < 0) return "--";

  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;

  return h ? `${h}h ${m}m` : `${m}m ${sec}s`;
}

function setProgress(percent, message, meta = "") {
  const p = Math.max(0, Math.min(100, Number(percent) || 0));

  $("progressWrap").classList.remove("hidden");
  $("progressBar").style.width = `${p}%`;
  $("progressPercent").textContent = `${Math.round(p)}%`;
  $("progressMessage").textContent = message || "Working...";
  $("progressMeta").textContent = meta;
}

function renderPlaylistItems(items = []) {
  const container = $("playlistItems");
  container.innerHTML = "";

  if (!items.length) {
    $("playlistProgress").classList.add("hidden");
    return;
  }

  $("playlistProgress").classList.remove("hidden");

  for (const item of items) {
    const row = document.createElement("div");
    row.className = "playlist-item";

    const image = document.createElement("img");
    image.className = "playlist-thumb";
    image.alt = "";
    if (item.thumbnail) image.src = item.thumbnail;

    const body = document.createElement("div");
    body.className = "playlist-item-body";

    const title = document.createElement("div");
    title.className = "playlist-item-title";
    title.textContent = `${item.index}. ${item.title || "Video"}`;

    const meta = document.createElement("div");
    meta.className = "playlist-item-meta";
    meta.textContent =
      item.status === "complete"
        ? `Completed • ${formatBytes(item.size)}`
        : item.status === "error"
          ? item.message || "Failed"
          : item.status === "processing"
            ? "Processing..."
            : `${Math.round(Number(item.percent) || 0)}% • ${formatBytes(item.size)}`;

    body.appendChild(title);
    body.appendChild(meta);
    row.appendChild(image);
    row.appendChild(body);
    container.appendChild(row);
  }
}

async function pollProgress(jobId) {
  if (progressTimer) clearInterval(progressTimer);

  const check = async () => {
    try {
      const res = await fetch(`/api/progress/${jobId}`, {
        cache: "no-store"
      });

      const data = await res.json();

      if (!res.ok) {
        throw new Error(data.detail || "Progress check failed.");
      }

      const meta =
        data.status === "downloading"
          ? `${formatBytes(data.downloaded)} / ${formatBytes(data.total)} • ${formatSpeed(data.speed)} • ETA ${formatEta(data.eta)}`
          : "";

      setProgress(data.percent, data.message, meta);

      if (Array.isArray(data.items) && data.items.length) {
        renderPlaylistItems(data.items);
        $("playlistCount").textContent =
          `${data.completed_count || 0} / ${data.total_count || data.items.length}`;
      }

      if (data.status === "complete") {
        clearInterval(progressTimer);
        progressTimer = null;

        $("downloadBtn").disabled = false;
        status(data.message || "Download complete.");

        const link = document.createElement("a");
        link.href = `/api/file/${jobId}`;
        link.download = data.filename || "download";
        document.body.appendChild(link);
        link.click();
        link.remove();

      } else if (data.status === "error") {
        clearInterval(progressTimer);
        progressTimer = null;

        $("downloadBtn").disabled = false;
        status(data.message || "Download failed.", true);
      }
    } catch (err) {
      clearInterval(progressTimer);
      progressTimer = null;

      $("downloadBtn").disabled = false;
      status(err.message || "Something went wrong.", true);
    }
  };

  await check();
  progressTimer = setInterval(check, 700);
}

$("analyzeBtn").addEventListener("click", async () => {
  const url = $("url").value.trim();

  if (!url) {
    status("Paste a URL first.", true);
    return;
  }

  $("analyzeBtn").disabled = true;
  $("downloadBtn").disabled = true;
  status("Analyzing...");
  $("info").classList.add("hidden");
  $("options").classList.add("hidden");
  $("downloadBtn").classList.add("hidden");
  $("progressWrap").classList.add("hidden");
  $("playlistProgress").classList.add("hidden");
  $("playlistBadge").classList.add("hidden");
  renderPlaylistItems([]);

  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ url }),
    });

    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || "Analysis failed.");
    }

    analyzedUrl = url;
    analyzedKind = data.kind || "single";

    $("title").textContent = data.title || "-";
    $("uploader").textContent = data.uploader || "-";
    $("source").textContent = data.source || "-";

    $("thumb").classList.add("hidden");
    if (data.thumbnail) {
      $("thumb").src = data.thumbnail;
      $("thumb").classList.remove("hidden");
    }

    const select = $("quality");
    select.innerHTML = "";

    if (analyzedKind === "playlist") {
      const best = document.createElement("option");
      best.value = "best";
      best.textContent = "Best available";
      select.appendChild(best);
      select.value = "best";

      $("playlistBadge").textContent =
        `${data.playlist_count || 0} videos • best available quality`;
      $("playlistBadge").classList.remove("hidden");
    } else {
      const qualities = data.qualities || [];

      qualities.forEach((q) => {
        const option = document.createElement("option");
        option.value = `${q}p`;
        option.textContent = `${q}p${q >= 720 ? " HD" : ""}`;
        select.appendChild(option);
      });

      const best = document.createElement("option");
      best.value = "best";
      best.textContent = "Best available";
      select.insertBefore(best, select.firstChild);

      select.value = qualities.includes(1080) ? "1080p" : "best";
    }

    $("info").classList.remove("hidden");
    $("options").classList.remove("hidden");
    $("downloadBtn").classList.remove("hidden");
    $("downloadBtn").disabled = false;

    status(
      analyzedKind === "playlist"
        ? "Playlist ready. Choose file type and download."
        : "Ready. Select quality and file type."
    );

  } catch (err) {
    status(err.message || "Analysis failed.", true);
  } finally {
    $("analyzeBtn").disabled = false;
  }
});

$("downloadBtn").addEventListener("click", async () => {
  if (!analyzedUrl) return;

  $("downloadBtn").disabled = true;
  setProgress(0, "Starting download...", "Preparing...");
  status("Download started...");

  try {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        url: analyzedUrl,
        quality: $("quality").value,
        file_type: $("fileType").value,
      }),
    });

    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || "Download failed.");
    }

    await pollProgress(data.job_id);
  } catch (err) {
    $("downloadBtn").disabled = false;
    status(err.message || "Download failed.", true);
  }
});

$("url").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !$("analyzeBtn").disabled) {
    $("analyzeBtn").click();
  }
});
