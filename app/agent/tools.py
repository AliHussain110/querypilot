"""The ``run_sql`` tool: its OpenAI schema, argument parsing and result formatting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.context import UserContext
from app.db.errors import SqlError
from app.db.runner import QueryResult, SqlRunner

RUN_SQL_NAME = "run_sql"
MAX_ROWS_FOR_MODEL = 50  # keep prompts small; the user still sees every returned row

RUN_SQL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": RUN_SQL_NAME,
        "description": (
            "Execute one read-only SQL SELECT statement against the database and return the "
            "columns and rows. Returns an error message if the SQL is invalid."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A single SELECT statement in the database's SQL dialect.",
                }
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
}


class RunSqlArgs(BaseModel):
    sql: str = Field(min_length=1)


@dataclass(frozen=True)
class ToolOutcome:
    content: str  # JSON string sent back to the model as the tool message
    sql: str | None = None  # the SQL as the model wrote it (before RLS rewriting)
    result: QueryResult | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def failure(message: str) -> ToolOutcome:
    return ToolOutcome(content=json.dumps({"error": message}), error=message)


def run_sql_tool(runner: SqlRunner, sql: str, user: UserContext) -> ToolOutcome:
    """Run ``sql`` as ``user`` and package the result (or the error) for the model."""
    try:
        result = runner.run(sql, user)
    except SqlError as exc:
        return failure(str(exc))

    payload: dict[str, Any] = {
        "columns": result.columns,
        "rows": result.rows[:MAX_ROWS_FOR_MODEL],
        "row_count": result.row_count,
        "truncated": result.truncated,
    }
    if result.row_count > MAX_ROWS_FOR_MODEL:
        payload["note"] = f"Only the first {MAX_ROWS_FOR_MODEL} rows are shown to you."
    return ToolOutcome(content=json.dumps(payload, default=str), sql=sql, result=result)
