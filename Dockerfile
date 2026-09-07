FROM python:3.12-slim

# --------------------------------
# System dependencies
# --------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# --------------------------------
# Install Deno
# --------------------------------
RUN curl -fsSL https://deno.land/install.sh | sh

ENV PATH="/root/.deno/bin:${PATH}"

# --------------------------------
# Verify Deno
# --------------------------------
RUN deno --version

# --------------------------------
# App directory
# --------------------------------
WORKDIR /app

# --------------------------------
# Python dependencies
# --------------------------------
COPY requirements.txt .

RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

# --------------------------------
# Verify yt-dlp + EJS + FFmpeg
# --------------------------------
RUN yt-dlp --version \
    && python -c "import yt_dlp_ejs; print('yt-dlp-ejs: OK')" \
    && ffmpeg -version \
    && ffprobe -version

# --------------------------------
# Copy application
# --------------------------------
COPY . .

ENV PYTHONUNBUFFERED=1

# --------------------------------
# Start FastAPI
# --------------------------------
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]