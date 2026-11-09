import inspect

import psycopg
import pytest

from ledger.graph.nodes.analysts import ANALYSTS
from ledger.tools import erp_queries


def test_analysts_bind_only_erp_queries() -> None:
    for name, analyst in ANALYSTS.items():
        assert analyst.tools, name
        for tool in analyst.tools:
            assert tool.__module__ == "ledger.tools.erp_queries", (name, tool)


def test_erp_queries_only_select() -> None:
    sql = [v for k, v in vars(erp_queries).items() if k.endswith("_SQL")]
    assert sql
    for statement in sql:
        assert statement.lstrip().upper().startswith("SELECT"), statement
    assert inspect.getsource(erp_queries).count("psycopg.connect(") == 1


def test_postgres_refuses_writes_through_erp_queries(db: str) -> None:
    with erp_queries.connect() as con, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        con.execute("DELETE FROM vendors")
