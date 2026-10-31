# vigil web app. The API key is passed at runtime (--env-file .env), never baked in.
#   docker build -t vigil .
#   docker run --rm --env-file .env -p 127.0.0.1:8100:8100 vigil
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin vigil
USER vigil

EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8100\")}/healthz', timeout=4)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8100}"]
