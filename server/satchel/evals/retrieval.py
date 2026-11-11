"""Retrieval eval: labelled questions -> expected item keys; reports recall@k and MRR."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from satchel.llm.embeddings import EmbeddingProvider
from satchel.retrieval.search import search_context


@dataclass
class CaseResult:
    question: str
    expected: list[str]
    got: list[str]
    recall: float
    rr: float


@dataclass
class RetrievalEvalResult:
    k: int
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def recall_at_k(self) -> float:
        return sum(c.recall for c in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def mrr(self) -> float:
        return sum(c.rr for c in self.cases) / len(self.cases) if self.cases else 0.0


def load_cases(path: str | Path) -> list[tuple[str, list[str]]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases = raw["cases"] if isinstance(raw, dict) else raw
    return [(str(c["question"]), [str(k) for k in c["expect"]]) for c in cases]


def run_retrieval_eval(
    session: Session,
    cases: list[tuple[str, list[str]]],
    embedder: EmbeddingProvider,
    k: int = 5,
) -> RetrievalEvalResult:
    """Recall@k = mean fraction of expected keys in the top k; MRR uses the first expected hit."""
    result = RetrievalEvalResult(k=k)
    for question, expected in cases:
        hits = search_context(session, question, k=max(k, 20), embedder=embedder)
        keys = [h.key for h in hits]
        top = set(keys[:k])
        recall = sum(e in top for e in expected) / len(expected)
        ranks = [keys.index(e) + 1 for e in expected if e in keys]
        rr = 1.0 / min(ranks) if ranks else 0.0
        result.cases.append(CaseResult(question, expected, keys[:k], recall, rr))
    return result
