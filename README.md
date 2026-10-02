# Halal Quantitative Trading Research Platform (V1)

Research platform to test whether a simple, transparent, Sharia-compliant
long-only equity strategy beats SPUS / HLAL after realistic costs — on
Interactive Brokers **paper trading only** in V1.

This is not an AI stock-picker and not a system aimed at high monthly returns.

## Status

Phase 1 (Foundation) is built: for any past date the platform can say which US common
stocks were eligible to be bought under the AAOIFI-v1 Sharia screen, and why the others were
not. Owner sign-off is pending. Research, risk, broker and dashboard phases come next.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and Docker Desktop (WSL 2 engine on Windows).

```sh
uv sync                                   # install dependencies
cp .env.example .env                      # then fill in the HQ_DB_* passwords and the data keys
docker compose up -d --wait               # start PostgreSQL (listens on 127.0.0.1 only)
uv run python -m halal_quant.db.bootstrap # create roles + schema (safe to re-run)
uv run alembic upgrade head               # apply migrations
```

Database roles (least privilege): `hq_migrator` owns the `hq` schema and is used only by
migrations; `hq_app` reads and writes data but cannot change the schema; `hq_readonly` can
only read. Audit, classification, universe and data-quality tables are add-only for `hq_app`.

## Running the Phase 1 pipeline

Data comes from [Sharadar](https://data.nasdaq.com/databases/SFA) (licensed, personal use:
never commit it) through `HQ_SHARADAR_API_KEY` in `.env`. Every importer is safe to re-run
(rows already stored are left alone) and the long ones resume by month or year. Sharadar allows
one large query at a time, so run them one after another.

```sh
# 1. Data (once, then to refresh)
uv run python -m halal_quant.data.sharadar.companies      # securities, share-class links
uv run python -m halal_quant.data.sharadar.actions        # splits, dividends, mergers, delistings
uv run python -m halal_quant.data.sharadar.prices         # daily prices, 1998 to today (monthly)
uv run python -m halal_quant.data.sharadar.fundamentals   # as-reported filings (yearly)
uv run python -m halal_quant.data.sharadar.market_cap     # daily market value (monthly)

# 2. Check the data (stored findings; the universe builder fails closed without a covering run)
uv run python -m halal_quant.data.quality --start 1998 --end 2026 --as-of 2026-09-30 --report out.md

# 3. Screen every month-end (AAOIFI-v1, config/sharia/aaoifi_v1.yaml)
uv run python -m halal_quant.sharia.classification --start 1998-12

# 4. Build the eligible universe for every month (config/universe/default.yaml)
uv run python -m halal_quant.universe.batch --start 1998-12

# 5. Ask the Phase 1 question for any date, and prove it is reproducible
uv run python -m halal_quant.universe.show --date 2020-01-02
```

Reports: `python -m halal_quant.sharia.reports` (change log and unknown-data report) and
`python -m halal_quant.sharia.crosscheck` (comparison with an outside provider; optional, needs
its own key).

### How a date's universe is decided

For a decision on date D, only information from before D is used. A security is eligible if it
is US common stock that is trading, its Sharia classification in effect on D is HALAL, its latest
price is fresh and at least $5, its median daily dollar volume over the last 20 trading days is at
least $1M, it has no data-quality finding in that window, and it is the most liquid share class of
its company. Anything not provably eligible is excluded. Every build is stored with a content hash
so the same inputs always give the same universe.

The Sharia rules (debt, cash and prohibited-income limits, excluded businesses) live in
`config/sharia/`; changing them needs a new methodology version, never an edit in place.

## Tests

```sh
uv run pytest                                  # unit tests
HQ_RUN_INTEGRATION_TESTS=1 uv run pytest       # also database tests (needs the setup above)
uv run ruff check . && uv run mypy src && uv run bandit -r src
```

Tests use made-up data only and never call an external service.

## Repository notes

Internal governance docs (requirements, security checklist, compliance
tracker, agent rulebook) and research/dashboard artifacts are kept out of
this public repository — see `private/` locally, which is git-ignored.
Licensed data and API keys never enter this repository.

## License / access

Personal project. Not investment advice. Not a religious ruling: the Sharia screen is an
approximation to be confirmed by a qualified reviewer before results are relied on.
