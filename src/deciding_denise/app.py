import asyncio
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

from fastapi import FastAPI, Request
from httpx2 import Timeout
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from . import constants as c
from .engine import (
    analyze_turn,
    model_input,
    solo_cards,
    solo_model_input,
)

request_id_context: ContextVar[str] = ContextVar("request_id", default="-")
logger = logging.getLogger(__name__)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_context.get()
        return True


class OneLineFormatter(logging.Formatter):
    def formatException(self, ei) -> str:
        return " | ".join(super().formatException(ei).splitlines())


def configure_logging() -> None:
    debug = c.DEBUG
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        OneLineFormatter(
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
    configure_logging()
    logger.info("Application starting")
    app.state.decision_model_client = None
    if c.TYPESAFE_API_KEY:
        base_url, model = c.TYPESAFE_BASE_URL, c.TYPESAFE_MODEL
        logger.info(
            "Decision model client configured endpoint=%s model=%s",
            "custom" if base_url else "sdk-default",
            "custom" if model else "sdk-default",
        )
        try:
            async with AsyncTypeSafeClient(
                api_key=c.TYPESAFE_API_KEY,
                base_url=base_url,
                model=model,
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
        "".join(char for char in supplied_id if char.isalnum() or char in "-_")[
            : c.REQUEST_ID_MAX_LEN
        ]
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
        "author": c.AUTHOR,
        "color": c.COLOR,
        "head": c.HEAD,
        "tail": c.TAIL,
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
    started = request.state.started_monotonic
    timeout_ms = state.get("game", {}).get("timeout", c.DEFAULT_TIMEOUT_MS)
    cutoff = started + max(0, timeout_ms - c.TRANSPORT_RESERVE_MS) / 1000
    fallback = None
    model_min = c.MODEL_MIN_MS / 1000
    depth = 0  # solo games don't search
    try:
        if len(state["board"]["snakes"]) == 1:
            cards = solo_cards(state)
            offered = list(cards)
            compact, question = solo_model_input(state, cards)
            analysis_ms = 0.0
        else:
            analysis = await asyncio.to_thread(analyze_turn, state, cutoff - model_min)
            offered = analysis.offered
            compact, question = model_input(state, analysis)
            analysis_ms = analysis.elapsed_ms
            depth = analysis.depth
            fallback = max(offered, key=lambda m: analysis.cards[m]["search"]["value"])
        if len(offered) == 1:
            return {"move": offered[0]}
        if cutoff - time.monotonic() < model_min:
            if fallback:
                logger.warning(
                    "Analysis overran, using best search move game_id=%s turn=%s move=%s analysis_ms=%.1f search_depth=%s",
                    state["game"]["id"],
                    state["turn"],
                    fallback,
                    analysis_ms,
                    depth,
                )
                return {"move": fallback}
            raise TimeoutError(
                f"less than {c.MODEL_MIN_MS:.0f} ms remains for decision model"
            )
        client = getattr(app.state, "decision_model_client", None)
        if client is None:
            raise RuntimeError("Model client unavailable")
        async with asyncio.timeout_at(
            asyncio.get_running_loop().time() + max(0, cutoff - time.monotonic())
        ):
            result = await client.system_one(compact, {"move": question})
        answer = result.choices["move"]
        chosen = answer.choice
        if chosen not in offered:
            raise ValueError(f"Decision model chose unavailable move: {chosen}")
        usage = getattr(result, "usage", None)
        logger.info(
            "Engine move selected game_id=%s turn=%s move=%s analysis_ms=%.1f search_depth=%s confidence=%s probabilities=%s model=%s input_tokens=%s output_tokens=%s",
            state["game"]["id"],
            state["turn"],
            chosen,
            analysis_ms,
            depth,
            getattr(answer, "confidence", None),
            getattr(answer, "probabilities", None),
            getattr(result, "model", None),
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
        )
        return {"move": chosen}
    except Exception:
        logger.exception("Engine move failed")
        raise
