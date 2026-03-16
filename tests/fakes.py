"""Shared test doubles for the OpenAI client and agent memory."""

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

from app.agent.memory import MemoryExample


class FakeCompletions:
    """Returns scripted responses; an Exception item is raised instead."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fake_client(*responses) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(list(responses))))


def tool_call(sql: str, call_id: str = "call_1"):
    call = SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name="run_sql", arguments=json.dumps({"sql": sql})),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))]
    )


def text_reply(text: str):
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@dataclass
class FakeMemory:
    examples: list[MemoryExample] = field(default_factory=list)
    saved: list[dict] = field(default_factory=list)
    searches: list[tuple] = field(default_factory=list)
    fail: bool = False

    def search(self, question: str, *, org_id: int, k: int) -> list[MemoryExample]:
        if self.fail:
            raise RuntimeError("chroma is down")
        self.searches.append((question, org_id, k))
        return self.examples

    def save(self, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("chroma is down")
        self.saved.append(kwargs)
