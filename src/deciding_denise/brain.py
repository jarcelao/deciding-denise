"""Deterministic move safety and decision model input."""

from collections import deque

from typesafe_sdk import Choice

DIRECTIONS = {"up": (0, 1), "right": (1, 0), "down": (0, -1), "left": (-1, 0)}


def candidates(state: dict) -> list[dict]:
    board = state["board"]
    you = state["you"]
    head = you["head"]
    food = {(p["x"], p["y"]) for p in board["food"]}
    hazards = {(p["x"], p["y"]) for p in board.get("hazards", [])}
    occupied = {(p["x"], p["y"]) for snake in board["snakes"] for p in snake["body"]}
    tail = you["body"][-1]
    tail_pos = (tail["x"], tail["y"])
    # A single, unstacked tail vacates unless we eat on this move.
    tail_vacates = len(you["body"]) > 1 and you["body"][-2] != tail
    threatened = set()
    for snake in board["snakes"]:
        if snake["id"] != you["id"] and snake["length"] >= you["length"]:
            h = snake["head"]
            threatened.update(
                (h["x"] + dx, h["y"] + dy) for dx, dy in DIRECTIONS.values()
            )
    damage = state["game"]["ruleset"].get("settings", {}).get("hazardDamagePerTurn", 14)
    results = []
    for direction, (dx, dy) in DIRECTIONS.items():
        pos = (head["x"] + dx, head["y"] + dy)
        in_bounds = 0 <= pos[0] < board["width"] and 0 <= pos[1] < board["height"]
        blocked = pos in occupied and not (
            pos == tail_pos and tail_vacates and pos not in food
        )
        hazard = pos in hazards
        fatal_health = pos not in food and you["health"] <= 1 + (
            damage if hazard else 0
        )
        safe = in_bounds and not blocked and not fatal_health and pos not in threatened
        results.append(
            {
                "move": direction,
                "position": pos,
                "safe": safe,
                "food": pos in food,
                "hazard": hazard,
                "threatened": pos in threatened,
                "blocked": blocked,
                "in_bounds": in_bounds,
            }
        )
    return results


def rank(state: dict, options: list[dict]) -> list[dict]:
    board = state["board"]
    food = [(p["x"], p["y"]) for p in board["food"]]
    occupied = {(p["x"], p["y"]) for snake in board["snakes"] for p in snake["body"]}

    def space(start: tuple[int, int]) -> int:
        seen = {start}
        queue = deque([start])
        while queue:
            x, y = queue.popleft()
            for dx, dy in DIRECTIONS.values():
                point = (x + dx, y + dy)
                if (
                    point not in seen
                    and point not in occupied
                    and 0 <= point[0] < board["width"]
                    and 0 <= point[1] < board["height"]
                ):
                    seen.add(point)
                    queue.append(point)
        return len(seen)

    def key(option: dict) -> tuple:
        x, y = option["position"]
        distance = min(
            (abs(x - fx) + abs(y - fy) for fx, fy in food),
            default=board["width"] + board["height"],
        )
        urgent = state["you"]["health"] <= 35
        return (
            not option["in_bounds"],
            option["blocked"],
            option["threatened"],
            not option["safe"],
            option["hazard"],
            distance if urgent else 0,
            -space((x, y)) if option["in_bounds"] else 0,
            distance,
            list(DIRECTIONS).index(option["move"]),
        )

    return sorted(options, key=key)


def decision_model_input(state: dict, safe: list[dict]) -> tuple[dict, Choice]:
    board = state["board"]
    you = state["you"]
    compact = {
        "board": {"width": board["width"], "height": board["height"]},
        "you": {"head": you["head"], "health": you["health"], "length": you["length"]},
        "food": board["food"],
        "hazards": board.get("hazards", []),
        "snakes": [
            {"head": s["head"], "length": s["length"], "body": s["body"]}
            for s in board["snakes"]
        ],
        "available_moves": [o["move"] for o in safe],
    }
    criteria = {
        o[
            "move"
        ]: f"Move to {o['position']}; {'food' if o['food'] else 'no food'}; {'hazard' if o['hazard'] else 'no hazard'}"
        for o in safe
    }
    question = Choice(
        instructions="Choose the move most likely to keep this Battlesnake alive and well positioned. Prefer food when health is low. Only choose an available move.",
        criteria=criteria,
    )
    return compact, question
