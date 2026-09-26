"""Battlesnake decision model."""


def main() -> None:
    import uvicorn

    uvicorn.run(
        "deciding_denise.app:app", host="0.0.0.0", port=8000, log_config=None
    )
