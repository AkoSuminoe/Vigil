"""vigil web demo.

    uvicorn app.main:app --host 127.0.0.1 --port 8100

Uploads are processed in memory and never written to disk. GOOGLE_API_KEY stays on
the server. Behind Cloudflare Tunnel + Caddy every request arrives from 127.0.0.1,
so set TRUST_PROXY=1 to rate-limit by CF-Connecting-IP / X-Forwarded-For instead.
"""

from __future__ import annotations

import asyncio
import math
import mimetypes
import os
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser

from vigil.pipeline import analyze
from vigil.tools import PROJECT_ROOT

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
# Multipart framing around the file is small; anything bigger than this is rejected unread.
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 64 * 1024
ALLOWED_SUFFIXES = {".log", ".txt"}
SAMPLES_DIR = PROJECT_ROOT / "sample_logs"
STATIC_DIR = Path(__file__).resolve().parent / "static"

RATE_LIMIT = int(os.getenv("RATE_LIMIT", "5"))
RATE_WINDOW_S = float(os.getenv("RATE_WINDOW_S", "600"))
TRUST_PROXY = os.getenv("TRUST_PROXY") == "1"
MAX_CONCURRENT_ANALYSES = int(os.getenv("MAX_CONCURRENT_ANALYSES", "2"))

# Starlette spools multipart files to a temp file past 1 MB. The body is capped at
# MAX_BODY_BYTES before parsing, so raising the spool size keeps uploads in memory.
MultiPartParser.spool_max_size = 2 * MAX_BODY_BYTES

CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class RateLimiter:
    """In-memory sliding window per client key. Fine for a single-process demo."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self.hits: dict[str, deque[float]] = {}

    def check(self, key: str, now: float | None = None) -> float | None:
        """Record a hit and return None, or return seconds to wait if the key is over the limit."""
        now = time.monotonic() if now is None else now
        if len(self.hits) > 10_000:
            self.hits = {k: q for k, q in self.hits.items() if q and q[-1] > now - self.window_s}
        hits = self.hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return self.window_s - (now - hits[0])
        hits.append(now)
        return None


limiter = RateLimiter(RATE_LIMIT, RATE_WINDOW_S)
analysis_slots = asyncio.Semaphore(MAX_CONCURRENT_ANALYSES)

app = FastAPI(title="vigil", docs_url=None, redoc_url=None, openapi_url=None)
# Self-hosted fonts (SIL OFL), so the page needs nothing outside 'self'. Slim images
# ship no mime table, so register the type explicitly.
mimetypes.add_type("font/woff2", ".woff2")
app.mount("/static/fonts", StaticFiles(directory=STATIC_DIR / "fonts"), name="fonts")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        if cf_ip := request.headers.get("cf-connecting-ip", "").strip():
            return cf_ip
        if forwarded := request.headers.get("x-forwarded-for", "").split(",")[0].strip():
            return forwarded
    return request.client.host if request.client else "unknown"


def error(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"status": "error", "error": message}, status_code=status, headers=headers)


def sample_files() -> dict[str, Path]:
    return {p.name: p for p in sorted(SAMPLES_DIR.glob("*.log"))}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/samples")
async def samples() -> list[dict]:
    return [
        {"name": name, "lines": len(path.read_text(encoding="utf-8").splitlines()), "bytes": path.stat().st_size}
        for name, path in sample_files().items()
    ]


async def _read_capped_body(request: Request) -> bytes | None:
    """Read the request body, or return None as soon as it exceeds MAX_BODY_BYTES."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


@app.post("/api/analyze")
async def analyze_endpoint(request: Request) -> JSONResponse:
    body = await _read_capped_body(request)
    if body is None:
        return error(413, "File is larger than 2 MB.")

    async def replay() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    try:
        form = await Request(request.scope, replay).form(max_files=1, max_fields=4)
    except Exception:
        return error(400, "Could not read the form data. Send multipart/form-data with 'file' or 'sample'.")

    try:
        upload, sample = form.get("file"), form.get("sample")
        if isinstance(upload, UploadFile) and upload.filename:
            source = Path(upload.filename).name
            if Path(source).suffix.lower() not in ALLOWED_SUFFIXES:
                return error(400, "Only .log and .txt files are accepted.")
            data = await upload.read()
            if len(data) > MAX_UPLOAD_BYTES:
                return error(413, "File is larger than 2 MB.")
            if b"\x00" in data:
                return error(400, "That looks like a binary file, not a text log.")
            text = data.decode("utf-8", errors="replace")
        elif isinstance(sample, str) and sample:
            path = sample_files().get(sample)
            if path is None:
                return error(400, "Unknown sample log.")
            source, text = path.name, path.read_text(encoding="utf-8")
        else:
            return error(400, "Choose a sample log or upload a .log/.txt file.")
    finally:
        await form.close()

    if not text.strip():
        return error(400, "The log is empty.")

    if (wait := limiter.check(client_ip(request))) is not None:
        minutes = max(1, math.ceil(wait / 60))
        return error(
            429,
            f"Rate limit: {RATE_LIMIT} analyses per {RATE_WINDOW_S / 60:g} minutes. Try again in ~{minutes} min.",
            headers={"Retry-After": str(math.ceil(wait))},
        )

    async with analysis_slots:
        result = await analyze(text, source)
    status = {None: 200, "unsupported_format": 422, "config": 503, "timeout": 504}.get(result["error_kind"], 502)
    return JSONResponse(result, status_code=status)
