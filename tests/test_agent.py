from datetime import date

import pytest
from openai import OpenAIError
from sqlalchemy import Engine

from app.agent.agent import Agent
from app.agent.memory import MemoryExample
from app.config import Settings
from app.context import MOCK_USERS, UserContext
from app.db.runner import SqlRunner
from app.db.seed import seed_database
from tests.fakes import FakeMemory, fake_client, text_reply, tool_call

ADMIN = MOCK_USERS["alice"]  # Acme, admin
USER = MOCK_USERS["bob"]  # Acme, normal user


@pytest.fixture
def runner(engine: Engine) -> SqlRunner:
    seed_database(engine, today=date(2026, 6, 1))
    return SqlRunner(engine)


def make_agent(
    runner: SqlRunner,
    client,
    user: UserContext = ADMIN,
    memory: FakeMemory | None = None,
    **overrides,
) -> Agent:
    settings = Settings(_env_file=None, openai_api_key="sk-test", **overrides)
    return Agent(settings, runner, user, memory=memory, client=client)  # type: ignore[arg-type]


def test_tool_call_then_answer(runner: SqlRunner) -> None:
    client = fake_client(
        tool_call("SELECT COUNT(*) AS n FROM orders"),
        text_reply("There are 180 orders."),
    )
    events = list(make_agent(runner, client).stream("How many orders?"))

    kinds = [e.kind for e in events]
    assert "sql" in kinds and "result" in kinds
    assert kinds[-1] == "answer"
    assert events[-1].message == "There are 180 orders."
    assert len(client.chat.completions.calls) == 2


def test_model_can_recover_from_a_sql_error(runner: SqlRunner) -> None:
    client = fake_client(
        tool_call("SELECT nope FROM orders"),
        tool_call("SELECT COUNT(*) FROM orders", call_id="call_2"),
        text_reply("Done."),
    )
    response = make_agent(runner, client).ask("How many orders?")
    assert response.error is None
    assert response.answer == "Done."
    assert response.result is not None and response.result.rows[0][0] == 180


def test_gives_up_after_max_tool_iterations(runner: SqlRunner) -> None:
    client = fake_client(tool_call("SELECT 1"), tool_call("SELECT 2", call_id="call_2"))
    events = list(make_agent(runner, client, max_tool_iterations=2).stream("loop forever"))
    assert events[-1].kind == "error"
    assert "2 tool calls" in events[-1].message


def test_llm_failures_are_reported_not_raised(runner: SqlRunner) -> None:
    client = fake_client(OpenAIError("boom"))
    response = make_agent(runner, client).ask("anything")
    assert response.answer is None
    assert response.error is not None and "boom" in response.error  # admins see the detail


def test_conversation_history_is_sent_on_follow_ups(runner: SqlRunner) -> None:
    client = fake_client(text_reply("First."), text_reply("Second."))
    agent = make_agent(runner, client)
    agent.ask("first question")
    agent.ask("second question")

    sent = client.chat.completions.calls[-1]["messages"]
    assert [m["content"] for m in sent if m["role"] == "user"] == [
        "first question",
        "second question",
    ]

    agent.reset()
    client.chat.completions._responses.append(text_reply("Third."))
    agent.ask("third question")
    sent = client.chat.completions.calls[-1]["messages"]
    assert [m["content"] for m in sent if m["role"] == "user"] == ["third question"]


# --- roles -------------------------------------------------------------------------------


def test_admins_see_generated_sql_and_the_executed_rewrite(runner: SqlRunner) -> None:
    client = fake_client(tool_call("SELECT COUNT(*) FROM orders"), text_reply("OK"))
    response = make_agent(runner, client, ADMIN).ask("count")
    assert response.sql == "SELECT COUNT(*) FROM orders"
    assert response.result is not None and "org_id = 1" in response.result.sql


def test_normal_users_never_receive_sql_or_error_details(runner: SqlRunner) -> None:
    client = fake_client(
        tool_call("SELECT nope FROM orders"),
        tool_call("SELECT COUNT(*) FROM orders", call_id="call_2"),
        text_reply("OK"),
    )
    events = list(make_agent(runner, client, USER).stream("count"))

    assert all(e.kind != "sql" for e in events)
    assert all(e.detail is None for e in events)
    results = [e.result for e in events if e.kind == "result"]
    assert results and all(r is not None and r.sql == "" for r in results)
    assert not any("nope" in repr(e) for e in events)


def test_normal_users_do_not_receive_llm_error_details(runner: SqlRunner) -> None:
    client = fake_client(OpenAIError("Incorrect API key provided: sk-secret"))
    response = make_agent(runner, client, USER).ask("anything")
    assert response.error == "The language model request failed."


def test_rls_is_applied_for_each_users_org(runner: SqlRunner) -> None:
    client = fake_client(tool_call("SELECT DISTINCT org_id FROM orders"), text_reply("OK"))
    response = make_agent(runner, client, MOCK_USERS["ivan"]).ask("which orgs?")
    assert response.result is not None and response.result.rows == [[3]]


# --- memory ------------------------------------------------------------------------------


def test_similar_examples_are_injected_into_the_prompt(runner: SqlRunner) -> None:
    memory = FakeMemory(
        examples=[MemoryExample("top customers", "SELECT 'example-sql'", "Emma", 0.1)]
    )
    client = fake_client(text_reply("OK"))
    make_agent(runner, client, memory=memory).ask("top customers please")

    system_prompt = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "top customers" in system_prompt and "SELECT 'example-sql'" in system_prompt
    assert memory.searches == [("top customers please", 1, 5)]


def test_successful_queries_are_saved_with_the_models_sql(runner: SqlRunner) -> None:
    memory = FakeMemory()
    client = fake_client(tool_call("SELECT COUNT(*) FROM orders"), text_reply("180 orders."))
    make_agent(runner, client, memory=memory).ask("how many orders")

    assert memory.saved == [
        {
            "question": "how many orders",
            "sql": "SELECT COUNT(*) FROM orders",  # not the RLS-rewritten SQL
            "summary": "180 orders.",
            "org_id": 1,
        }
    ]


def test_empty_results_and_failures_are_not_saved(runner: SqlRunner) -> None:
    memory = FakeMemory()
    client = fake_client(
        tool_call("SELECT * FROM orders WHERE id < 0"),
        text_reply("Nothing found."),
        tool_call("SELECT nope"),
        text_reply("Sorry."),
    )
    agent = make_agent(runner, client, memory=memory)
    agent.ask("empty")
    agent.ask("broken")
    assert memory.saved == []


def test_memory_failures_do_not_break_answers(runner: SqlRunner) -> None:
    memory = FakeMemory(fail=True)
    client = fake_client(tool_call("SELECT COUNT(*) FROM orders"), text_reply("180 orders."))
    response = make_agent(runner, client, memory=memory).ask("how many orders")
    assert response.answer == "180 orders."
