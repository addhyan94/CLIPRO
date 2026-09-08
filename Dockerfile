FROM node:22-bookworm-slim AS pot-provider

WORKDIR /opt/bgutil

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch 1.3.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git .

WORKDIR /opt/bgutil/server

RUN npm ci \
    && npx tsc

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/root/.deno/bin:/usr/local/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        nodejs \
        npm \
        unzip \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh | sh

COPY --from=pot-provider /opt/bgutil/server /opt/bgutil/server

WORKDIR /app

COPY requirements.txt .

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python -m py_compile main.py \
    && python -m pip show bgutil-ytdlp-pot-provider \
    && python -c "import yt_dlp, yt_dlp_ejs; print('yt-dlp:', yt_dlp.version.__version__); print('EJS: OK')" \
    && yt-dlp --version \
    && deno --version \
    && node --version \
    && npm --version \
    && ffmpeg -version \
    && ffprobe -version \
    && test -f /opt/bgutil/server/build/main.js

CMD ["sh", "-c", "node /opt/bgutil/server/build/main.js --host 127.0.0.1 --port 4416 >/tmp/bgutil.log 2>&1 & exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
