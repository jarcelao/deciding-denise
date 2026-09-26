# Deciding Denise

A Battlesnake that asks a typed decision model (e.g. Jev) to choose among moves that pass an immediate safety check. 

## Run

Requires Python 3.12 and `uv`.

```sh
uv sync
cp .env.example .env
uv run deciding-denise
```

Set your key in `.env` in the directory where you run the server:

```dotenv
TYPESAFE_API_KEY=your_key
# TYPESAFE_BASE_URL=https://your-compatible-api-root
# DECISION_TIMEOUT_SECONDS=0.25
# DEBUG=1
```

The server listens on port 8000. Environment variables take precedence over `.env` values.

Leave `TYPESAFE_API_KEY` unset to run with only the deterministic fallback. Set `TYPESAFE_BASE_URL` to a TypeSafe-compatible API root when using another provider; the SDK default is TypeSafe's API.

`DECISION_TIMEOUT_SECONDS` defaults to `0.25` and accepts a positive number of seconds. Set it to `-1` to disable the remote-model timeout.

Logs go to standard error at `INFO` by default. Set `DEBUG=1` to include debug logs.

In Battlesnake, point the snake's URL to your publicly reachable server. For a local smoke check:

```sh
curl http://localhost:8000/
```

Run tests with `uv run pytest`.

This prototype targets standard boards. It checks immediate collisions, hazard damage, and possible head-to-head losses, then lets Jev pick among safe moves. It does not simulate future turns.
