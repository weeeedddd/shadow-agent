"""Two-stage memory recall.

License note -- read before assuming provenance
-----------------------------------------------
``MemoriLabs/Memori`` carries a **non-standard licence** (GitHub reports
``NOASSERTION``), so its terms cannot be assumed compatible with this MIT
project. **No code was taken from it.** Its ``core/src/retrieval/pipeline.rs``
was read to confirm the pipeline shape; that shape -- candidate generation
followed by reranking -- is standard information-retrieval practice and long
predates any single implementation. Everything below is written from scratch.

The mechanism the reading confirmed
-----------------------------------
Memori's retrieval does not run one search. It runs two stages with different
jobs:

    stage 1  fetch_embeddings(entity_id, dense_limit)   wide  -- recall
    stage 2  rerank down to `limit`                     narrow -- precision

Casting wide first and narrowing second beats a single ranked query, because
the cheap first pass is allowed to be imprecise. Anything it drops is gone for
good, so its only job is *not missing things*; judgement belongs in stage 2,
where the candidate set is small enough to afford it.

Two further details worth keeping:

* **Scoped by entity.** Every retrieval is bounded to an ``entity_id`` before
  ranking. Recall across an unbounded corpus is slower *and* worse.
* **Degenerate inputs return empty, not error.** An empty query or a zero
  limit yields no results rather than raising. A recall miss is a normal
  outcome; an exception makes callers defensive about the ordinary case.

The port
--------
Memori's stage 1 is dense vector search. This framework has no embedding model
and no vector store, so stage 1 here is lexical -- token overlap over memory
keys, tags, and bodies. **The pipeline shape is the assimilated part**, and
:class:`RecallEngine` is written so stage 1 can be swapped for a real dense
index without touching stage 2 or any caller. That is the same seam Memori
draws with its ``StorageBridge`` trait: storage-agnostic core, host-supplied
retrieval.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Sequence

DEFAULT_DENSE_LIMIT = 50   # stage 1: wide
DEFAULT_LIMIT = 6          # stage 2: narrow

_TOKEN = re.compile(r"[a-z0-9_]+")

# Words that match everything and therefore discriminate nothing.
_STOPWORDS = frozenset(
    """a an the and or but if then than of to in on at by for with from into over
    is are was were be been being do does did this that these those it its as not
    no so such can will just should would could i you we they he she them my your
    what which who when where why how""".split()
)


@dataclass
class Fact:
    """One retrievable memory."""

    key: str
    value: str
    tags: List[str] = field(default_factory=list)
    entity: str = "default"
    updated_at: str = ""

    def searchable(self) -> str:
        return f"{self.key} {' '.join(self.tags)} {self.value}"


@dataclass
class RankedFact:
    fact: Fact
    score: float
    matched: List[str] = field(default_factory=list)


class FactSource(Protocol):
    """The storage seam.

    Memori's ``StorageBridge`` in miniature: the engine never knows whether
    facts come from JSON, Markdown, SQLite, or a vector index.
    """

    def facts(self, entity: str) -> Sequence[Fact]:
        ...


def tokenize(text: str) -> List[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


@dataclass
class RecallEngine:
    """Two-stage recall over a fact source."""

    source: FactSource
    dense_limit: int = DEFAULT_DENSE_LIMIT
    limit: int = DEFAULT_LIMIT
    # Swap point: supply a dense retriever and stage 1 becomes vector search.
    candidate_fn: Optional[Callable[[str, str, int], Sequence[Fact]]] = None

    # --- stage 1: wide -------------------------------------------------------

    def candidates(self, query: str, entity: str) -> List[Fact]:
        """Cast wide. Cheap, imprecise, must not miss.

        Recall-oriented on purpose: a fact dropped here can never be recovered
        by stage 2, so the bar for inclusion is *any* signal at all.
        """
        if self.candidate_fn is not None:
            return list(self.candidate_fn(query, entity, self.dense_limit))

        query_tokens = set(tokenize(query))
        if not query_tokens:
            return []

        scored: List[tuple] = []
        for fact in self.source.facts(entity):
            fact_tokens = set(tokenize(fact.searchable()))
            if not fact_tokens:
                continue
            overlap = len(query_tokens & fact_tokens)
            if overlap == 0:
                # Substring fallback: catches `auth` inside `authenticate`,
                # which exact token overlap misses entirely.
                blob = fact.searchable().lower()
                if not any(t in blob for t in query_tokens):
                    continue
                overlap = 0.5
            scored.append((overlap, fact))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [fact for _, fact in scored[: self.dense_limit]]

    # --- stage 2: narrow -----------------------------------------------------

    def rerank(self, query: str, candidates: Sequence[Fact]) -> List[RankedFact]:
        """Score precisely. The set is small, so this can afford to be careful.

        Three signals, weighted:
          * IDF-ish token overlap -- rare shared terms count for more
          * key and tag hits -- a match on the name beats one in the body
          * length normalisation -- a long fact should not win on volume
        """
        query_tokens = tokenize(query)
        if not query_tokens or not candidates:
            return []

        document_frequency: Dict[str, int] = {}
        for fact in candidates:
            for token in set(tokenize(fact.searchable())):
                document_frequency[token] = document_frequency.get(token, 0) + 1

        total = len(candidates)
        ranked: List[RankedFact] = []
        for fact in candidates:
            body_tokens = tokenize(fact.value)
            key_tokens = set(tokenize(fact.key) + tokenize(" ".join(fact.tags)))
            all_tokens = body_tokens + list(key_tokens)
            if not all_tokens:
                continue

            score = 0.0
            matched: List[str] = []
            for token in set(query_tokens):
                if token not in all_tokens:
                    continue
                matched.append(token)
                idf = math.log((total + 1) / (document_frequency.get(token, 0) + 1)) + 1.0
                score += idf * (2.5 if token in key_tokens else 1.0)

            if not matched:
                continue
            score /= 1.0 + math.log(1 + len(all_tokens) / 25.0)
            ranked.append(RankedFact(fact, round(score, 4), sorted(matched)))

        ranked.sort(key=lambda r: (-r.score, r.fact.key))
        return ranked[: self.limit]

    # --- the pipeline --------------------------------------------------------

    def recall(self, query: str, entity: str = "default") -> List[RankedFact]:
        """Wide, then narrow. Degenerate input returns empty, never raises."""
        if not query or not query.strip():
            return []
        if self.limit <= 0 or self.dense_limit <= 0:
            return []
        return self.rerank(query, self.candidates(query, entity))
