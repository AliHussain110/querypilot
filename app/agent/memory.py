"""Agent memory: successful question -> SQL pairs stored in ChromaDB.

Embeddings are computed by us (OpenAI) and handed to Chroma, so Chroma is used purely as a
persistent vector store. Examples are stored per organization and only retrieved for that
organization, so literals from one tenant's questions never reach another tenant's prompt.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import chromadb
from openai import OpenAI

logger = logging.getLogger(__name__)

Embedder = Callable[[list[str]], list[list[float]]]
COLLECTION_NAME = "successful_queries"
_MAX_SUMMARY_CHARS = 500


@dataclass(frozen=True)
class MemoryExample:
    question: str
    sql: str
    summary: str
    distance: float  # cosine distance: 0 = identical, larger = less similar


class MemoryStore(Protocol):
    """What the agent needs from a memory (lets tests substitute a fake)."""

    def search(self, question: str, *, org_id: int, k: int) -> list[MemoryExample]: ...

    def save(self, *, question: str, sql: str, summary: str, org_id: int) -> None: ...


def openai_embedder(client: OpenAI, model: str) -> Embedder:
    def embed(texts: list[str]) -> list[list[float]]:
        response = client.embeddings.create(model=model, input=texts)
        return [item.embedding for item in response.data]

    return embed


class AgentMemory:
    def __init__(
        self,
        path: Path,
        embedder: Embedder,
        *,
        max_distance: float = 0.55,
        collection_name: str = COLLECTION_NAME,
    ) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self._embed = embedder
        self._max_distance = max_distance
        self._collection = chromadb.PersistentClient(path=str(path)).get_or_create_collection(
            name=collection_name,
            embedding_function=None,  # we supply embeddings explicitly
            metadata={"hnsw:space": "cosine"},
        )

    def __len__(self) -> int:
        return self._collection.count()

    def save(self, *, question: str, sql: str, summary: str, org_id: int) -> None:
        """Remember a successful pair. Re-asking the same question replaces the old entry."""
        self._collection.upsert(
            ids=[_example_id(org_id, question)],
            embeddings=[self._embed([question])[0]],
            documents=[question],
            metadatas=[
                {
                    "org_id": org_id,
                    "sql": sql,
                    "summary": summary[:_MAX_SUMMARY_CHARS],
                    "created_at": datetime.now(UTC).isoformat(),
                }
            ],
        )
        logger.debug("Saved memory for org %s: %s", org_id, question)

    def search(self, question: str, *, org_id: int, k: int = 5) -> list[MemoryExample]:
        """Return up to ``k`` similar past questions for this org, closest first."""
        if len(self) == 0:
            return []
        found = self._collection.query(
            query_embeddings=[self._embed([question])[0]],
            n_results=k,
            where={"org_id": org_id},
            include=["documents", "metadatas", "distances"],
        )
        documents = (found.get("documents") or [[]])[0]
        metadatas = (found.get("metadatas") or [[]])[0]
        distances = (found.get("distances") or [[]])[0]

        examples = [
            MemoryExample(
                question=doc,
                sql=str(meta["sql"]),
                summary=str(meta.get("summary", "")),
                distance=float(distance),
            )
            for doc, meta, distance in zip(documents, metadatas, distances, strict=True)
            if distance <= self._max_distance
        ]
        return sorted(examples, key=lambda e: e.distance)


def _example_id(org_id: int, question: str) -> str:
    normalized = " ".join(question.lower().split())
    return hashlib.sha256(f"{org_id}:{normalized}".encode()).hexdigest()[:32]
