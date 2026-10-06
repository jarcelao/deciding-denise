import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


# --- Env-configurable ---
DEBUG = os.getenv("DEBUG", "").lower() in {"1", "true", "yes", "on"}
TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY")
TYPESAFE_BASE_URL = os.getenv("TYPESAFE_BASE_URL")
TYPESAFE_MODEL = os.getenv("TYPESAFE_MODEL")
TRANSPORT_RESERVE_MS = _env_float(
    "DENISE_TRANSPORT_RESERVE_MS", 125
)  # time held back from the game's timeout for network transport.
MODEL_MIN_MS = _env_float(
    "DENISE_MODEL_MIN_MS", 250
)  # analysis leaves the decision model at least this long.

# --- Snake identity ---
AUTHOR = "jarcelao"
COLOR = "#7345B7"
HEAD = "beluga"
TAIL = "present"

# --- Server ---
HOST = "0.0.0.0"
PORT = 8000
REQUEST_ID_MAX_LEN = 64

# --- Battlesnake rules ---
STEPS = {"up": (0, 1), "right": (1, 0), "down": (0, -1), "left": (-1, 0)}
MAX_HEALTH = 100
DEFAULT_HAZARD_DAMAGE = 14
DEFAULT_TIMEOUT_MS = 500
SUPPORTED_RULESET_VERSION = re.compile(r"v1\.\d+\.\d+\Z")

# --- Search ---
SEARCH_MARGIN_MS = 100  # search stops this early; must exceed OS scheduling stalls
SEARCH_DEPTH = 8  # iterative deepening stops at the deadline, usually well before this
WIN, LOSS, DRAW = 1000, -1000, -100
PROVEN_BAND = 100  # values within this of WIN/LOSS are forced outcomes
FORCED_WIN = WIN - PROVEN_BAND
FORCED_LOSS = LOSS + PROVEN_BAND
INF = 10**6

# --- Evaluation weights ---
LENGTH_WEIGHT = 3
FOOD_WEIGHT = 4
TRAPPED_PENALTY = 30  # fewer reachable cells than snake length
TRAPPED_PER_CELL = 5
HUNGRY_HEALTH = 40
HUNGER_WEIGHT = 1  # per missing health, when we reach food first
STARVING_WEIGHT = 3  # per missing health, when we don't
