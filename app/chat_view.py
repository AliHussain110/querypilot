"""Turn agent events into chat messages for the UI.

Pure functions and plain dicts (no Gradio import), so the rendering rules are unit-testable.
Messages use Gradio's "messages" format; ``metadata.title`` renders as a collapsible block and
``metadata.status == "pending"`` shows a spinner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot.errors import SqlglotError

from app.agent.agent import AgentEvent
from app.db.runner import QueryResult

Message = dict[str, Any]

MAX_CELL_CHARS = 80
MAX_TABLE_ROWS = 50


def format_cell(value: object) -> str:
    """Format one value for a Markdown table cell."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.2f}"
    text = str(value).replace("\n", " ").replace("|", "\\|")
    return text if len(text) <= MAX_CELL_CHARS else text[: MAX_CELL_CHARS - 1] + "…"


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def result_to_markdown(result: QueryResult, max_rows: int = MAX_TABLE_ROWS) -> str:
    """Render a query result as a Markdown table with a short footer."""
    if not result.rows:
        return "*No rows returned.*"

    shown = result.rows[:max_rows]
    numeric = [
        all(row[i] is None or _is_number(row[i]) for row in result.rows)
        for i in range(len(result.columns))
    ]
    lines = [
        "| " + " | ".join(c.replace("|", "\\|") for c in result.columns) + " |",
        "| " + " | ".join("---:" if is_num else "---" for is_num in numeric) + " |",
        *("| " + " | ".join(format_cell(v) for v in row) + " |" for row in shown),
    ]

    notes = [f"{result.row_count} row(s)"]
    if len(shown) < result.row_count:
        notes = [f"showing the first {len(shown)} of {result.row_count} rows"]
    if result.truncated:
        notes.append("capped by the row limit")
    return "\n".join(lines) + "\n\n*" + " · ".join(notes) + "*"


def _pretty_sql(sql: str, dialect: str | None) -> str:
    try:
        return sqlglot.transpile(sql, read=dialect, write=dialect, pretty=True)[0]
    except (SqlglotError, IndexError):
        return sql


def _sql_block(sql: str) -> str:
    return f"```sql\n{sql}\n```"


@dataclass
class TurnView:
    """Accumulates one assistant turn from agent events and renders it as chat messages.

    Order of messages: progress steps, generated SQL (admins only; the agent already
    filters it out for normal users), result table, final answer or error.
    """

    dialect: str | None = None
    max_table_rows: int = MAX_TABLE_ROWS
    _steps: list[str] = field(default_factory=list)
    _sql: str | None = None
    _result: QueryResult | None = None
    _answer: str | None = None
    _error: str | None = None
    _done: bool = False

    def update(self, event: AgentEvent) -> None:
        if event.kind == "status":
            step = f"{event.message} ({event.detail})" if event.detail else event.message
            if not self._steps or self._steps[-1] != step:
                self._steps.append(step)
        elif event.kind == "sql":
            self._sql = event.sql
        elif event.kind == "result":
            self._result = event.result
        elif event.kind == "answer":
            self._answer, self._done = event.message, True
        elif event.kind == "error":
            detail = f"\n\n`{event.detail}`" if event.detail else ""
            self._error, self._done = f"⚠️ {event.message}{detail}", True

    def messages(self) -> list[Message]:
        out: list[Message] = []
        if self._steps:
            if self._done:
                title, status = f"Worked through {len(self._steps)} step(s)", "done"
            else:
                title, status = self._steps[-1], "pending"
            out.append(
                {
                    "role": "assistant",
                    "content": "\n".join(f"- {step}" for step in self._steps),
                    "metadata": {"title": title, "status": status},
                }
            )
        if self._sql:
            out.append(
                {
                    "role": "assistant",
                    "content": self._sql_content(self._sql),
                    "metadata": {"title": "Generated SQL", "status": "done"},
                }
            )
        if self._result is not None:
            out.append(
                {
                    "role": "assistant",
                    "content": result_to_markdown(self._result, self.max_table_rows),
                }
            )
        if self._answer:
            out.append({"role": "assistant", "content": self._answer})
        if self._error:
            out.append({"role": "assistant", "content": self._error})
        return out

    def _sql_content(self, generated: str) -> str:
        content = _sql_block(_pretty_sql(generated, self.dialect))
        executed = self._result.sql if self._result else ""
        if executed:
            pretty_executed = _pretty_sql(executed, self.dialect)
            if pretty_executed != _pretty_sql(generated, self.dialect):
                content += "\n\n**Executed with row-level security applied:**\n"
                content += _sql_block(pretty_executed)
        return content
