"""The Gradio interface (Gradio 6): chat, identity picker, sample questions, dark theme."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import gradio as gr

from app.agent.agent import Agent, AgentEvent
from app.bootstrap import AppContext
from app.chat_view import Message, TurnView
from app.context import Role, UserContext

logger = logging.getLogger(__name__)

ROLE_CHOICES = ["Admin", "User"]
CONCURRENCY_LIMIT = 8

SAMPLE_QUESTIONS = [
    "Who are our top 5 customers by revenue?",
    "Monthly revenue for the last 6 months",
    "Which products sell the most units?",
    "What share of orders were cancelled or refunded?",
    "Average order value by customer country",
    "Which customers signed up in the last 90 days?",
    "Which product category brings in the most revenue?",
    "List every organization in the database",  # RLS demo: only your own appears
]

CSS = """
.gradio-container { max-width: 1100px !important; margin: 0 auto; }
#header h1 { margin-bottom: 0; }
#identity { opacity: 0.85; font-size: 0.9rem; }
#chatbot table { border-collapse: collapse; width: 100%; margin: 0.4rem 0; font-size: 0.9rem; }
#chatbot th, #chatbot td { padding: 6px 10px; border: 1px solid #263148; }
#chatbot th { background: #17203a; text-align: left; }
#chatbot tr:nth-child(even) td { background: #0f1629; }
#samples { flex-wrap: wrap; gap: 8px; }
#samples button { flex: 0 1 auto; min-width: 0; }
"""

_DARK_PALETTE = {
    "body_background_fill": "#0b1020",
    "body_text_color": "#e5e9f5",
    "body_text_color_subdued": "#9aa6c4",
    "background_fill_primary": "#0b1020",
    "background_fill_secondary": "#111831",
    "block_background_fill": "#111831",
    "block_border_color": "#263148",
    "block_label_background_fill": "#111831",
    "block_title_text_color": "#c7d0ea",
    "border_color_primary": "#263148",
    "input_background_fill": "#0f1629",
    "input_border_color": "#263148",
    "button_primary_background_fill": "#6366f1",
    "button_primary_background_fill_hover": "#4f52d9",
    "button_primary_text_color": "#ffffff",
    "button_secondary_background_fill": "#17203a",
    "button_secondary_background_fill_hover": "#1f2b4d",
    "button_secondary_text_color": "#c7d0ea",
}


def build_theme() -> gr.themes.ThemeClass:
    """A dark theme that looks the same regardless of the visitor's system setting."""
    palette = {**_DARK_PALETTE, **{f"{k}_dark": v for k, v in _DARK_PALETTE.items()}}
    return gr.themes.Base(
        primary_hue="indigo",
        neutral_hue="slate",
        font=["ui-sans-serif", "system-ui", "sans-serif"],
    ).set(**palette)


def launch_kwargs() -> dict[str, Any]:
    """App-level styling; in Gradio 6 these are passed to ``launch()``, not ``Blocks()``."""
    return {"theme": build_theme(), "css": CSS}


@dataclass
class Session:
    """Per-browser-session state: one agent (conversation) and the rendered transcript."""

    agent: Agent
    transcript: list[Message] = field(default_factory=list)


def describe_identity(user: UserContext) -> str:
    sql = "visible" if user.can_see_sql else "hidden"
    return (
        f"Signed in as **{user.name}** at **{user.org_name}** · Generated SQL: **{sql}** · "
        f"Row-level security: `org_id = {user.org_id}`"
    )


class ChatController:
    """UI-independent behavior behind the event handlers (so it can be unit-tested)."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._org_names = dict(ctx.organizations)

    def user_for(self, role_label: str, org_id: int) -> UserContext:
        return UserContext(
            name=role_label,
            role=Role(role_label.lower()),
            org_id=org_id,
            org_name=self._org_names[org_id],
        )

    def reset(self, role_label: str, org_id: int) -> tuple[None, list[Message], str]:
        """New identity or 'clear': drop the agent and transcript, refresh the banner."""
        return None, [], describe_identity(self.user_for(role_label, org_id))

    def respond(
        self, text: str | None, session: Session | None, role_label: str, org_id: int
    ) -> Iterator[tuple[list[Message], Any, Session | None]]:
        """Stream one chat turn. Yields (chat, textbox value, session) after every event."""
        question = (text or "").strip()
        if not question:
            yield (session.transcript if session else []), gr.skip(), session
            return

        if session is None:
            user = self.user_for(role_label, org_id)
            session = Session(agent=self._ctx.new_agent(user))
        transcript = session.transcript
        transcript.append({"role": "user", "content": question})

        turn = TurnView(dialect=self._ctx.runner.dialect)
        yield [*transcript], "", session  # show the question and clear the textbox at once
        try:
            for event in session.agent.stream(question):
                turn.update(event)
                yield [*transcript, *turn.messages()], gr.skip(), session
        except Exception:  # last line of defence: never leave the UI hanging
            logger.exception("Unexpected error while answering")
            turn.update(AgentEvent("error", "Something went wrong. Please try again."))
            yield [*transcript, *turn.messages()], gr.skip(), session
        transcript.extend(turn.messages())


def _with_question(
    controller: ChatController, question: str
) -> Callable[[Session | None, str, int], Iterator[tuple[list[Message], Any, Session | None]]]:
    def handler(
        session: Session | None, role_label: str, org_id: int
    ) -> Iterator[tuple[list[Message], Any, Session | None]]:
        yield from controller.respond(question, session, role_label, org_id)

    return handler


def build_demo(ctx: AppContext) -> gr.Blocks:
    controller = ChatController(ctx)
    default_org = ctx.organizations[0][0]
    default_user = controller.user_for(ROLE_CHOICES[0], default_org)

    with gr.Blocks(title="QueryPilot") as demo:
        gr.Markdown(
            "# ✈️ QueryPilot\nAsk questions about your data in plain English.",
            elem_id="header",
        )
        with gr.Row():
            role = gr.Radio(ROLE_CHOICES, value=ROLE_CHOICES[0], label="Role", scale=1)
            org = gr.Dropdown(
                choices=[(name, org_id) for org_id, name in ctx.organizations],
                value=default_org,
                label="Organization",
                scale=2,
            )
        identity = gr.Markdown(describe_identity(default_user), elem_id="identity")

        chatbot = gr.Chatbot(
            label="Conversation",
            height=520,
            placeholder="Ask a question, or pick one of the examples below.",
            elem_id="chatbot",
        )
        with gr.Row():
            question = gr.Textbox(
                placeholder="e.g. Who are our top 5 customers by revenue?",
                show_label=False,
                container=False,
                scale=8,
                autofocus=True,
            )
            send = gr.Button("Ask", variant="primary", scale=1)

        gr.Markdown("**Try asking**")
        with gr.Row(elem_id="samples"):
            sample_buttons = [gr.Button(q, size="sm") for q in SAMPLE_QUESTIONS]
        clear = gr.Button("Clear conversation", size="sm")
        gr.Markdown(
            "Switch role and organization to see row-level security and SQL visibility in "
            "action. Admins can expand **Generated SQL**; normal users only see answers and tables."
        )

        session = gr.State(None)
        outputs = [chatbot, question, session]
        options = {"concurrency_limit": CONCURRENCY_LIMIT}

        question.submit(controller.respond, [question, session, role, org], outputs, **options)
        send.click(controller.respond, [question, session, role, org], outputs, **options)
        for button, sample in zip(sample_buttons, SAMPLE_QUESTIONS, strict=True):
            handler = _with_question(controller, sample)
            button.click(handler, [session, role, org], outputs, **options)

        reset_outputs = [session, chatbot, identity]
        role.change(controller.reset, [role, org], reset_outputs)
        org.change(controller.reset, [role, org], reset_outputs)
        clear.click(controller.reset, [role, org], reset_outputs)

    return demo
