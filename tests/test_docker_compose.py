import re
from pathlib import Path

COMPOSE = Path(__file__).parents[1] / "docker-compose.yml"


def test_postgres_port_is_bound_to_localhost_only() -> None:
    port_lines = re.findall(r'^\s*-\s*"([^"]*:5432)"\s*$', COMPOSE.read_text(), flags=re.M)
    assert port_lines, "expected a published Postgres port"
    for mapping in port_lines:
        assert mapping.startswith("127.0.0.1:"), mapping


def test_postgres_image_is_pinned() -> None:
    assert re.search(r"^\s*image:\s*postgres:\d+\.\d+\s*$", COMPOSE.read_text(), flags=re.M)
