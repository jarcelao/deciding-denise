# Deciding Denise

A Battlesnake that chooses moves made by a typed decision model (e.g. Jev).

## Run

Requires Python 3.13 and `uv`.

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

Run tests with `uv run pytest`.

This prototype targets standard boards. It checks immediate collisions, hazard damage, and possible head-to-head losses, then lets Jev pick among safe moves. It does not simulate future turns.
