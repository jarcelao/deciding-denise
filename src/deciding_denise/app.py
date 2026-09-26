"""Battlesnake HTTP server."""

import inspect
import logging
import math
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from httpx2 import Timeout
from loguru import logger
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from . import config
from .brain import candidates, decision_model_input, rank

DEFAULT_DECISION_TIMEOUT_SECONDS = 0.25


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def configure_logging() -> None:
    debug = os.getenv("DEBUG", "").lower() in {"1", "true", "yes", "on"}
    level = "DEBUG" if debug else "INFO"
    logger.remove()
    logger.configure(extra={"request_id": "-"})
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
            "<dim>|</dim> <level>{level:<8}</level> <dim>|</dim> "
            "<cyan>request_id={extra[request_id]}</cyan> "
            "<dim>|</dim> {message}"
        ),
        backtrace=False,
        diagnose=False,
    )
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.NOTSET, force=True)
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
            "Decision model client configured endpoint={} timeout_seconds={}",
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
    with logger.contextualize(request_id=request_id):
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "Request failed method={} path={} duration_ms={:.1f}",
                request.method,
                request.url.path,
                elapsed_ms,
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["x-request-id"] = request_id
        logger.info(
            "Request completed method={} path={} status={} duration_ms={:.1f}",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response


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
        "Game started game_id={}",
        (state or {}).get("game", {}).get("id", "unknown"),
    )
    return {}


@app.post("/end")
async def end(state: dict | None = None) -> dict:
    logger.info(
        "Game ended game_id={} turn={}",
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
        "Move evaluated game_id={} turn={} safe_moves={} fallback={}",
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
                    "Decision model move selected game_id={} turn={} move={} duration_ms={:.1f}",
                    game_id,
                    turn,
                    chosen,
                    (time.perf_counter() - started) * 1000,
                )
                return {"move": chosen}
            logger.warning(
                "Decision model returned unavailable move game_id={} turn={} move={}; using fallback={}",
                game_id,
                turn,
                chosen,
                fallback,
            )
        except Exception:  # noqa: BLE001 - any model failure must use the safe fallback
            logger.exception(
                "Decision model move failed game_id={} turn={} duration_ms={:.1f}; using fallback={}",
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
            "Deterministic move selected game_id={} turn={} move={} reason={}",
            game_id,
            turn,
            fallback,
            reason,
        )
    return {"move": fallback}
