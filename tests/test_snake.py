import io
import logging
from types import SimpleNamespace

from fastapi.testclient import TestClient

from deciding_denise.app import RequestIdFilter, app


def state(
    *,
    head=(2, 2),
    body=None,
    health=80,
    food=None,
    hazards=None,
    enemies=None,
    width=5,
    height=5,
):
    body = body or [head, (2, 1), (2, 0)]
    snake = {
        "id": "me",
        "head": {"x": head[0], "y": head[1]},
        "body": [{"x": x, "y": y} for x, y in body],
        "length": len(body),
        "health": health,
    }
    return {
        "game": {
            "id": "game",
            "timeout": 500,
            "ruleset": {"name": "standard", "settings": {"hazardDamagePerTurn": 14}},
        },
        "turn": 1,
        "board": {
            "width": width,
            "height": height,
            "food": [{"x": x, "y": y} for x, y in (food or [])],
            "hazards": [{"x": x, "y": y} for x, y in (hazards or [])],
            "snakes": [snake] + (enemies or []),
        },
        "you": snake,
    }


def enemy(head, length=3):
    return {
        "id": "enemy",
        "head": {"x": head[0], "y": head[1]},
        "body": [{"x": head[0], "y": head[1]}] * length,
        "length": length,
        "health": 80,
    }


class FakeDecisionModel:
    def __init__(self, choice=None, error=None):
        self.choice = choice
        self.error = error
        self.calls = []

    async def system_one(self, state, questions, **kwargs):
        self.calls.append((state, questions, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(choices={"move": SimpleNamespace(choice=self.choice)})


def test_webhooks_and_terminal_turn():
    with TestClient(app) as client:
        assert client.get("/").json()["apiversion"] == "1"
        assert client.post("/start", json=state()).status_code == 200
        response = client.post("/move", json=state())
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"move": "up"}
        assert client.post("/end", json=state()).status_code == 200


def test_solo_move_avoids_wall_body_and_fatal_hazard():
    board = state(
        head=(0, 2),
        body=[(0, 2), (0, 1), (1, 1)],
        health=10,
        food=[(1, 2)],
        hazards=[(0, 3)],
    )
    with TestClient(app) as client:
        response = client.post("/move", json=board)
    assert response.json() == {"move": "right"}


def test_request_id_appears_in_logs():
    with TestClient(app) as client:
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.addFilter(RequestIdFilter())
        handler.setFormatter(logging.Formatter("request_id=%(request_id)s %(message)s"))
        logging.getLogger().addHandler(handler)
        try:
            response = client.get("/", headers={"x-request-id": "test-123"})
        finally:
            logging.getLogger().removeHandler(handler)
        assert response.headers["x-request-id"] == "test-123"
        assert "Request completed" in output.getvalue()
        assert "request_id=test-123" in output.getvalue()


def test_hazard_and_starvation():
    from deciding_denise.hybrid import simulate

    board = state(health=15, hazards=[(2, 3)], enemies=[enemy((4, 4))])
    assert simulate(board, "up", "left")["me"]["reason"] == "hazard"
    starving = state(health=1, enemies=[enemy((4, 4))])
    assert simulate(starving, "left", "left")["me"]["reason"] == "health"


def test_invalid_choice_has_no_fallback():
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.decision_model_client = FakeDecisionModel("invalid")
        response = client.post("/move", json=state(enemies=[enemy((4, 4))]))
        assert response.status_code == 500


def test_hybrid_preserves_conditional_head_contest():
    from deciding_denise.hybrid import analyze_turn

    board = state(head=(2, 2), body=[(2, 2), (2, 1), (2, 0)], enemies=[enemy((4, 2))])
    analysis = analyze_turn(board)
    assert "right" in analysis.offered
    assert analysis.cards["right"]["surviving_replies"] == 3
    assert (
        next(o for o in analysis.cards["right"]["outcomes"] if o["reply"] == "left")[
            "our_reason"
        ]
        == "head_to_head"
    )


def test_hybrid_food_growth_and_one_health():
    from deciding_denise.hybrid import simulate

    board = state(health=1, food=[(2, 3)], enemies=[enemy((4, 4))])
    result = simulate(board, "up", "left")["me"]
    assert result["reason"] is None
    assert result["health"] == 100
    assert result["body"] == [(2, 3), (2, 2), (2, 1), (2, 1)]


def test_hybrid_endpoint_model_controls_choice(monkeypatch):
    monkeypatch.delenv("DENISE_POLICY", raising=False)
    with TestClient(app) as client:
        fake = FakeDecisionModel("right")
        app.state.decision_model_client = fake
        result = client.post("/move", json=state(enemies=[enemy((4, 4))]))
        assert result.status_code == 200
        assert result.json() == {"move": "right"}
        assert len(fake.calls[0][0]["cards"]) > 1


def test_hybrid_model_failure_has_no_fallback(monkeypatch):
    monkeypatch.setenv("DENISE_POLICY", "hybrid")
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.decision_model_client = FakeDecisionModel(error=TimeoutError())
        result = client.post("/move", json=state(enemies=[enemy((4, 4))]))
        assert result.status_code == 500


def test_simulator_matches_cli_engine_turns():
    import json
    from pathlib import Path

    from deciding_denise.hybrid import simulate

    cases = json.loads(
        (Path(__file__).parent / "fixtures" / "engine_turns.json").read_text()
    )
    for case in cases:
        board = case["board"]
        state = {
            "game": {
                "ruleset": {
                    "name": "standard",
                    "version": "cli",
                    "settings": {"hazardDamagePerTurn": 14},
                }
            },
            "board": board,
            "you": board["snakes"][0],
        }
        predicted = simulate(state, *case["moves"])
        for observed in case["next"]:
            actual = predicted[observed["id"]]
            assert actual["health"] == observed["health"], case["turn"]
            assert actual["length"] == observed["length"], case["turn"]
            assert actual["body"] == [(p["x"], p["y"]) for p in observed["body"]], case[
                "turn"
            ]


def test_hybrid_equal_head_contest_and_simultaneous_death():
    from deciding_denise.hybrid import simulate

    rival = {
        "id": "enemy",
        "head": {"x": 4, "y": 2},
        "body": [{"x": 4, "y": 2}, {"x": 4, "y": 1}, {"x": 4, "y": 0}],
        "length": 3,
        "health": 80,
    }
    board = state(head=(2, 2), enemies=[rival])
    outcome = simulate(board, "right", "left")
    assert outcome["me"]["reason"] == "head_to_head"
    assert outcome["enemy"]["reason"] == "head_to_head"


def test_hybrid_vacating_and_stacked_tails():
    from deciding_denise.hybrid import simulate

    rival = enemy((4, 4))
    vacating = state(
        head=(2, 2), body=[(2, 2), (2, 1), (1, 1), (1, 2)], enemies=[rival]
    )
    assert simulate(vacating, "left", "left")["me"]["reason"] is None
    stacked = state(head=(2, 2), body=[(2, 2), (2, 1), (1, 2), (1, 2)], enemies=[rival])
    assert simulate(stacked, "left", "left")["me"]["reason"] == "body"


def test_hybrid_food_on_hazard_avoids_damage():
    from deciding_denise.hybrid import simulate

    board = state(health=1, food=[(2, 3)], hazards=[(2, 3)], enemies=[enemy((4, 4))])
    result = simulate(board, "up", "left")["me"]
    assert result["reason"] is None
    assert result["health"] == 100


def test_hybrid_eliminated_opponent_body_does_not_collide():
    from deciding_denise.hybrid import simulate

    rival = {
        "id": "enemy",
        "head": {"x": 4, "y": 2},
        "body": [{"x": 4, "y": 2}, {"x": 3, "y": 2}, {"x": 2, "y": 2}],
        "length": 3,
        "health": 80,
    }
    board = state(head=(3, 1), body=[(3, 1), (2, 1), (1, 1)], enemies=[rival])
    outcome = simulate(board, "up", "right")
    assert outcome["enemy"]["reason"] == "wall"
    assert outcome["me"]["reason"] is None
