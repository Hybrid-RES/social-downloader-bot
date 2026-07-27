# syntax=docker/dockerfile:1.7

FROM denoland/deno:bin-2.8.1 AS deno_bin

FROM python:3.13-slim-bookworm

ARG APP_VERSION=dev
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_VERSION=${APP_VERSION} \
    DENO_DIR=/tmp/deno \
    DENO_NO_UPDATE_CHECK=1 \
    DENO_NO_PROMPT=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       ffmpeg \
       tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app

COPY --from=deno_bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && yt-dlp --version \
    && gallery-dl --version \
    && ffmpeg -version | head -n 1 \
    && deno --version | head -n 1

COPY app/ ./app/
COPY config/ ./config/

RUN python -m compileall -q /app/app \
    && chown -R app:app /app

USER 1000:1000

HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
  CMD ["python", "-m", "app.healthcheck"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]
