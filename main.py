from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import edge_tts
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("story-tts-api")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("TTS_DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "jobs.sqlite3"

VOICES: dict[str, str] = {
    "vi-VN-NamMinhNeural": "Nam Minh",
    "vi-VN-HoaiMyNeural": "Hoài My",
}
RATES = (0.75, 1.0, 1.25, 1.5, 2.0)
MAX_QUEUED_JOBS = max(1, int(os.getenv("MAX_QUEUED_JOBS", "50")))
AUDIO_URL_TTL_SECONDS = max(60, int(os.getenv("AUDIO_URL_TTL_SECONDS", "86400")))
QUEUE_POLL_SECONDS = 1.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def database() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_database() -> None:
    with database() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                voice TEXT NOT NULL,
                rate REAL NOT NULL,
                chapter_id TEXT,
                cache_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT,
                audio_path TEXT,
                source_text TEXT,
                cache_hit INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_cache_key ON jobs(cache_key)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id)")
        # If the service restarted mid-synthesis, its text is still present.
        con.execute(
            "UPDATE jobs SET status='queued', updated_at=? WHERE status='generating'",
            (utc_now(),),
        )


def valid_rate(rate: float) -> bool:
    return any(abs(rate - supported) < 1e-9 for supported in RATES)


def build_cache_key(text: str, voice: str, rate: float) -> str:
    material = json.dumps(
        {"text": text, "voice": voice, "rate": rate},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def api_key() -> str:
    return os.getenv("TTS_API_KEY", "").strip()


async def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = api_key()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="API chưa được cấu hình. Hãy đặt biến môi trường TTS_API_KEY.",
        )
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="API key không hợp lệ hoặc bị thiếu.")


def audio_secret() -> bytes:
    secret = os.getenv("AUDIO_URL_SECRET", "").strip() or api_key()
    if not secret:
        # Signed URLs are not issued unless an authenticated API request is possible.
        secret = "not-configured"
    return secret.encode("utf-8")


def sign_audio_url(job_id: int, expires: int) -> str:
    message = f"{job_id}:{expires}".encode("ascii")
    return hmac.new(audio_secret(), message, hashlib.sha256).hexdigest()


def audio_links(request: Request, job_id: int) -> tuple[str | None, str | None]:
    row = fetch_job(job_id)
    if row is None or row["status"] != "completed" or not row["audio_path"]:
        return None, None
    if not Path(row["audio_path"]).is_file():
        return None, None
    expires = int(time.time()) + AUDIO_URL_TTL_SECONDS
    token = sign_audio_url(job_id, expires)
    base = str(request.base_url).rstrip("/")
    audio = f"{base}/v1/jobs/{job_id}/audio?expires={expires}&token={token}"
    return audio, f"{audio}&download=1"


def fetch_job(job_id: int) -> sqlite3.Row | None:
    with database() as con:
        return con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def insert_job(
    *,
    status: str,
    voice: str,
    rate: float,
    chapter_id: str | None,
    cache_key: str,
    source_text: str | None,
    audio_path: str | None = None,
    cache_hit: bool = False,
) -> int:
    now = utc_now()
    with database() as con:
        cursor = con.execute(
            """
            INSERT INTO jobs
                (status, voice, rate, chapter_id, cache_key, created_at, updated_at,
                 audio_path, source_text, cache_hit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                status,
                voice,
                rate,
                chapter_id,
                cache_key,
                now,
                now,
                audio_path,
                source_text,
                int(cache_hit),
            ),
        )
        return int(cursor.lastrowid)


def set_job(
    job_id: int,
    status: str,
    *,
    error: str | None = None,
    audio_path: str | None = None,
    clear_text: bool = False,
) -> None:
    with database() as con:
        con.execute(
            """
            UPDATE jobs
            SET status=?, updated_at=?, error=?, audio_path=COALESCE(?, audio_path),
                source_text=CASE WHEN ? THEN NULL ELSE source_text END
            WHERE id=?
            """,
            (status, utc_now(), error, audio_path, int(clear_text), job_id),
        )


def public_job(row: sqlite3.Row, request: Request, *, reused: bool = False) -> dict[str, Any]:
    job_id = int(row["id"])
    status = str(row["status"])
    audio_url, download_url = audio_links(request, job_id) if status == "completed" else (None, None)
    return {
        "job_id": job_id,
        "chapter_id": row["chapter_id"],
        "status": status,
        "voice": row["voice"],
        "voice_name": VOICES.get(row["voice"], row["voice"]),
        "rate": float(row["rate"]),
        "cache_hit": bool(row["cache_hit"]),
        "reused_request": reused,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "status_url": str(request.base_url).rstrip("/") + f"/v1/jobs/{job_id}",
        "audio_url": audio_url,
        "download_url": download_url,
        "error": row["error"],
    }


class TTSRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str = "vi-VN-NamMinhNeural"
    rate: float = 1.0
    chapter_id: str | None = Field(default=None, max_length=300)


async def synthesize_to_file(text: str, voice: str, rate: float, output: Path) -> None:
    edge_percent = round((rate - 1.0) * 100)
    edge_rate = f"{edge_percent:+d}%"
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".partial" + output.suffix)
    last_error: Exception | None = None

    for attempt in range(1, 4):
        partial.unlink(missing_ok=True)
        try:
            communicator = edge_tts.Communicate(text=text, voice=voice, rate=edge_rate)
            await communicator.save(str(partial))
            if not partial.is_file() or partial.stat().st_size < 128:
                raise RuntimeError("Edge-TTS không trả về tệp MP3 hợp lệ.")
            os.replace(partial, output)
            return
        except Exception as exc:
            last_error = exc
            partial.unlink(missing_ok=True)
            if attempt < 3:
                delay = 2 ** attempt
                logger.warning(
                    "TTS job retry %s/3 after %ss: %s",
                    attempt,
                    delay,
                    str(exc)[:500],
                )
                await asyncio.sleep(delay)

    raise RuntimeError(f"Tổng hợp thất bại sau 3 lần thử: {last_error}") from last_error


async def worker_loop(queue: asyncio.Queue[int]) -> None:
    while True:
        job_id = await queue.get()
        try:
            row = fetch_job(job_id)
            if row is None or row["status"] != "queued":
                continue
            source_text = row["source_text"]
            if not source_text or not source_text.strip():
                set_job(job_id, "failed", error="Không còn nội dung để tổng hợp.", clear_text=True)
                continue

            key = str(row["cache_key"])
            output = AUDIO_DIR / f"{key}.mp3"
            # A previous matching job may have completed while this request waited.
            with database() as con:
                cached = con.execute(
                    """
                    SELECT audio_path FROM jobs
                    WHERE cache_key=? AND status='completed' AND audio_path IS NOT NULL
                    ORDER BY id DESC LIMIT 1
                    """,
                    (key,),
                ).fetchone()
            if cached and Path(cached["audio_path"]).is_file():
                set_job(job_id, "completed", audio_path=str(cached["audio_path"]), clear_text=True)
                continue

            set_job(job_id, "generating", error=None)
            try:
                await synthesize_to_file(
                    str(source_text), str(row["voice"]), float(row["rate"]), output
                )
                set_job(
                    job_id,
                    "completed",
                    audio_path=str(output),
                    error=None,
                    clear_text=True,
                )
            except Exception as exc:
                output.unlink(missing_ok=True)
                output.with_name(output.stem + ".partial" + output.suffix).unlink(missing_ok=True)
                logger.exception("TTS job %s failed", job_id)
                set_job(
                    job_id,
                    "failed",
                    error=(str(exc) or exc.__class__.__name__)[:1500],
                    clear_text=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected worker error for job %s", job_id)
        finally:
            queue.task_done()


@asynccontextmanager
async def lifespan(application: FastAPI):
    init_database()
    queue: asyncio.Queue[int] = asyncio.Queue()
    with database() as con:
        pending_ids = con.execute(
            "SELECT id FROM jobs WHERE status='queued' ORDER BY id"
        ).fetchall()
    for row in pending_ids:
        queue.put_nowait(int(row["id"]))
    application.state.job_queue = queue
    worker_task = asyncio.create_task(worker_loop(queue), name="story-tts-worker")
    application.state.worker_task = worker_task
    try:
        yield
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Story TTS API",
    description="Vietnamese story chapter to MP3: Nam Minh, Hoài My and configurable reading speed.",
    version="1.0.0",
    lifespan=lifespan,
)

origins_raw = os.getenv("ALLOWED_ORIGINS", "").strip()
allowed_origins = [origin.strip() for origin in origins_raw.split(",") if origin.strip()]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "Story TTS API",
        "docs": "/docs",
        "health": "/health",
        "supported_voices": VOICES,
        "supported_rates": list(RATES),
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "story-tts-api"}


@app.get("/v1/voices", dependencies=[Depends(require_api_key)])
async def voices() -> dict[str, Any]:
    return {
        "voices": [{"id": voice_id, "name": name} for voice_id, name in VOICES.items()],
        "rates": list(RATES),
        "format": "audio/mpeg",
    }


@app.post("/v1/tts", status_code=202, dependencies=[Depends(require_api_key)])
async def create_tts_job(payload: TTSRequest, request: Request) -> JSONResponse:
    text = payload.text
    if not text.strip():
        raise HTTPException(status_code=422, detail="Nội dung chương không được để trống.")
    if payload.voice not in VOICES:
        raise HTTPException(
            status_code=422,
            detail=f"Giọng đọc không hợp lệ. Chọn một trong: {', '.join(VOICES)}",
        )
    if not valid_rate(payload.rate):
        raise HTTPException(status_code=422, detail=f"Tốc độ phải thuộc: {', '.join(map(str, RATES))}")

    cache_key = build_cache_key(text, payload.voice, payload.rate)
    queue: asyncio.Queue[int] = request.app.state.job_queue

    with database() as con:
        pending = con.execute(
            """
            SELECT * FROM jobs
            WHERE cache_key=? AND status IN ('queued', 'generating')
            ORDER BY id ASC LIMIT 1
            """,
            (cache_key,),
        ).fetchone()
        cached = con.execute(
            """
            SELECT * FROM jobs
            WHERE cache_key=? AND status='completed' AND audio_path IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (cache_key,),
        ).fetchone()
        waiting_count = int(
            con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','generating')").fetchone()[0]
        )

    if pending is not None:
        return JSONResponse(
            status_code=202,
            content=public_job(pending, request, reused=True),
        )

    if cached is not None and Path(cached["audio_path"]).is_file():
        new_id = insert_job(
            status="completed",
            voice=payload.voice,
            rate=payload.rate,
            chapter_id=payload.chapter_id,
            cache_key=cache_key,
            source_text=None,
            audio_path=str(cached["audio_path"]),
            cache_hit=True,
        )
        return JSONResponse(status_code=200, content=public_job(fetch_job(new_id), request))

    if waiting_count >= MAX_QUEUED_JOBS:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "30"},
            content={"detail": "Hàng đợi đang đầy. Hãy thử lại sau.", "max_queued_jobs": MAX_QUEUED_JOBS},
        )

    job_id = insert_job(
        status="queued",
        voice=payload.voice,
        rate=payload.rate,
        chapter_id=payload.chapter_id,
        cache_key=cache_key,
        source_text=text,
    )
    queue.put_nowait(job_id)
    return JSONResponse(status_code=202, content=public_job(fetch_job(job_id), request))


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def get_job_status(job_id: int, request: Request) -> dict[str, Any]:
    row = fetch_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ audio.")
    return public_job(row, request)


@app.get("/v1/jobs/{job_id}/audio", name="serve_audio")
async def serve_audio(
    job_id: int,
    expires: int = Query(...),
    token: str = Query(..., min_length=32, max_length=128),
    download: bool = Query(False),
) -> FileResponse:
    if expires < int(time.time()):
        raise HTTPException(status_code=403, detail="Liên kết audio đã hết hạn. Hãy tải lại trạng thái tác vụ.")
    expected = sign_audio_url(job_id, expires)
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Chữ ký liên kết audio không hợp lệ.")
    row = fetch_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ audio.")
    if row["status"] != "completed" or not row["audio_path"]:
        raise HTTPException(status_code=409, detail="Audio chưa sẵn sàng.")
    path = Path(row["audio_path"])
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Tệp audio đã bị xóa khỏi bộ nhớ cache.")
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=f"chapter-{job_id}.mp3",
        content_disposition_type=disposition,
        headers={"Cache-Control": "private, max-age=300"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        workers=1,
    )
