import json
from datetime import date

import pytest
from sqlalchemy import Engine

from app.agent.tools import MAX_ROWS_FOR_MODEL, run_sql_tool
from app.context import MOCK_USERS
from app.db.runner import SqlRunner
from app.db.seed import seed_database

ACME = MOCK_USERS["alice"]


@pytest.fixture
def runner(engine: Engine) -> SqlRunner:
    seed_database(engine, today=date(2026, 6, 1))
    return SqlRunner(engine, max_rows=100)


def test_success_payload(runner: SqlRunner) -> None:
    sql = "SELECT name FROM organizations ORDER BY id"
    outcome = run_sql_tool(runner, sql, ACME)
    payload = json.loads(outcome.content)
    assert outcome.ok and outcome.result is not None
    assert outcome.sql == sql  # the model's SQL, not the RLS-rewritten one
    assert payload["columns"] == ["name"]
    assert payload["rows"] == [["Acme Corp"]]
    assert payload["row_count"] == 1


def test_errors_are_returned_to_the_model(runner: SqlRunner) -> None:
    outcome = run_sql_tool(runner, "DROP TABLE orders", ACME)
    assert not outcome.ok and outcome.result is None
    assert "error" in json.loads(outcome.content)


def test_model_sees_a_capped_number_of_rows(runner: SqlRunner) -> None:
    outcome = run_sql_tool(runner, "SELECT id FROM orders", ACME)
    payload = json.loads(outcome.content)
    assert outcome.result is not None and outcome.result.row_count == 100
    assert len(payload["rows"]) == MAX_ROWS_FOR_MODEL
    assert "note" in payload
