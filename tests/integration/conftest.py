"""Integration tests need a real, bootstrapped and migrated PostgreSQL.

They run only when HQ_RUN_INTEGRATION_TESTS=1 (CI sets it; locally, start the database first:
`docker compose up -d --wait`, bootstrap, `alembic upgrade head`).
"""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from halal_quant.core.settings import DbRole, Settings, get_settings
from halal_quant.db.engine import make_engine


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("HQ_RUN_INTEGRATION_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="set HQ_RUN_INTEGRATION_TESTS=1 to run database tests")
    for item in items:
        if "integration" in item.nodeid:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def engines(settings: Settings) -> Iterator[dict[DbRole, Engine]]:
    created = {role: make_engine(settings, role) for role in DbRole}
    yield created
    for engine in created.values():
        engine.dispose()
