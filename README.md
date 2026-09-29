# Halal Quantitative Trading Research Platform (V1)

Research platform to test whether a simple, transparent, Sharia-compliant
long-only equity strategy beats SPUS / HLAL after realistic costs — on
Interactive Brokers **paper trading only** in V1.

This is not an AI stock-picker and not a system aimed at high monthly returns.

## Status

Phase 1 (Foundation) in progress: database foundation in place.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and Docker Desktop (WSL 2 engine on Windows).

```sh
uv sync                                   # install dependencies
cp .env.example .env                      # then fill in the HQ_DB_* passwords
docker compose up -d --wait               # start PostgreSQL (listens on 127.0.0.1 only)
uv run python -m halal_quant.db.bootstrap # create roles + schema (safe to re-run)
uv run alembic upgrade head               # apply migrations
```

Database roles (least privilege): `hq_migrator` owns the `hq` schema and is used only by
migrations; `hq_app` reads and writes data but cannot change the schema; `hq_readonly` can
only read.

## Tests

```sh
uv run pytest                                  # unit tests
HQ_RUN_INTEGRATION_TESTS=1 uv run pytest       # also database tests (needs the steps above)
```

## Repository notes

Internal governance docs (requirements, security checklist, compliance
tracker, agent rulebook) and research/dashboard artifacts are kept out of
this public repository — see `private/` locally, which is git-ignored.

## License / access

Personal project. Not investment advice.
