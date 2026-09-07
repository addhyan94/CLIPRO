const $ = (id) => document.getElementById(id);
let analyzedUrl = "";
let progressTimer = null;

function status(text, error = false) {
  $("status").textContent = text;
  $("status").style.color = error ? "#c62828" : "#555";
}

function formatBytes(bytes) {
  if (!bytes) return "0 MB";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
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

async function pollProgress(jobId) {
  if (progressTimer) clearInterval(progressTimer);

  const check = async () => {
    try {
      const res = await fetch(`/api/progress/${jobId}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Progress check failed.");

      const meta = data.status === "downloading"
        ? `${formatBytes(data.downloaded)} / ${formatBytes(data.total)}  •  ${formatSpeed(data.speed)}  •  ETA ${formatEta(data.eta)}`
        : "";
      setProgress(data.percent, data.message, meta);

      if (data.status === "complete") {
        clearInterval(progressTimer);
        progressTimer = null;
        $("downloadBtn").disabled = false;
        status("Download complete.");
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
      status(err.message, true);
    }
  };

  await check();
  progressTimer = setInterval(check, 700);
}

$("analyzeBtn").addEventListener("click", async () => {
  const url = $("url").value.trim();
  if (!url) return status("Paste a URL first.", true);

  $("analyzeBtn").disabled = true;
  status("Analyzing...");
  $("info").classList.add("hidden");
  $("options").classList.add("hidden");
  $("downloadBtn").classList.add("hidden");
  $("progressWrap").classList.add("hidden");

  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Analysis failed.");

    analyzedUrl = url;
    $("title").textContent = data.title || "-";
    $("uploader").textContent = data.uploader || "-";
    $("source").textContent = data.source || "-";

    if (data.thumbnail) {
      $("thumb").src = data.thumbnail;
      $("thumb").classList.remove("hidden");
    }

    const select = $("quality");
    select.innerHTML = "";
    const qualities = data.qualities || [];
    qualities.forEach(q => {
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

    $("info").classList.remove("hidden");
    $("options").classList.remove("hidden");
    $("downloadBtn").classList.remove("hidden");
    status("Ready. Select quality and file type.");
  } catch (err) {
    status(err.message, true);
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
        file_type: $("fileType").value
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Download failed.");
    await pollProgress(data.job_id);
  } catch (err) {
    $("downloadBtn").disabled = false;
    status(err.message, true);
  }
});
