"""Finding past runs that resemble a new task.

Retrieval needs a way to measure "similar". Two implementations sit
behind one interface:

  SentenceTransformerEmbedder  a real local embedding model. Understands
                               that "file the Acme bill" and "record the
                               Acme invoice" mean nearly the same thing.
  LexicalEmbedder              hashed bag-of-words. Understands only that
                               they share the word "Acme".

The second is a fallback, chosen automatically when sentence-transformers
is not installed -- it pulls in PyTorch, roughly 2GB, which is a lot to
require of someone cloning this repo to read it.

The fallback is less clever and, at this scale, mostly good enough. With
a few dozen past tasks phrased in similar language by the same person,
word overlap finds the right neighbours nearly as often as embeddings do.
It earns its place by never failing to install, and by keeping the test
suite offline and instant.

What matters architecturally is that nothing above this file knows which
one is in use. Swapping them changes retrieval quality, not behaviour --
the same property that lets `llm/client.py` hide which model provider is
behind it.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

_WORD = re.compile(r"[a-z0-9]+")

# Words too common to say anything about similarity. Short list on
# purpose: an aggressive stop list throws away signal, and "invoice"
# genuinely is informative here even though it appears constantly.
_STOPWORDS = frozenset(
    "a an and are as at be by for from in into is it its of on or that the "
    "then there these this to was were will with".split()
)


class Embedder(Protocol):
    """Turns text into a vector. The only thing retrieval needs."""

    name: str

    def embed(self, text: str) -> list[float]: ...


class LexicalEmbedder:
    """Hashed bag-of-words. No dependencies beyond the standard library.

    Each word is hashed into one of `dimensions` buckets and counted,
    then the vector is normalised. Two texts sharing vocabulary end up
    pointing in similar directions.

    Its blind spot is real: "file the bill" and "record the invoice"
    share no words and score zero, where an embedding model would see
    them as near-identical. Worth knowing when reading retrieval results
    that look thinner than expected.
    """

    name = "lexical"

    def __init__(self, dimensions: int = 512) -> None:
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for word in _WORD.findall(text.lower()):
            if word in _STOPWORDS or len(word) < 2:
                continue
            # Hashing rather than a learned vocabulary: no training step,
            # no vocabulary file to keep in sync with the stored vectors.
            bucket = int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dimensions
            vector[bucket] += 1.0

        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector


class SentenceTransformerEmbedder:
    """A real local embedding model. Understands meaning, not just words."""

    name = "sentence-transformers"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer

        # Loading takes a few seconds and holds a few hundred MB, so it
        # happens once per process, not once per call.
        self._model = SentenceTransformer(model_name)

    def embed(self, text: str) -> list[float]:
        return self._model.encode(text, normalize_embeddings=True).tolist()


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    """The best embedder available, decided once.

    Preference is quality first, then availability. If
    sentence-transformers is installed but fails to load -- a broken
    install, no disk space for the model download -- the lexical
    fallback takes over rather than the whole agent failing. Memory is
    an enhancement; losing it should degrade the agent, not stop it.
    """
    global _embedder
    if _embedder is not None:
        return _embedder

    try:
        _embedder = SentenceTransformerEmbedder()
    except Exception:
        _embedder = LexicalEmbedder()
    return _embedder


def use_embedder(embedder: Embedder) -> None:
    """Override the embedder. For tests, and for forcing the fallback."""
    global _embedder
    _embedder = embedder


def cosine(a: list[float], b: list[float]) -> float:
    """Similarity of two vectors, 0 to 1 for non-negative inputs.

    Both embedders return normalised vectors, so this is a dot product.
    It still normalises, because a stored vector may have come from a
    different embedder than the one running now.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def most_similar(
    query: str,
    candidates: list[tuple[object, list[float]]],
    *,
    top_k: int = 3,
    threshold: float = 0.25,
) -> list[tuple[object, float]]:
    """The candidates closest to `query`, best first.

    `threshold` matters more than `top_k`. Without it, the three
    least-unrelated episodes are always returned, so a brand-new kind of
    task arrives at the planner accompanied by three irrelevant examples
    -- which is worse than no memory at all, because the planner will
    try to follow them.
    """
    if not candidates:
        return []

    query_vector = get_embedder().embed(query)
    scored = [
        (item, cosine(query_vector, vector))
        for item, vector in candidates
        if vector
    ]
    scored = [pair for pair in scored if pair[1] >= threshold]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_k]
