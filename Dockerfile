# Image of the jobs and the notebook: the GDAL command line and Python.
# Debian sid for a recent GDAL (3.11 or newer) with the gdal vector and raster commands that
# job.firms_grid uses, as in the image of remote-sensing-open-source-docker
FROM debian:sid-slim

WORKDIR /usr/src/app

# --no-install-recommends keeps the image small, ca-certificates is needed for HTTPS
RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    gdal-bin \
    gdal-plugins \
    python3 \
    python3-venv \
  && rm -rf /var/lib/apt/lists/*

# Dependencies first, so they stay cached when only the code changes
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p src/forest_fire && touch src/forest_fire/__init__.py \
  && python3 -m venv .venv \
  && .venv/bin/pip install --no-cache-dir -e ".[notebook]"
ENV PATH=/usr/src/app/.venv/bin:$PATH

COPY src src
COPY job job
COPY notebook notebook

ENV GDAL_HTTP_MAX_RETRY=5
ENV GDAL_HTTP_RETRY_DELAY=5
ENV GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR
ENV VSI_CACHE=TRUE
ENV CPL_VSIL_CURL_CACHE_SIZE=268435456
ENV GDAL_HTTP_TCP_KEEPALIVE=YES
