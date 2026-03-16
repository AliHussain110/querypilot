from app.cli import format_table
from app.db.runner import QueryResult


def make_result(rows: list[list], truncated: bool = False) -> QueryResult:
    return QueryResult(
        sql="SELECT 1", columns=["id", "name"], rows=rows, truncated=truncated, elapsed_ms=1.0
    )


def test_format_table_aligns_columns() -> None:
    table = format_table(make_result([[1, "Acme Corp"], [22, None]]))
    lines = table.splitlines()
    assert lines[0].startswith("id")
    assert "NULL" in table
    assert lines[-1] == "2 row(s)"


def test_format_table_reports_hidden_rows() -> None:
    table = format_table(make_result([[i, "x"] for i in range(30)]), max_rows=5)
    assert "30 row(s), showing first 5" in table
