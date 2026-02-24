"""The agent: an OpenAI tool-calling loop that emits progress events as it works."""

from __future__ import annotations

import logging
from collections.abc import Generator, Iterator
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from openai import OpenAI, OpenAIError
from pydantic import ValidationError

from app.agent.prompts import build_system_prompt
from app.agent.tools import (
    RUN_SQL_NAME,
    RUN_SQL_TOOL,
    RunSqlArgs,
    ToolOutcome,
    failure,
    run_sql_tool,
)
from app.config import Settings
from app.context import UserContext
from app.db.runner import QueryResult, SqlRunner, describe_schema

if TYPE_CHECKING:
    from app.agent.memory import MemoryExample, MemoryStore

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 20

EventKind = Literal["status", "sql", "result", "answer", "error"]


@dataclass(frozen=True)
class AgentEvent:
    """One progress update. ``answer`` and ``error`` are terminal.

    ``detail`` carries technical information (SQL errors, API errors) that only admins see.
    """

    kind: EventKind
    message: str = ""
    sql: str | None = None
    result: QueryResult | None = None
    detail: str | None = None


@dataclass(frozen=True)
class AgentResponse:
    """Collected outcome of :meth:`Agent.ask` (non-streaming convenience)."""

    answer: str | None
    error: str | None
    sql: str | None
    result: QueryResult | None


class Agent:
    """One conversation for one user. Create a new Agent when the user or session changes."""

    def __init__(
        self,
        settings: Settings,
        runner: SqlRunner,
        user: UserContext,
        *,
        memory: MemoryStore | None = None,
        client: OpenAI | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner
        self._user = user
        self._memory = memory
        self._client = client or OpenAI(api_key=settings.require_openai_api_key())
        self._schema = describe_schema(runner.engine)
        self._history: list[dict[str, str]] = []

    @property
    def user(self) -> UserContext:
        return self._user

    def reset(self) -> None:
        """Forget the conversation so far."""
        self._history.clear()

    def ask(self, question: str) -> AgentResponse:
        """Run the agent to completion and return the final outcome."""
        answer = error = sql = None
        result: QueryResult | None = None
        for event in self.stream(question):
            if event.kind == "sql":
                sql = event.sql
            elif event.kind == "result":
                result = event.result
            elif event.kind == "answer":
                answer = event.message
            elif event.kind == "error":
                error = f"{event.message} {event.detail}" if event.detail else event.message
        return AgentResponse(answer=answer, error=error, sql=sql, result=result)

    def stream(self, question: str) -> Iterator[AgentEvent]:
        """Answer ``question``, yielding events as they happen, filtered for the user's role."""
        for event in self._run(question):
            visible = self._redact(event)
            if visible is not None:
                yield visible

    def _redact(self, event: AgentEvent) -> AgentEvent | None:
        """Enforce 'only admins see SQL' here, so no UI can accidentally leak it."""
        if self._user.can_see_sql:
            return event
        if event.kind == "sql":
            return None
        if event.kind == "result" and event.result is not None:
            return replace(event, result=replace(event.result, sql=""))
        return replace(event, detail=None) if event.detail else event

    def _run(self, question: str) -> Generator[AgentEvent, None, None]:
        logger.info(
            "Question from %s (%s, org %s): %s",
            self._user.name,
            self._user.role,
            self._user.org_id,
            question,
        )
        yield AgentEvent("status", "Thinking...")
        examples = yield from self._recall(question)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(examples)},
            *self._history,
            {"role": "user", "content": question},
        ]
        last_success: ToolOutcome | None = None
        max_iterations = self._settings.max_tool_iterations

        for _ in range(max_iterations):
            try:
                response = self._client.chat.completions.create(
                    model=self._settings.openai_model,
                    messages=messages,
                    tools=[RUN_SQL_TOOL],
                )
            except OpenAIError as exc:
                logger.exception("OpenAI request failed")
                yield AgentEvent("error", "The language model request failed.", detail=str(exc))
                return

            message = response.choices[0].message
            tool_calls = message.tool_calls or []
            if not tool_calls:
                answer = (message.content or "").strip() or "I couldn't produce an answer."
                self._remember(question, answer, last_success.sql if last_success else None)
                self._learn(question, answer, last_success)
                yield AgentEvent("answer", answer)
                return

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                outcome = yield from self._run_tool_call(call)
                if outcome.result is not None:
                    last_success = outcome
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": outcome.content}
                )
            yield AgentEvent("status", "Analyzing results...")

        yield AgentEvent(
            "error",
            f"I couldn't reach an answer within {max_iterations} tool calls. "
            "Try rephrasing or narrowing the question.",
        )

    def _run_tool_call(self, call: Any) -> Generator[AgentEvent, None, ToolOutcome]:
        """Execute one tool call, yielding events; the generator's return value is the outcome."""
        if call.type != "function" or call.function.name != RUN_SQL_NAME:
            return failure("Unknown tool.")

        try:
            args = RunSqlArgs.model_validate_json(call.function.arguments)
        except ValidationError:
            yield AgentEvent("status", "The model sent invalid tool arguments; retrying...")
            return failure('Invalid arguments. Expected JSON like {"sql": "SELECT ..."}.')

        yield AgentEvent("sql", sql=args.sql)
        yield AgentEvent("status", "Executing query...")
        outcome = run_sql_tool(self._runner, args.sql, self._user)
        if outcome.result is not None:
            logger.info(
                "Query OK: %d row(s) in %.0f ms",
                outcome.result.row_count,
                outcome.result.elapsed_ms,
            )
            yield AgentEvent("result", result=outcome.result)
        else:
            logger.warning("Query failed: %s", outcome.error)
            yield AgentEvent(
                "status",
                "The query failed; asking the model to fix it...",
                detail=outcome.error,
            )
        return outcome

    def _recall(self, question: str) -> Generator[AgentEvent, None, list[MemoryExample]]:
        """Fetch similar past queries. Memory is best-effort: failures never break an answer."""
        if self._memory is None:
            return []
        yield AgentEvent("status", "Checking past queries...")
        try:
            examples = self._memory.search(
                question, org_id=self._user.org_id, k=self._settings.memory_top_k
            )
        except Exception:
            logger.warning("Memory search failed", exc_info=True)
            return []
        if examples:
            yield AgentEvent("status", f"Found {len(examples)} similar past queries.")
        return examples

    def _learn(self, question: str, answer: str, outcome: ToolOutcome | None) -> None:
        """Save the last successful, non-empty query so future questions can reuse it."""
        if self._memory is None or outcome is None or outcome.sql is None:
            return
        if outcome.result is None or outcome.result.row_count == 0:
            return
        try:
            self._memory.save(
                question=question, sql=outcome.sql, summary=answer, org_id=self._user.org_id
            )
        except Exception:
            logger.warning("Memory save failed", exc_info=True)

    def _system_prompt(self, examples: list[MemoryExample]) -> str:
        return build_system_prompt(
            schema=self._schema,
            dialect=self._runner.dialect,
            today=date.today(),
            user=self._user,
            examples=examples,
        )

    def _remember(self, question: str, answer: str, sql: str | None) -> None:
        """Keep the turn for follow-ups; the SQL is included so the model can refine it."""
        assistant_text = f"{answer}\n\n[SQL used: {sql}]" if sql else answer
        self._history += [
            {"role": "user", "content": question},
            {"role": "assistant", "content": assistant_text},
        ]
        del self._history[:-MAX_HISTORY_MESSAGES]
