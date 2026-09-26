"""Battlesnake HTTP server."""

import asyncio
import logging
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
from .hybrid import analyze_turn, deterministic_move, model_input, solo_move

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv(Path.cwd() / ".env")
    configure_logging()
    logger.info("Application starting")
    app.state.decision_model_client = None
    if os.getenv("TYPESAFE_API_KEY"):
        base_url = os.getenv("TYPESAFE_BASE_URL")
        logger.info(
            "Decision model client configured endpoint=%s; game deadline enforced per move",
            "custom" if base_url else "sdk-default",
        )
        try:
            async with AsyncTypeSafeClient(
                api_key=os.environ["TYPESAFE_API_KEY"],
                base_url=base_url,
                retry=RetryPolicy(max_retries=0),
                timeout=Timeout(None),
            ) as client:
                app.state.decision_model_client = client
                yield
        finally:
            logger.info("Application stopping")
    else:
        logger.info("Decision model client not configured")
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
    request.state.started_monotonic = time.monotonic()
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
async def move(state: dict, request: Request) -> dict[str, str]:
    policy = os.getenv("DENISE_POLICY", "hybrid")
    if policy not in {"hybrid", "deterministic"}:
        raise ValueError(f"Unknown DENISE_POLICY: {policy}")
    if len(state["board"]["snakes"]) == 1:
        return {"move": solo_move(state)}
    started = request.state.started_monotonic
    timeout_ms = state.get("game", {}).get("timeout", 500)
    reserve_ms = float(os.getenv("DENISE_TRANSPORT_RESERVE_MS", "125"))
    cutoff = started + max(0, timeout_ms - reserve_ms) / 1000
    try:
        analysis = analyze_turn(state, cutoff)
        if policy == "deterministic":
            return {"move": deterministic_move(analysis)}
        if len(analysis.offered) == 1:
            return {"move": analysis.offered[0]}
        if cutoff - time.monotonic() < 0.25:
            raise TimeoutError("less than 250 ms remains for Jev")
        client = getattr(app.state, "decision_model_client", None)
        if client is None:
            raise RuntimeError("Jev client unavailable")
        compact, question = model_input(state, analysis)
        async with asyncio.timeout_at(
            asyncio.get_running_loop().time() + max(0, cutoff - time.monotonic())
        ):
            result = await client.system_one(compact, {"move": question})
        answer = result.choices["move"]
        chosen = answer.choice
        if chosen not in analysis.offered:
            raise ValueError(f"Jev chose unavailable move: {chosen}")
        usage = getattr(result, "usage", None)
        logger.info(
            "Hybrid move selected game_id=%s turn=%s move=%s analysis_ms=%.1f confidence=%s probabilities=%s model=%s input_tokens=%s output_tokens=%s",
            state["game"]["id"],
            state["turn"],
            chosen,
            analysis.elapsed_ms,
            getattr(answer, "confidence", None),
            getattr(answer, "probabilities", None),
            getattr(result, "model", None),
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
        )
        return {"move": chosen}
    except Exception:
        logger.exception("Experimental policy failed policy=%s", policy)
        raise
