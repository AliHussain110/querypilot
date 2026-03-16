from datetime import date

import gradio as gr
import pytest
from sqlalchemy import Engine

from app.bootstrap import AppContext, list_organizations
from app.config import Settings
from app.db.runner import SqlRunner
from app.db.seed import seed_database
from app.ui import ChatController, build_demo, launch_kwargs
from tests.fakes import fake_client, text_reply, tool_call


def make_ctx(engine: Engine, *responses) -> AppContext:
    seed_database(engine, today=date(2026, 6, 1))
    return AppContext(
        settings=Settings(_env_file=None, openai_api_key="sk-test"),
        runner=SqlRunner(engine),
        client=fake_client(*responses),  # type: ignore[arg-type]
        memory=None,
        organizations=list_organizations(engine),
    )


def run_turn(controller: ChatController, text, session, role="Admin", org_id=1):
    yields = list(controller.respond(text, session, role, org_id))
    chat, _, session = yields[-1]
    return chat, session, yields


def titles(chat: list[dict]) -> list[str | None]:
    return [m.get("metadata", {}).get("title") for m in chat]


def test_admin_turn_streams_progress_sql_table_and_answer(engine: Engine) -> None:
    ctx = make_ctx(
        engine,
        tool_call("SELECT name FROM organizations"),
        text_reply("Your organization is Acme Corp."),
    )
    chat, session, yields = run_turn(ChatController(ctx), "Which org am I in?", None)

    assert len(yields) > 4  # the UI updates after every agent event
    assert chat[0] == {"role": "user", "content": "Which org am I in?"}
    assert "Generated SQL" in titles(chat)
    assert any("| name |" in m["content"] and "Acme Corp" in m["content"] for m in chat)
    assert chat[-1]["content"] == "Your organization is Acme Corp."
    assert session is not None and session.transcript[-1] == chat[-1]


def test_normal_user_never_sees_sql(engine: Engine) -> None:
    ctx = make_ctx(engine, tool_call("SELECT name FROM organizations"), text_reply("Acme."))
    chat, _, yields = run_turn(ChatController(ctx), "Which org?", None, role="User")

    for frame, _, _ in yields:
        assert "Generated SQL" not in titles(frame)
        assert not any("SELECT" in m["content"] for m in frame)
    assert any("Acme Corp" in m["content"] for m in chat)


def test_identity_switch_applies_row_level_security(engine: Engine) -> None:
    ctx = make_ctx(engine, tool_call("SELECT id FROM organizations"), text_reply("OK"))
    controller = ChatController(ctx)

    session, chat, banner = controller.reset("Admin", 2)
    assert (session, chat) == (None, [])
    assert "Globex Industries" in banner and "org_id = 2" in banner

    chat, _, _ = run_turn(controller, "Which org?", session, org_id=2)
    table = next(m["content"] for m in chat if "| id |" in m["content"])
    assert "| 2 |" in table and "| 1 |" not in table and "| 3 |" not in table


def test_conversation_continues_in_the_same_session(engine: Engine) -> None:
    ctx = make_ctx(engine, text_reply("First."), text_reply("Second."))
    controller = ChatController(ctx)
    _, session, _ = run_turn(controller, "one", None)
    chat, session, _ = run_turn(controller, "two", session)

    assert [m["content"] for m in chat if m["role"] == "user"] == ["one", "two"]
    assert chat[-1]["content"] == "Second."


def test_blank_question_does_nothing(engine: Engine) -> None:
    ctx = make_ctx(engine)  # no scripted responses: any LLM call would fail
    chat, session, _ = run_turn(ChatController(ctx), "   ", None)
    assert chat == [] and session is None


def test_demo_builds_with_theme_and_css(engine: Engine) -> None:
    demo = build_demo(make_ctx(engine))
    assert isinstance(demo, gr.Blocks)
    assert set(launch_kwargs()) == {"theme", "css"}


@pytest.mark.parametrize("org_id", [1, 2, 3])
def test_every_org_has_a_banner(engine: Engine, org_id: int) -> None:
    banner = ChatController(make_ctx(engine)).reset("User", org_id)[2]
    assert f"org_id = {org_id}" in banner and "hidden" in banner
