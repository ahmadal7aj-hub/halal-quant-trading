"""The eligible universe for a date (task 14; PRD §8, §19, §29; critical Tests 1, 2, 3, 10).

`build_universe(conn, as_of, ...)` answers: which securities may the strategy hold when it decides
on `as_of`? A security must pass every rule, in this order, and the first rule it fails is
recorded as its reason for exclusion (nothing is dropped silently):

1. it is one of the allowed security categories (US common stock, G8 OI-14);
2. it was trading on `as_of` (not yet delisted);
3. its Sharia classification in effect on `as_of` is HALAL (PRD §6; anything else is excluded);
4. it has a recent price (not older than the staleness limit) and that price is at least the
   minimum price;
5. its median daily dollar volume over the liquidity window is at least the minimum;
6. it has no data-quality finding inside the window (fail closed; Test 3);
7. only one share class per company is kept: the most liquid (G8 OI-19).

Only information from before `as_of` is used: prices dated on or after `as_of` are ignored, and a
classification only counts once its `effective_from` has been reached (Test 10).

The result is stored, with a hash of its content and the parameters it was built with, so the
same inputs always give the same universe and it can be reproduced later. Stored builds are
add-only.
"""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import batched
from statistics import median

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Connection,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, distinct_on

from halal_quant.audit import AuditEvent, record_event
from halal_quant.core.config import LoadedConfig, UniverseConfig
from halal_quant.data.calendar import previous_trading_day
from halal_quant.data.market_data import daily_price_table
from halal_quant.data.quality import data_quality_finding_table, data_quality_run_table
from halal_quant.data.security_master import security_table
from halal_quant.db.engine import metadata
from halal_quant.sharia.classification import (
    COMMON_STOCK_CATEGORIES,
    PROVIDER,
    classification_table,
)
from halal_quant.sharia.screening import Status

ACTOR = "system:universe-builder"
INSERT_BATCH = 5000
PRICE_LOOKBACK_DAYS = 60  # how far back to look for a security's latest price

universe_build_table = Table(
    "universe_build",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("as_of", Date, nullable=False),
    Column("methodology", Text, nullable=False),
    Column("universe_version", Text, nullable=False),
    Column("config_sha256", Text, nullable=False),
    Column("data_quality_run_id", BigInteger),
    Column("member_count", Integer, nullable=False),
    Column("content_sha256", Text, nullable=False),
    Column("exclusions", JSONB, nullable=False),  # count of securities per reason
    Column("built_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("content_sha256", name="uq_universe_build_content"),
    Index("ix_universe_build_as_of", "as_of"),
)

universe_member_table = Table(
    "universe_member",
    metadata,
    Column(
        "build_id",
        BigInteger,
        ForeignKey("universe_build.id", name="fk_universe_member_build"),
        nullable=False,
    ),
    Column(
        "security_id",
        BigInteger,
        ForeignKey("security.security_id", name="fk_universe_member_security"),
        nullable=False,
    ),
    PrimaryKeyConstraint("build_id", "security_id", name="pk_universe_member"),
    CheckConstraint("security_id > 0", name="ck_universe_member_security"),
)


class UniverseError(Exception):
    """The universe cannot be built safely (for example, no data-quality run covers the window)."""


@dataclass
class UniverseResult:
    build_id: int
    as_of: date
    content_sha256: str
    members: list[int]
    exclusions: Counter[str] = field(default_factory=Counter)
    reused: bool = False  # True when an identical build was already stored


def liquidity_window(as_of: date, length: int) -> list[date]:
    """The last `length` trading days strictly before `as_of`, oldest first."""
    days: list[date] = []
    current = as_of
    while len(days) < length:
        current = previous_trading_day(current)
        days.append(current)
    return sorted(days)


def staleness_cutoff(as_of: date, max_age_trading_days: int) -> date:
    """A latest price dated before this is stale on `as_of`."""
    cutoff = as_of
    for _ in range(max_age_trading_days):
        cutoff = previous_trading_day(cutoff)
    return cutoff


def content_hash(
    as_of: date,
    methodology: str,
    config_sha256: str,
    quality_run_id: int | None,
    members: Sequence[int],
    categories: Sequence[str],
) -> str:
    """Identifies a universe by what it was built from and what it contains."""
    payload = {
        "as_of": as_of.isoformat(),
        "methodology": methodology,
        "config": config_sha256,
        "quality_run": quality_run_id,
        "categories": sorted(categories),
        "members": sorted(members),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _company_key(name: str) -> str:
    return " ".join(name.upper().split())


def _covering_quality_run(conn: Connection, first: date, last: date) -> int:
    r = data_quality_run_table.c
    run_id = conn.execute(
        select(r.id)
        .where(r.first_date <= first, r.last_date >= last)
        .order_by(r.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run_id is None:
        raise UniverseError(
            f"No data-quality run covers {first} to {last}. Run the data-quality checks first: "
            "without them the data cannot be trusted, so no universe is built (fail closed)."
        )
    return int(run_id)


def build_universe(
    conn: Connection,
    as_of: date,
    universe: LoadedConfig[UniverseConfig],
    categories: Sequence[str] = COMMON_STOCK_CATEGORIES,
    security_ids: Sequence[int] | None = None,
    quality_run_id: int | None = None,
    actor: str = ACTOR,
) -> UniverseResult:
    """Build, store and return the eligible universe for `as_of`.

    Runs on the caller's connection: the caller commits, or rolls everything back. `security_ids`
    narrows the securities considered (used by tests and spot checks).
    """
    cfg = universe.config
    methodology = cfg.sharia_methodology
    window = liquidity_window(as_of, cfg.liquidity_window_trading_days)
    run_id = quality_run_id or _covering_quality_run(conn, window[0], window[-1])
    exclusions: Counter[str] = Counter()

    def drop(reason: str) -> None:
        exclusions[reason] += 1

    # 1-2. category and lifecycle
    s = security_table.c
    wanted = select(s.security_id, s.company_name).where(
        s.category.in_(categories),
        (s.start_date.is_(None)) | (s.start_date <= as_of),
        (s.end_date.is_(None)) | (s.end_date >= as_of),
    )
    if security_ids is not None:
        wanted = wanted.where(s.security_id.in_(security_ids))
    candidates = {r.security_id: r.company_name for r in conn.execute(wanted)}

    # 3. Sharia classification in effect on as_of (Test 10: only records already effective)
    c = classification_table.c
    status_rows = conn.execute(
        select(c.security_id, c.status)
        .ext(distinct_on(c.security_id))
        .where(
            c.provider == PROVIDER,
            c.methodology == methodology,
            c.effective_from <= as_of,
            c.effective_to >= as_of,
        )
        .order_by(c.security_id, c.screening_date.desc())
    )
    status = {r.security_id: r.status for r in status_rows}
    survivors: list[int] = []
    for security_id in sorted(candidates):
        record = status.get(security_id)
        if record is None:
            drop("sharia_no_record")
        elif record != Status.HALAL.value:
            drop(f"sharia_{record.lower()}")
        else:
            survivors.append(security_id)

    # 4. latest price before as_of: fresh enough and above the minimum
    p = daily_price_table.c
    cutoff = staleness_cutoff(as_of, cfg.max_price_age_trading_days)
    latest = {
        r.security_id: (r.price_date, r.close_unadjusted)
        for r in conn.execute(
            select(p.security_id, p.price_date, p.close_unadjusted)
            .ext(distinct_on(p.security_id))
            .where(
                p.price_date < as_of,
                p.price_date >= as_of - timedelta(days=PRICE_LOOKBACK_DAYS),
                p.security_id.in_(survivors),
            )
            .order_by(p.security_id, p.price_date.desc())
        )
    }
    priced: list[int] = []
    for security_id in survivors:
        seen = latest.get(security_id)
        if seen is None or seen[0] < cutoff:
            drop("stale_price")
        elif seen[1] < cfg.min_price_usd:
            drop("below_min_price")
        else:
            priced.append(security_id)

    # 5. median daily dollar volume over the window (a day with no price counts as zero)
    dollar_volume: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for r in conn.execute(
        select(p.security_id, p.price_date, p.close, p.volume).where(
            p.price_date.in_(window), p.security_id.in_(priced)
        )
    ):
        dollar_volume[r.security_id][r.price_date] = r.close * r.volume
    liquidity: dict[int, Decimal] = {}
    liquid: list[int] = []
    for security_id in priced:
        days = dollar_volume.get(security_id, {})
        liquidity[security_id] = Decimal(median(days.get(d, Decimal(0)) for d in window))
        if liquidity[security_id] < cfg.min_median_daily_dollar_volume_usd:
            drop("below_min_liquidity")
        else:
            liquid.append(security_id)

    # 6. no data-quality finding inside the window
    f = data_quality_finding_table.c
    flagged = {
        r.security_id
        for r in conn.execute(
            select(f.security_id)
            .where(
                f.run_id == run_id,
                f.data_date.between(window[0], window[-1]),
                f.security_id.in_(liquid),
            )
            .distinct()
        )
    }
    clean: list[int] = []
    for security_id in liquid:
        if security_id in flagged:
            drop("data_quality_finding")
        else:
            clean.append(security_id)

    # 7. one share class per company: keep the most liquid (ties: lowest security_id)
    by_company: dict[str, list[int]] = defaultdict(list)
    for security_id in clean:
        by_company[_company_key(candidates[security_id])].append(security_id)
    members: list[int] = []
    for classes in by_company.values():
        keep = min(classes, key=lambda sid: (-liquidity[sid], sid))
        members.append(keep)
        if len(classes) > 1:
            exclusions["duplicate_share_class"] += len(classes) - 1
    members.sort()

    sha = content_hash(as_of, methodology, universe.ref.sha256, run_id, members, tuple(categories))
    b = universe_build_table.c
    existing = conn.execute(select(b.id).where(b.content_sha256 == sha)).scalar_one_or_none()
    if existing is not None:
        return UniverseResult(int(existing), as_of, sha, members, exclusions, reused=True)

    build_id: int = conn.execute(
        universe_build_table.insert()
        .values(
            as_of=as_of,
            methodology=methodology,
            universe_version=universe.ref.version,
            config_sha256=universe.ref.sha256,
            data_quality_run_id=run_id,
            member_count=len(members),
            content_sha256=sha,
            exclusions=dict(exclusions),
        )
        .returning(b.id)
    ).scalar_one()
    for batch in batched(members, INSERT_BATCH):
        conn.execute(
            universe_member_table.insert(),
            [{"build_id": build_id, "security_id": sid} for sid in batch],
        )
    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="universe.built",
            entity_type="universe_build",
            entity_id=str(build_id),
            reason="Eligible universe build",
            source=__name__,
            details={
                "as_of": as_of,
                "methodology": methodology,
                "universe_version": universe.ref.version,
                "config_sha256": universe.ref.sha256,
                "data_quality_run_id": run_id,
                "content_sha256": sha,
                "member_count": len(members),
                "exclusions": dict(exclusions),
            },
        ),
    )
    return UniverseResult(build_id, as_of, sha, members, exclusions)


def stored_members(conn: Connection, build_id: int) -> list[int]:
    """The members of a stored universe build, in security_id order."""
    m = universe_member_table.c
    return [
        r.security_id
        for r in conn.execute(
            select(m.security_id).where(m.build_id == build_id).order_by(m.security_id)
        )
    ]
