"""Terminal chat loop for testing the agent without the UI: ``python -m app.cli``."""

from __future__ import annotations

import argparse

from sqlalchemy.exc import SQLAlchemyError

from app.agent.agent import Agent, AgentEvent
from app.bootstrap import build_context
from app.config import ConfigError, configure_logging, get_settings
from app.context import MOCK_USERS
from app.db.runner import QueryResult

DIM, RESET = "\033[2m", "\033[0m"
MAX_CELL_WIDTH = 40
MAX_TABLE_ROWS = 20


def format_table(result: QueryResult, *, max_rows: int = MAX_TABLE_ROWS) -> str:
    """Render a result as a fixed-width text table."""

    def cell(value: object) -> str:
        text = "NULL" if value is None else str(value)
        return text if len(text) <= MAX_CELL_WIDTH else text[: MAX_CELL_WIDTH - 1] + "…"

    shown = [[cell(v) for v in row] for row in result.rows[:max_rows]]
    widths = [
        max(len(col), *(len(row[i]) for row in shown)) if shown else len(col)
        for i, col in enumerate(result.columns)
    ]

    def line(values: list[str]) -> str:
        return "  ".join(v.ljust(w) for v, w in zip(values, widths, strict=True)).rstrip()

    out = [line(result.columns), line(["-" * w for w in widths]), *(line(r) for r in shown)]
    hidden = result.row_count - len(shown)
    footer = f"{result.row_count} row(s)"
    if hidden > 0:
        footer += f", showing first {len(shown)}"
    if result.truncated:
        footer += " (result was capped by the row limit)"
    return "\n".join([*out, footer])


def render(event: AgentEvent) -> None:
    if event.kind == "status":
        detail = f" ({event.detail})" if event.detail else ""
        print(f"{DIM}· {event.message}{detail}{RESET}")
    elif event.kind == "sql":
        print(f"\nSQL:\n  {event.sql}\n")
    elif event.kind == "result" and event.result is not None:
        print(format_table(event.result))
    elif event.kind == "answer":
        print(f"\n{event.message}")
    elif event.kind == "error":
        detail = f" {event.detail}" if event.detail else ""
        print(f"\n! {event.message}{detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chat with QueryPilot in the terminal.")
    parser.add_argument(
        "--user", choices=sorted(MOCK_USERS), default="alice", help="who you are (default: alice)"
    )
    args = parser.parse_args(argv)
    user = MOCK_USERS[args.user]

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        agent = build_context(settings).new_agent(user)
    except ConfigError as exc:
        print(exc)
        return 1
    except SQLAlchemyError as exc:
        print(f"Could not initialise the database ({settings.safe_database_url}): {exc}")
        return 1

    print(f"QueryPilot CLI - signed in as {user.name} ({user.role}, {user.org_name}).")
    print("Ask a question about the data. Commands: /reset, exit")
    while True:
        try:
            question = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            return 0
        if question == "/reset":
            agent.reset()
            print("Conversation cleared.")
            continue
        for event in agent.stream(question):
            render(event)


if __name__ == "__main__":
    raise SystemExit(main())
