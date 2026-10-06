def main() -> None:
    import uvicorn

    from .constants import HOST, PORT

    uvicorn.run("deciding_denise.app:app", host=HOST, port=PORT, log_config=None)
