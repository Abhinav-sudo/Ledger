import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ledger.settings import database_url
from world.generator import DEV_SEED, World, erp_sql, generate

SCHEMA = Path(__file__).parents[1] / "world" / "schema.sql"


@pytest.fixture(scope="session")
def world() -> World:
    return generate(100, DEV_SEED)


@pytest.fixture(scope="session")
def database(world: World) -> Iterator[str]:
    """A throwaway database holding the schema and the ERP side of a small world."""
    admin = database_url()
    try:
        psycopg.connect(admin, connect_timeout=2).close()
    except psycopg.OperationalError:
        pytest.skip("Postgres is not reachable; run `docker compose up -d postgres`")
    name = f"ledger_test_{os.getpid()}"
    with psycopg.connect(admin, autocommit=True) as con:
        con.execute(f"DROP DATABASE IF EXISTS {name}")
        con.execute(f"CREATE DATABASE {name}")
    url = psycopg.conninfo.make_conninfo(admin, dbname=name)
    with psycopg.connect(url) as con:
        con.execute(SCHEMA.read_text())
        con.execute(erp_sql(world))
    previous = os.environ.get("LEDGER_DATABASE_URL")
    os.environ["LEDGER_DATABASE_URL"] = url
    yield url
    if previous is None:
        del os.environ["LEDGER_DATABASE_URL"]
    else:
        os.environ["LEDGER_DATABASE_URL"] = previous
    with psycopg.connect(admin, autocommit=True) as con:
        con.execute(f"DROP DATABASE {name} WITH (FORCE)")


@pytest.fixture
def db(database: str) -> str:
    with psycopg.connect(database) as con:
        con.execute(
            "TRUNCATE invoice_lines, invoices, payment_intents, audit_log, graph_jobs CASCADE"
        )
    return database
