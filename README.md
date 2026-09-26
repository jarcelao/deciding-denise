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
# DEBUG=1
```

Run tests with `uv run pytest`.
