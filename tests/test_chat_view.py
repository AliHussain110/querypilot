from app.agent.agent import AgentEvent
from app.chat_view import TurnView, format_cell, result_to_markdown
from app.db.runner import QueryResult


def make_result(rows: list[list], *, sql: str = "", truncated: bool = False) -> QueryResult:
    return QueryResult(
        sql=sql, columns=["name", "revenue"], rows=rows, truncated=truncated, elapsed_ms=1.0
    )


def test_format_cell() -> None:
    assert format_cell(None) == "—"
    assert format_cell(1234.5) == "1,234.50"
    assert format_cell(2026) == "2026"  # ints (ids, years) are not thousands-separated
    assert format_cell("a|b\nc") == "a\\|b c"
    assert len(format_cell("x" * 500)) == 80


def test_markdown_table_aligns_numbers_right() -> None:
    markdown = result_to_markdown(make_result([["Emma", 10.5], ["Liam", 3.0]]))
    lines = markdown.splitlines()
    assert lines[0] == "| name | revenue |"
    assert lines[1] == "| --- | ---: |"
    assert lines[2] == "| Emma | 10.50 |"
    assert markdown.endswith("*2 row(s)*")


def test_markdown_table_reports_hidden_and_capped_rows() -> None:
    result = make_result([[f"c{i}", float(i)] for i in range(10)], truncated=True)
    markdown = result_to_markdown(result, max_rows=3)
    assert markdown.count("\n| c") == 3
    assert "showing the first 3 of 10 rows" in markdown
    assert "capped by the row limit" in markdown


def test_empty_result() -> None:
    assert result_to_markdown(make_result([])) == "*No rows returned.*"


def admin_turn() -> TurnView:
    turn = TurnView(dialect="sqlite")
    executed = "SELECT name FROM (SELECT * FROM customers WHERE org_id = 1) AS customers"
    for event in (
        AgentEvent("status", "Thinking..."),
        AgentEvent("sql", sql="SELECT name FROM customers"),
        AgentEvent("status", "Executing query..."),
        AgentEvent("result", result=make_result([["Emma", 1.0]], sql=executed)),
    ):
        turn.update(event)
    return turn


def test_in_progress_turn_shows_latest_status_as_pending_title() -> None:
    first = admin_turn().messages()[0]
    assert first["metadata"] == {"title": "Executing query...", "status": "pending"}
    assert "- Thinking..." in first["content"]


def test_admin_turn_shows_sql_and_the_rls_rewrite() -> None:
    turn = admin_turn()
    turn.update(AgentEvent("answer", "Emma is first."))
    messages = turn.messages()

    assert [m.get("metadata", {}).get("title") for m in messages][:2] == [
        "Worked through 2 step(s)",
        "Generated SQL",
    ]
    sql_message = messages[1]["content"]
    assert "```sql" in sql_message and "Executed with row-level security applied" in sql_message
    assert "org_id = 1" in sql_message
    assert "| name | revenue |" in messages[2]["content"]
    assert messages[-1] == {"role": "assistant", "content": "Emma is first."}


def test_normal_user_turn_has_no_sql_message() -> None:
    turn = TurnView(dialect="sqlite")
    turn.update(AgentEvent("status", "Executing query..."))
    turn.update(AgentEvent("result", result=make_result([["Emma", 1.0]], sql="")))  # redacted
    turn.update(AgentEvent("answer", "Done."))

    messages = turn.messages()
    assert all(m.get("metadata", {}).get("title") != "Generated SQL" for m in messages)
    assert not any("SELECT" in m["content"] for m in messages)


def test_error_turn() -> None:
    turn = TurnView()
    turn.update(AgentEvent("error", "The language model request failed.", detail="boom"))
    last = turn.messages()[-1]["content"]
    assert last.startswith("⚠️ The language model request failed.")
    assert "boom" in last
