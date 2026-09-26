"""Battlesnake HTTP server."""

import logging
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from httpx2 import Timeout
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from .brain import candidates, jev_input, rank

log = logging.getLogger(__name__)
DEFAULT_DECISION_TIMEOUT_SECONDS = 0.25


def jev_timeout() -> float | Timeout:
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
    app.state.jev_client = None
    if os.getenv("TYPESAFE_API_KEY"):
        async with AsyncTypeSafeClient(
            api_key=os.environ["TYPESAFE_API_KEY"],
            base_url=os.getenv("TYPESAFE_BASE_URL"),
            retry=RetryPolicy(max_retries=0),
            timeout=jev_timeout(),
        ) as client:
            app.state.jev_client = client
            yield
    else:
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def details() -> dict:
    return {"apiversion": "1", "color": "#7345B7", "head": "default", "tail": "default"}


@app.post("/start")
async def start() -> dict:
    return {}


@app.post("/end")
async def end() -> dict:
    return {}


@app.post("/move")
async def move(state: dict) -> dict[str, str]:
    options = candidates(state)
    safe = rank(state, [o for o in options if o["safe"]])
    fallback = safe[0]["move"] if safe else rank(state, options)[0]["move"]
    client = getattr(app.state, "jev_client", None)
    if client is not None and len(safe) > 1:
        compact, question = jev_input(state, safe)
        try:
            result = await client.system_one(
                compact,
                {"move": question},
                timeout=jev_timeout(),
                retry=RetryPolicy(max_retries=0),
            )
            chosen = result.choices["move"].choice
            if chosen in {option["move"] for option in safe}:
                return {"move": chosen}
        except Exception:
            log.warning(
                "Jev move unavailable; using deterministic fallback", exc_info=True
            )
    return {"move": fallback}
