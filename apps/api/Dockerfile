# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

# ffmpeg is pinned deliberately: filter behaviour drifts between releases, and
# a "worked yesterday" render bug is the worst possible way to spend the last
# week before a deadline (ARCHITECTURE 10.6).
ARG FFMPEG_VERSION=7:5.1.7-0+deb12u1
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg=${FFMPEG_VERSION} \
        libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version | head -1

WORKDIR /srv/api
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY tools ./tools

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /srv/storage /var/tmp/render \
    && chown -R app:app /srv /var/tmp/render
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
