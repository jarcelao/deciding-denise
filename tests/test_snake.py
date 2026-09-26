import io
import logging
from types import SimpleNamespace

from fastapi.testclient import TestClient

from deciding_denise.app import RequestIdFilter, app
from deciding_denise.brain import candidates


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


def test_webhooks_and_decision_model_choice():
    with TestClient(app) as client:
        fake = FakeDecisionModel("left")
        app.state.decision_model_client = fake
        assert client.get("/").json()["apiversion"] == "1"
        assert client.post("/start", json=state()).status_code == 200
        response = client.post("/move", json=state())
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"move": "left"}
        assert "left" in fake.calls[0][1]["move"].criteria
        assert client.post("/end", json=state()).status_code == 200


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


def test_walls_bodies_and_head_threats():
    board = state(head=(0, 2), body=[(0, 2), (0, 1), (1, 1)], enemies=[enemy((1, 3))])
    moves = {o["move"]: o for o in candidates(board)}
    assert not moves["left"]["safe"]
    assert not moves["down"]["safe"]
    assert not moves["up"]["safe"]
    assert moves["right"]["safe"] is False  # Enemy can move into (1, 2).


def test_hazard_and_starvation():
    board = state(health=15, hazards=[(2, 3)])
    moves = {o["move"]: o for o in candidates(board)}
    assert not moves["up"]["safe"]
    assert moves["left"]["safe"]
    assert not {o["move"]: o for o in candidates(state(health=1))}["left"]["safe"]


def test_invalid_choice_and_error_fall_back():
    with TestClient(app) as client:
        for fake in [
            FakeDecisionModel("down"),
            FakeDecisionModel(error=TimeoutError()),
        ]:
            app.state.decision_model_client = fake
            result = client.post("/move", json=state()).json()
            assert result["move"] in {"up", "left", "right"}
            assert result["move"] != "down"
        app.state.decision_model_client = None
        assert client.post("/move", json=state()).json()["move"] in {
            "up",
            "left",
            "right",
        }
