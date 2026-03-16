import re
import zlib

import pytest

from app.agent.memory import AgentMemory

DIMS = 512


def fake_embedder(texts: list[str]) -> list[list[float]]:
    """Deterministic bag-of-words embedding so tests need no network."""
    vectors = []
    for text in texts:
        vector = [0.0] * DIMS
        for word in re.findall(r"[a-z]+", text.lower()):
            vector[zlib.crc32(word.encode()) % DIMS] += 1.0
        vectors.append(vector)
    return vectors


@pytest.fixture
def memory(tmp_path) -> AgentMemory:
    return AgentMemory(tmp_path / "chroma", fake_embedder, max_distance=0.5)


def test_empty_memory_returns_nothing(memory: AgentMemory) -> None:
    assert memory.search("anything", org_id=1) == []


def test_similar_questions_are_found(memory: AgentMemory) -> None:
    memory.save(
        question="top customers by revenue", sql="SELECT 1", summary="Emma leads.", org_id=1
    )
    memory.save(question="list all products", sql="SELECT 2", summary="Ten products.", org_id=1)

    found = memory.search("top customers by revenue last month", org_id=1, k=4)
    assert [e.sql for e in found] == ["SELECT 1"]
    assert found[0].summary == "Emma leads."
    assert 0 <= found[0].distance <= 0.5


def test_memory_is_isolated_per_organization(memory: AgentMemory) -> None:
    memory.save(question="top customers by revenue", sql="SELECT 1", summary="s", org_id=1)
    assert memory.search("top customers by revenue", org_id=2) == []
    assert len(memory.search("top customers by revenue", org_id=1)) == 1


def test_saving_the_same_question_replaces_the_entry(memory: AgentMemory) -> None:
    memory.save(question="Top customers?", sql="SELECT old", summary="s", org_id=1)
    memory.save(question="  top   customers? ", sql="SELECT new", summary="s", org_id=1)
    assert len(memory) == 1
    assert memory.search("top customers", org_id=1)[0].sql == "SELECT new"
