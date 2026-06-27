"""Optional embedding-based semantic search for `deja find --semantic` (PLAN.md §8 #4).

Fuzzy text search (``deja.search``) is great when the query shares *words* with
the function — but it whiffs on pure intent ("turn a blob of HTML into clean
text") when the existing function is named ``strip_markup`` with a terse
docstring. Semantic search ranks by *meaning*: it embeds each function and the
query into the same vector space and ranks by cosine similarity.

Design constraints (issue #9 / PLAN.md §9 "no mandatory LLM in the core"):

* **Zero impact when off.** This module is only imported when ``--semantic`` is
  passed; the heavy backend (``sentence-transformers`` / ``torch``) is imported
  *lazily inside functions*, never at module top level. ``import deja.semantic``
  stays cheap and dependency-free.
* **Graceful fallback.** :func:`load_backend` returns ``None`` (with a reason)
  when no backend is installed, so the CLI can fall back to fuzzy search with a
  clear message instead of crashing.
* **Cached + incremental.** Embeddings are persisted to
  ``.dejafunc/embeddings.json`` keyed by a content fingerprint of each function.
  Re-running only embeds *new or changed* functions; everything else is reused.

Two backends are supported, tried in order (override with ``DEJA_EMBED_BACKEND``):

* ``sentence-transformers`` — local model (default
  ``all-MiniLM-L6-v2``, override via ``DEJA_EMBED_MODEL``).
* ``ollama`` — a running Ollama daemon (model via ``DEJA_EMBED_MODEL``,
  default ``nomic-embed-text``); uses only ``urllib`` from the stdlib.

The backend is an injectable :class:`EmbeddingBackend` protocol so tests (and
future backends) never need a real model.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .index import INDEX_DIR
from .parsers import FunctionRecord

#: Embedding cache filename inside :data:`deja.index.INDEX_DIR`.
EMBED_FILE = "embeddings.json"
#: Bump when the on-disk embedding cache shape changes incompatibly.
EMBED_SCHEMA_VERSION = 1

#: Default sentence-transformers model — small, fast, no GPU required.
DEFAULT_ST_MODEL = "all-MiniLM-L6-v2"
#: Default Ollama embedding model.
DEFAULT_OLLAMA_MODEL = "nomic-embed-text"
#: Default Ollama endpoint (overridable via ``OLLAMA_HOST``).
DEFAULT_OLLAMA_HOST = "http://localhost:11434"

#: Cosine-similarity scores below this (after 0-100 scaling) are dropped as noise.
MIN_SCORE = 35.0
#: Default number of semantic matches to return (mirrors fuzzy DEFAULT_LIMIT).
DEFAULT_LIMIT = 10


@runtime_checkable
class EmbeddingBackend(Protocol):
    """A thing that turns strings into fixed-length float vectors.

    Implementations must return one vector per input text, in order, all of the
    same dimensionality. Stateless from the caller's perspective.
    """

    #: Short identifier recorded in the cache (e.g. ``sentence-transformers:all-MiniLM-L6-v2``).
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per text in *texts* (same order)."""
        ...


@dataclass(frozen=True, slots=True)
class ScoredRecord:
    """A :class:`FunctionRecord` paired with its semantic similarity (0-100)."""

    record: FunctionRecord
    score: float


# -- text + fingerprint ----------------------------------------------------


def record_text(record: FunctionRecord) -> str:
    """Build the natural-language string we embed for *record*.

    We fold the human-meaningful signals — qualified name (de-underscored so word
    boundaries survive), signature, and docstring — into one short document. This
    is what makes intent queries land: the docstring usually carries the meaning,
    the name carries the concept.
    """
    name = (record.qualname or record.name).replace("_", " ").replace(".", " ")
    parts = [name.strip()]
    if record.signature and record.signature != "()":
        parts.append(record.signature)
    if record.docstring:
        parts.append(record.docstring)
    return " — ".join(p for p in parts if p)


def fingerprint(record: FunctionRecord) -> str:
    """Stable content hash of the embed-relevant fields of *record*.

    Keyed on the *text we embed* (plus location, so two identical bodies in
    different files get distinct cache entries). Changing a docstring or
    signature changes the fingerprint, so the cache re-embeds exactly the
    functions that actually changed — the incremental-update contract (issue #9).
    """
    basis = f"{record.file}\0{record.line}\0{record_text(record)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# -- backends --------------------------------------------------------------


class _SentenceTransformerBackend:
    """Backend wrapping a local ``sentence-transformers`` model (lazy-loaded)."""

    def __init__(self, model_name: str) -> None:
        # Heavy import stays here, never at module import time.
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.name = f"sentence-transformers:{model_name}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return [[float(x) for x in row] for row in vecs]


class _OllamaBackend:
    """Backend talking to a local Ollama daemon over HTTP (stdlib only)."""

    def __init__(self, model_name: str, host: str) -> None:
        self._model = model_name
        self._host = host.rstrip("/")
        self.name = f"ollama:{model_name}"
        # Fail fast if the daemon isn't reachable, so we fall back cleanly.
        self._probe()

    def _probe(self) -> None:
        import urllib.request

        req = urllib.request.Request(f"{self._host}/api/tags")
        with urllib.request.urlopen(req, timeout=2):  # noqa: S310 - localhost only
            return

    def embed(self, texts: list[str]) -> list[list[float]]:
        import urllib.request

        out: list[list[float]] = []
        for text in texts:
            payload = json.dumps({"model": self._model, "prompt": text}).encode("utf-8")
            req = urllib.request.Request(
                f"{self._host}/api/embeddings",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - localhost
                data = json.loads(resp.read().decode("utf-8"))
            out.append([float(x) for x in data.get("embedding", [])])
        return out


def load_backend(prefer: str | None = None) -> tuple[EmbeddingBackend | None, str]:
    """Return ``(backend, message)`` for the first available embedding backend.

    Tries backends in order (``sentence-transformers`` then ``ollama``), honoring
    ``DEJA_EMBED_BACKEND`` / *prefer* to force one. On success the message names
    the chosen backend; on failure ``backend`` is ``None`` and the message
    explains what to install — the CLI surfaces this and falls back to fuzzy
    search (issue #9 acceptance: "graceful fallback + clear message").

    Args:
        prefer: Force a specific backend (``"sentence-transformers"`` or
            ``"ollama"``). Defaults to ``DEJA_EMBED_BACKEND`` then auto-detect.

    Returns:
        ``(backend_or_None, human_message)``.
    """
    choice = (prefer or os.environ.get("DEJA_EMBED_BACKEND") or "").strip().lower()
    order: list[str]
    if choice in {"sentence-transformers", "sentence_transformers", "st"}:
        order = ["st"]
    elif choice in {"ollama"}:
        order = ["ollama"]
    elif choice:
        return None, (
            f"unknown embedding backend {choice!r} (try 'sentence-transformers' or 'ollama')"
        )
    else:
        order = ["st", "ollama"]

    reasons: list[str] = []
    for backend in order:
        if backend == "st":
            model = os.environ.get("DEJA_EMBED_MODEL") or DEFAULT_ST_MODEL
            try:
                impl = _SentenceTransformerBackend(model)
                return impl, f"using {impl.name}"
            except ImportError:
                reasons.append(
                    "sentence-transformers not installed (pip install 'deja-func[semantic]')"
                )
            except Exception as exc:  # pragma: no cover - model load/runtime issues
                reasons.append(f"sentence-transformers failed to load: {exc}")
        elif backend == "ollama":
            model = os.environ.get("DEJA_EMBED_MODEL") or DEFAULT_OLLAMA_MODEL
            host = os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST
            try:
                impl = _OllamaBackend(model, host)
                return impl, f"using {impl.name}"
            except Exception:
                reasons.append(f"ollama not reachable at {host}")

    detail = "; ".join(reasons) if reasons else "no embedding backend available"
    return None, f"semantic search unavailable: {detail}"


# -- cache -----------------------------------------------------------------


def embed_cache_path(root: str | os.PathLike[str]) -> Path:
    """Return the embedding cache path for *root* (``.dejafunc/embeddings.json``)."""
    return Path(root) / INDEX_DIR / EMBED_FILE


def _load_cache(root: str | os.PathLike[str], *, backend_name: str) -> dict[str, list[float]]:
    """Load the fingerprint→vector cache, ignoring stale/foreign entries.

    The cache is scoped to a backend: switching models (different vector space)
    invalidates it wholesale, since cosine similarity across models is meaningless.
    """
    path = embed_cache_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover - corrupt cache
        return {}
    if data.get("schema_version") != EMBED_SCHEMA_VERSION:
        return {}
    if data.get("backend") != backend_name:
        return {}
    vectors = data.get("vectors", {})
    if not isinstance(vectors, dict):  # pragma: no cover - corrupt cache
        return {}
    return {k: [float(x) for x in v] for k, v in vectors.items()}


def _save_cache(
    root: str | os.PathLike[str],
    *,
    backend_name: str,
    vectors: dict[str, list[float]],
) -> Path:
    """Persist *vectors* (fingerprint→embedding) for *backend_name*."""
    path = embed_cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": EMBED_SCHEMA_VERSION,
        "backend": backend_name,
        "count": len(vectors),
        "vectors": vectors,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def embed_records(
    records: list[FunctionRecord],
    backend: EmbeddingBackend,
    *,
    root: str | os.PathLike[str] | None = None,
) -> dict[str, list[float]]:
    """Return ``fingerprint -> vector`` for *records*, embedding only what's new.

    Reuses any cached vectors under *root* whose fingerprint still matches, embeds
    the remainder in a single batched backend call, and writes the merged result
    back. This is the incremental-update path (issue #9): edit one function and
    only that function is re-embedded next run.

    Args:
        records: Functions to embed (typically ``index.records``).
        backend: The embedding backend to use for cache-miss records.
        root: Repo root for the on-disk cache. When ``None`` the cache is skipped
            entirely (pure in-memory embedding).

    Returns:
        A dict mapping each record's :func:`fingerprint` to its embedding vector.
    """
    cache: dict[str, list[float]] = {}
    if root is not None:
        cache = _load_cache(root, backend_name=backend.name)

    wanted = {fingerprint(r): r for r in records}
    missing_fps = [fp for fp in wanted if fp not in cache]

    fresh: dict[str, list[float]] = {}
    if missing_fps:
        texts = [record_text(wanted[fp]) for fp in missing_fps]
        vectors = backend.embed(texts)
        fresh = dict(zip(missing_fps, vectors, strict=True))

    result = {fp: (cache[fp] if fp in cache else fresh[fp]) for fp in wanted}

    if root is not None:
        # Rewrite when we embedded anything new *or* when the on-disk cache holds
        # vectors for functions that no longer exist (prune so the cache tracks
        # the live inventory and never grows unbounded). Skip the write only when
        # the cache is already exactly the wanted set.
        stale = bool(set(cache) - set(wanted))
        if missing_fps or stale:
            _save_cache(root, backend_name=backend.name, vectors=result)

    return result


# -- similarity + search ---------------------------------------------------


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors, in ``[-1, 1]`` (0 if degenerate)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _scale(cos: float) -> float:
    """Map a cosine similarity in ``[-1, 1]`` onto a ``[0, 100]`` score."""
    return max(0.0, min(100.0, (cos + 1.0) * 50.0))


def semantic_search(
    query: str,
    records: list[FunctionRecord],
    backend: EmbeddingBackend,
    *,
    limit: int = DEFAULT_LIMIT,
    min_score: float = MIN_SCORE,
    root: str | os.PathLike[str] | None = None,
) -> list[ScoredRecord]:
    """Rank *records* against *query* by embedding cosine similarity, best first.

    Args:
        query: Natural-language intent (e.g. "turn HTML into plain text").
        records: Candidate functions (typically ``index.records``).
        backend: Embedding backend (from :func:`load_backend`).
        limit: Maximum matches to return.
        min_score: Drop matches scoring below this (0-100).
        root: Repo root for the embedding cache (incremental reuse). ``None``
            disables the on-disk cache.

    Returns:
        Up to *limit* :class:`ScoredRecord` s sorted by score (desc); ties break
        on ``(file, line)`` for deterministic output.
    """
    q = query.strip()
    if not q or not records:
        return []

    by_fp = embed_records(records, backend, root=root)
    query_vec = backend.embed([q])[0]

    scored: list[ScoredRecord] = []
    for r in records:
        vec = by_fp[fingerprint(r)]
        score = _scale(cosine(query_vec, vec))
        scored.append(ScoredRecord(record=r, score=score))

    scored = [s for s in scored if s.score >= min_score]
    scored.sort(key=lambda s: (-s.score, s.record.file, s.record.line))
    return scored[: max(0, limit)]
