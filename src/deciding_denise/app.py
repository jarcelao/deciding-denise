"""Battlesnake HTTP server."""

import logging
import math
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from httpx2 import Timeout
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from . import config
from .brain import candidates, decision_model_input, rank

DEFAULT_DECISION_TIMEOUT_SECONDS = 0.25
request_id_context: ContextVar[str] = ContextVar("request_id", default="-")
logger = logging.getLogger(__name__)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_context.get()
        return True


def configure_logging() -> None:
    debug = os.getenv("DEBUG", "").lower() in {"1", "true", "yes", "on"}
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | request_id=%(request_id)s | %(message)s"
        )
    )
    logging.basicConfig(
        handlers=[handler], level=logging.DEBUG if debug else logging.INFO, force=True
    )
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(logging.DEBUG if debug else logging.INFO)


def decision_model_timeout() -> float | Timeout:
    seconds = float(
        os.getenv("DECISION_TIMEOUT_SECONDS", str(DEFAULT_DECISION_TIMEOUT_SECONDS))
    )
    if seconds == -1:
        return Timeout(None)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("DECISION_TIMEOUT_SECONDS must be -1 or a positive number")
    return seconds


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv(Path.cwd() / ".env")
    configure_logging()
    logger.info("Application starting")
    app.state.decision_model_client = None
    if os.getenv("TYPESAFE_API_KEY"):
        timeout = decision_model_timeout()
        base_url = os.getenv("TYPESAFE_BASE_URL")
        logger.info(
            "Decision model client configured endpoint=%s timeout_seconds=%s",
            "custom" if base_url else "sdk-default",
            "disabled" if isinstance(timeout, Timeout) else timeout,
        )
        try:
            async with AsyncTypeSafeClient(
                api_key=os.environ["TYPESAFE_API_KEY"],
                base_url=base_url,
                retry=RetryPolicy(max_retries=0),
                timeout=timeout,
            ) as client:
                app.state.decision_model_client = client
                yield
        finally:
            logger.info("Application stopping")
    else:
        logger.info(
            "Decision model client not configured; deterministic fallback enabled"
        )
        try:
            yield
        finally:
            logger.info("Application stopping")


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    supplied_id = request.headers.get("x-request-id", "")
    request_id = (
        "".join(char for char in supplied_id if char.isalnum() or char in "-_")[:64]
        or uuid.uuid4().hex
    )
    started = time.perf_counter()
    token = request_id_context.set(request_id)
    try:
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "Request failed method=%s path=%s duration_ms=%.1f",
                request.method,
                request.url.path,
                elapsed_ms,
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["x-request-id"] = request_id
        logger.info(
            "Request completed method=%s path=%s status=%s duration_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response
    finally:
        request_id_context.reset(token)


@app.get("/")
async def details() -> dict:
    logger.debug("Snake details requested")
    return {
        "apiversion": "1",
        "author": config.AUTHOR,
        "color": config.COLOR,
        "head": config.HEAD,
        "tail": config.TAIL,
    }


@app.post("/start")
async def start(state: dict | None = None) -> dict:
    logger.info(
        "Game started game_id=%s",
        (state or {}).get("game", {}).get("id", "unknown"),
    )
    return {}


@app.post("/end")
async def end(state: dict | None = None) -> dict:
    logger.info(
        "Game ended game_id=%s turn=%s",
        (state or {}).get("game", {}).get("id", "unknown"),
        (state or {}).get("turn", "unknown"),
    )
    return {}


@app.post("/move")
async def move(state: dict) -> dict[str, str]:
    options = candidates(state)
    safe = rank(state, [o for o in options if o["safe"]])
    fallback = safe[0]["move"] if safe else rank(state, options)[0]["move"]
    client = getattr(app.state, "decision_model_client", None)
    game_id = state.get("game", {}).get("id", "unknown")
    turn = state.get("turn", "unknown")
    logger.info(
        "Move evaluated game_id=%s turn=%s safe_moves=%s fallback=%s",
        game_id,
        turn,
        ",".join(option["move"] for option in safe) or "none",
        fallback,
    )
    if client is not None and len(safe) > 1:
        compact, question = decision_model_input(state, safe)
        started = time.perf_counter()
        try:
            result = await client.system_one(
                compact,
                {"move": question},
            )
            chosen = result.choices["move"].choice
            if chosen in {option["move"] for option in safe}:
                logger.info(
                    "Decision model move selected game_id=%s turn=%s move=%s duration_ms=%.1f",
                    game_id,
                    turn,
                    chosen,
                    (time.perf_counter() - started) * 1000,
                )
                return {"move": chosen}
            logger.warning(
                "Decision model returned unavailable move game_id=%s turn=%s move=%s; using fallback=%s",
                game_id,
                turn,
                chosen,
                fallback,
            )
        except Exception:
            logger.exception(
                "Decision model move failed game_id=%s turn=%s duration_ms=%.1f; using fallback=%s",
                game_id,
                turn,
                (time.perf_counter() - started) * 1000,
                fallback,
            )
    else:
        reason = (
            "no_decision_model_client"
            if client is None
            else f"safe_move_count_{len(safe)}"
        )
        logger.info(
            "Deterministic move selected game_id=%s turn=%s move=%s reason=%s",
            game_id,
            turn,
            fallback,
            reason,
        )
    return {"move": fallback}
