"""One-turn standard-duel evidence and experimental policies."""

from collections import deque
from dataclasses import dataclass
from time import monotonic
from typing import TypedDict

from typesafe_sdk import Choice

DIRECTIONS = {"up": (0, 1), "right": (1, 0), "down": (0, -1), "left": (-1, 0)}


class SnakeOutcome(TypedDict):
    head: tuple[int, int]
    body: list[tuple[int, int]]
    length: int
    health: int
    ate: bool
    reason: str | None


class OutcomeEvidence(TypedDict):
    reply: str
    our_reason: str | None
    our_health: int
    our_length: int
    enemy_reason: str | None
    head_contest: bool
    length_advantage: int | None
    mobility: dict | None
    food: dict | None


class MoveEvidence(TypedDict):
    destination: list[int]
    outcomes: list[OutcomeEvidence]
    surviving_replies: int


def point(value) -> tuple[int, int]:
    return (value["x"], value["y"])


def solo_move(state):
    board, snake = state["board"], state["you"]
    food = {point(p) for p in board["food"]}
    hazards = {point(p) for p in board.get("hazards", [])}
    body = [point(p) for p in snake["body"]]
    occupied = set(body[:-1])
    choices = []
    damage = state["game"]["ruleset"].get("settings", {}).get("hazardDamagePerTurn", 14)
    for index, (move, (dx, dy)) in enumerate(DIRECTIONS.items()):
        dest = (snake["head"]["x"] + dx, snake["head"]["y"] + dy)
        if not (0 <= dest[0] < board["width"] and 0 <= dest[1] < board["height"]):
            continue
        if dest in occupied or (dest == body[-1] and dest in food):
            continue
        fatal = dest not in food and snake["health"] <= 1 + (
            damage if dest in hazards else 0
        )
        choices.append((fatal, dest not in food, dest in hazards, index, move))
    return min(choices)[-1] if choices else next(iter(DIRECTIONS))


def supported(state):
    game = state["game"]
    rules = game["ruleset"]
    settings = rules.get("settings", {})
    snakes = state["board"]["snakes"]
    return (
        rules.get("name") == "standard"
        and rules.get("version", "cli") == "cli"
        and game.get("map", "standard") == "standard"
        and len(snakes) == 2
        and not settings.get("wrapped", False)
        and settings.get("hazardDamagePerTurn", 14) >= 0
        and len({s["id"] for s in snakes}) == 2
        and state["you"]["id"] in {s["id"] for s in snakes}
    )


def simulate(state, our_move, their_move) -> dict[str, SnakeOutcome]:
    """Resolve one simultaneous standard-rules turn without spawning future food."""
    board = state["board"]
    snakes = board["snakes"]
    you_id = state["you"]["id"]
    moves = {
        you_id: our_move,
        next(s["id"] for s in snakes if s["id"] != you_id): their_move,
    }
    food = {point(p) for p in board["food"]}
    hazards = {point(p) for p in board.get("hazards", [])}
    damage = state["game"]["ruleset"].get("settings", {}).get("hazardDamagePerTurn", 14)
    result: dict[str, SnakeOutcome] = {}
    for snake in snakes:
        sid = snake["id"]
        dx, dy = DIRECTIONS[moves[sid]]
        head = point(snake["head"])
        dest = (head[0] + dx, head[1] + dy)
        ate = dest in food
        body = [dest, *[point(p) for p in snake["body"][:-1]]]
        if ate:
            body.append(body[-1])
        health = snake["health"] - 1
        hazard_death = dest in hazards and not ate and health - damage <= 0
        if dest in hazards and not ate:
            health = max(0, health - damage)
        if ate:
            health = 100
        result[sid] = {
            "head": dest,
            "body": body,
            "length": len(body),
            "health": health,
            "ate": ate,
            "reason": "hazard" if hazard_death else None,
        }
    for snake in snakes:
        sid = snake["id"]
        item = result[sid]
        x, y = item["head"]
        if item["reason"]:
            continue
        if item["health"] <= 0:
            item["reason"] = "health"
        elif not (0 <= x < board["width"] and 0 <= y < board["height"]):
            item["reason"] = "wall"
    prelim_alive = {sid for sid, item in result.items() if item["reason"] is None}
    collision_reasons = {}
    for sid in prelim_alive:
        item = result[sid]
        if item["head"] in item["body"][1:] or any(
            item["head"] in result[other_id]["body"][1:]
            for other_id in prelim_alive
            if other_id != sid
        ):
            collision_reasons[sid] = "body"
        elif any(
            item["head"] == result[other_id]["head"]
            and item["length"] <= result[other_id]["length"]
            for other_id in prelim_alive
            if other_id != sid
        ):
            collision_reasons[sid] = "head_to_head"
    for sid, reason in collision_reasons.items():
        result[sid]["reason"] = reason
    return result


def mobility(board, result, you_id):
    ours = result[you_id]
    if ours["reason"]:
        return None
    occupied = {
        p for s in result.values() if s["reason"] is None for p in s["body"][1:]
    }
    occupied.update(
        s["head"] for sid, s in result.items() if sid != you_id and s["reason"] is None
    )
    head = ours["head"]
    seen = {head}
    queue = deque([head])
    exits = 0
    while queue:
        x, y = queue.popleft()
        for dx, dy in DIRECTIONS.values():
            nxt = (x + dx, y + dy)
            if (
                nxt in seen
                or nxt in occupied
                or not (0 <= nxt[0] < board["width"] and 0 <= nxt[1] < board["height"])
            ):
                continue
            if (x, y) == head:
                exits += 1
            seen.add(nxt)
            queue.append(nxt)
    return {
        "reachable": len(seen),
        "space_minus_length": len(seen) - ours["length"],
        "exits": exits,
    }


def food_route(board, result, you_id, damage):
    ours = result[you_id]
    if ours["reason"]:
        return None
    foods = {point(p) for p in board["food"]} - {
        s["head"] for s in result.values() if s["ate"]
    }
    if not foods:
        return {"status": "no_visible_food"}
    occupied = {
        p for s in result.values() if s["reason"] is None for p in s["body"][1:]
    }
    occupied.update(
        s["head"] for sid, s in result.items() if sid != you_id and s["reason"] is None
    )
    hazards = {point(p) for p in board.get("hazards", [])}
    queue = deque([(ours["head"], 0, 0)])
    seen = {ours["head"]}
    while queue:
        pos, distance, hazard_steps = queue.popleft()
        if pos in foods:
            return {
                "status": "reachable_static",
                "distance": distance,
                "pre_food_health_margin": ours["health"]
                - distance
                - hazard_steps * damage,
                "hazard_steps": hazard_steps,
            }
        for dx, dy in DIRECTIONS.values():
            nxt = (pos[0] + dx, pos[1] + dy)
            if (
                nxt in seen
                or nxt in occupied
                or not (0 <= nxt[0] < board["width"] and 0 <= nxt[1] < board["height"])
            ):
                continue
            seen.add(nxt)
            queue.append(
                (
                    nxt,
                    distance + 1,
                    hazard_steps + (nxt in hazards and nxt not in foods),
                )
            )
    return {"status": "unreachable_static"}


@dataclass
class TurnAnalysis:
    cards: dict[str, MoveEvidence]
    offered: list[str]
    elapsed_ms: float


def analyze_turn(state, deadline=None):
    if not supported(state):
        raise ValueError("unsupported game: expected standard non-wrapped duel")
    started = monotonic()
    you_id = state["you"]["id"]
    enemy_id = next(s["id"] for s in state["board"]["snakes"] if s["id"] != you_id)
    damage = state["game"]["ruleset"].get("settings", {}).get("hazardDamagePerTurn", 14)
    cards: dict[str, MoveEvidence] = {}
    for move, (dx, dy) in DIRECTIONS.items():
        outcomes: list[OutcomeEvidence] = []
        for reply in DIRECTIONS:
            if deadline is not None and monotonic() >= deadline:
                raise TimeoutError("analysis deadline")
            result = simulate(state, move, reply)
            ours, enemy = result[you_id], result[enemy_id]
            outcomes.append(
                {
                    "reply": reply,
                    "our_reason": ours["reason"],
                    "our_health": ours["health"],
                    "our_length": ours["length"],
                    "enemy_reason": enemy["reason"],
                    "head_contest": ours["head"] == enemy["head"],
                    "length_advantage": ours["length"] - enemy["length"]
                    if ours["head"] == enemy["head"]
                    else None,
                    "mobility": mobility(state["board"], result, you_id),
                    "food": food_route(state["board"], result, you_id, damage),
                }
            )
        cards[move] = {
            "destination": [
                state["you"]["head"]["x"] + dx,
                state["you"]["head"]["y"] + dy,
            ],
            "outcomes": outcomes,
            "surviving_replies": sum(o["our_reason"] is None for o in outcomes),
        }
    offered = [
        move for move, card in cards.items() if card["surviving_replies"]
    ] or list(DIRECTIONS)
    return TurnAnalysis(cards, offered, (monotonic() - started) * 1000)


def model_input(state, analysis):
    board = state["board"]
    compact = {
        "turn": state["turn"],
        "rules": state["game"]["ruleset"],
        "board": {"width": board["width"], "height": board["height"]},
        "you_id": state["you"]["id"],
        "snakes": [
            {
                "id": s["id"],
                "health": s["health"],
                "length": s["length"],
                "body": s["body"],
            }
            for s in board["snakes"]
        ],
        "food": board["food"],
        "hazards": board.get("hazards", []),
        "cards": {m: analysis.cards[m] for m in analysis.offered},
        "assumptions": "All four opponent replies are possibilities, not probabilities. Mobility and food routes use static post-turn occupancy; future movement and food spawning are unknown. A pre-food margin of zero can be viable on arrival.",
    }
    question = Choice(
        instructions="Which offered move gives Denise the best chance to win this duel? Use exact immediate outcomes and approximate positional evidence. Choose only an offered direction.",
        criteria={m: f"Move {m}; evidence is in cards.{m}" for m in analysis.offered},
    )
    return compact, question


def deterministic_move(analysis):
    """Experimental same-evidence control; never used as a model fallback."""

    def key(move):
        outcomes = analysis.cards[move]["outcomes"]
        survivors = [o for o in outcomes if o["our_reason"] is None]
        return (
            len(survivors),
            min((o["mobility"]["space_minus_length"] for o in survivors), default=-999),
            max(
                (
                    o["food"].get("pre_food_health_margin", -999)
                    for o in survivors
                    if o["food"]
                ),
                default=-999,
            ),
            -list(DIRECTIONS).index(move),
        )

    return max(analysis.offered, key=key)
