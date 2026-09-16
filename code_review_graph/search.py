"""Hybrid search engine combining FTS5 (BM25) and vector embeddings.

Uses Reciprocal Rank Fusion (RRF) to merge results from full-text search
and semantic similarity, with query-aware kind boosting and context-file
boosting for relevance tuning.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Optional

from .graph import GraphStore, _sanitize_name, identifier_words
from .migrations import NODES_FTS_DDL
from .parser import normalize_file_path

logger = logging.getLogger(__name__)

# Per-column BM25 weights for the FTS5 index, looked up by column name so the
# weight vector follows the table's real shape (see NODES_FTS_DDL). A name hit
# is the strongest evidence; a docstring hit is the weakest, which is what
# keeps a symbol ahead of the prose that merely mentions it.
_FTS_COLUMN_WEIGHTS: dict[str, float] = {
    "name": 10.0,
    "qualified_name": 6.0,
    "file_path": 1.0,
    "signature": 2.0,
    "docstring": 1.5,
    "name_tokens": 4.0,
}
_FTS_DEFAULT_WEIGHT = 1.0

# Score multipliers for a query that is exactly one identifier. The first
# applies when the node's own name is that identifier, the second when the
# identifier is the tail of its qualified name (a method named by its class).
_EXACT_NAME_BOOST = 4.0
_EXACT_SYMBOL_BOOST = 3.0

# A symbol named after the query is almost always the wanted one. BM25 cannot
# say so here: every row carries its whole file path in two columns, so path
# tokens dominate the length normalization and a long test name that repeats
# the query words outscores the short function the query describes. These
# multiply a hit by how much of the query its own symbol name accounts for.
_NAME_COVERAGE_BOOST = 1.0
_NAME_EXACT_COVERAGE_BOOST = 2.0


# ---------------------------------------------------------------------------
# FTS5 index management
# ---------------------------------------------------------------------------


def rebuild_fts_index(store: GraphStore) -> int:
    """Rebuild the FTS5 index from the nodes table.

    Checks whether the ``nodes_fts`` virtual table exists, clears it, then
    repopulates it from every row in ``nodes``.

    Returns:
        Number of rows indexed.
    """
    # NOTE: rebuild_fts_index uses store._conn directly because it manages
    # the FTS5 virtual table DDL, which is tightly coupled to SQLite internals.
    conn = store._conn

    # Wrap the full DROP + CREATE + INSERT sequence in an explicit transaction
    # so a crash mid-rebuild cannot leave the DB without an FTS table at all
    # (DROP succeeded but CREATE/INSERT didn't).  See #259.
    if conn.in_transaction:
        logger.warning("Rolling back uncommitted transaction before BEGIN IMMEDIATE")
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Drop and recreate the FTS table with content sync. The DDL lives in
        # migrations so this cannot drift from the shape migration v11 creates.
        conn.execute("DROP TABLE IF EXISTS nodes_fts")
        conn.execute(NODES_FTS_DDL)

        # Rebuild from the content table (nodes) using the FTS5 rebuild command
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")


        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    count = conn.execute("SELECT count(*) FROM nodes_fts").fetchone()[0]
    logger.info("FTS index rebuilt: %d rows indexed", count)
    return count


# ---------------------------------------------------------------------------
# Query kind boosting heuristics
# ---------------------------------------------------------------------------


# A query that is one identifier and nothing else: the user is naming a
# symbol, not describing behaviour.
_EXACT_IDENT_QUERY_RE = re.compile(r'^[A-Za-z_][\w.]*$')
_DOTTED_IDENT_RE = re.compile(r'\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+\b')
_SNAKE_IDENT_RE = re.compile(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b')
_PASCAL_IDENT_RE = re.compile(r'\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b')


def extract_query_identifiers(query: str) -> list[str]:
    """Pull out identifier-shaped tokens from anywhere in a query.

    Catches dotted forms (``Context.Next``), snake_case (``get_dependant``),
    and CamelCase (``APIRoute``) even when they're embedded in a natural-
    language sentence. Used to boost search hits whose qualified_name
    contains any of these tokens, so an LLM asking "Who advances the gin
    middleware chain via Context.Next" lands on ``Context.Next`` instead of
    the bare ``Context`` class.
    """
    found: list[str] = []
    seen: set[str] = set()
    for pat in (_DOTTED_IDENT_RE, _SNAKE_IDENT_RE, _PASCAL_IDENT_RE):
        for match in pat.findall(query):
            lo = match.lower()
            if lo not in seen and len(lo) >= 3:
                seen.add(lo)
                found.append(lo)
    return found


def detect_query_kind_boost(query: str) -> dict[str, Any]:
    """Detect query patterns and return per-node boost multipliers.

    Heuristics:
    - PascalCase queries (e.g. ``MyClass``) boost Class/Type by 1.5x
    - snake_case queries (e.g. ``get_users``) boost Function by 1.5x
    - Queries containing ``.`` boost qualified name matches by 2.0x
    - Identifier-shaped tokens *anywhere* in the query (dotted, snake_case,
      CamelCase) boost results whose qualified_name contains them by 2.0x.
      See ``extract_query_identifiers``.
    - A query that is *only* an identifier names one symbol, so the node
      whose name is exactly that identifier is boosted hardest. This is what
      keeps ``_sanitize_name`` first now that prefix matching widens the
      candidate set and docstrings that merely mention a symbol are indexed.

    Returns:
        Dict whose keys are either node kind strings (mapped to float
        multipliers) or one of the special keys ``_qualified``,
        ``_qualified_identifiers``, ``_exact_name``.
    """
    boosts: dict[str, Any] = {}

    if not query or not query.strip():
        return boosts

    q = query.strip()

    # PascalCase: starts with uppercase, has at least one lowercase after
    if re.match(r'^[A-Z][a-z]', q) and not q.isupper():
        boosts["Class"] = 1.5
        boosts["Type"] = 1.5

    # snake_case or SCREAMING_SNAKE_CASE: contains underscore with letters
    if '_' in q and re.search(r'[a-zA-Z]', q):
        boosts["Function"] = 1.5

    # Dotted path: boost qualified name matches
    if '.' in q:
        boosts["_qualified"] = 2.0

    # Identifiers extracted from anywhere in the query
    idents = extract_query_identifiers(q)
    if idents:
        boosts["_qualified_identifiers"] = idents

    # The whole query is one identifier: an exact symbol match outranks
    # everything that merely contains or documents it.
    if _EXACT_IDENT_QUERY_RE.match(q):
        boosts["_exact_name"] = q.lower()

    return boosts


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def rrf_merge(*result_lists: list[tuple[int, float]], k: int = 60) -> list[tuple[int, float]]:
    """Merge multiple ranked result lists using Reciprocal Rank Fusion.

    Each input list contains ``(id, score)`` tuples, ordered by score
    descending. The RRF score for each item is the sum of
    ``1 / (k + rank + 1)`` across all lists it appears in, where rank is
    the 0-based position.

    Args:
        *result_lists: Variable number of ranked result lists.
        k: RRF constant (default 60). Higher values reduce the impact of
           rank differences.

    Returns:
        Merged list of ``(id, rrf_score)`` tuples sorted by score descending.
    """
    scores: dict[int, float] = {}

    for result_list in result_lists:
        for rank, (item_id, _score) in enumerate(result_list):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)

    merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return merged


# ---------------------------------------------------------------------------
# FTS5 search
# ---------------------------------------------------------------------------


_EXPLICIT_PHRASE_RE = re.compile(r'"([^"]*)"')
_HAS_ALNUM_RE = re.compile(r"[0-9A-Za-z]")
# A prefix term is only useful once it carries some signal. "a*" matches
# nearly every document in the index, so short tokens stay exact.
_MIN_PREFIX_ALNUM = 3


def _quote_term(text: str) -> str:
    """Return *text* as a single FTS5 string, with inner quotes doubled.

    Quoting is what keeps the term a term: it neutralizes the operators
    (``AND``/``OR``/``NOT``/``NEAR``), the column filter ``col:``, the
    initial-token ``^``, parentheses and ``*`` that a user may legitimately
    have typed as part of what they are looking for.
    """
    return '"' + text.replace('"', '""') + '"'


def _alnum_count(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


def build_fts_queries(query: str) -> list[str]:
    """Build the FTS5 MATCH expressions to try for *query*, in order.

    The previous implementation wrapped the entire query in one pair of
    double quotes, which made every search an exact-adjacency phrase match:
    ``password hashing`` found nothing unless some node carried those two
    tokens side by side. Instead the query is split into tokens, each token
    is quoted (and given a prefix ``*`` when long enough), and the tokens are
    combined:

    * the first expression ``AND``s the tokens, which is the precise reading
      of a multi-word query;
    * the second ``OR``s them, and is only reached when the ``AND`` finds
      nothing, so recall is widened without ever costing precision.

    A phrase the user typed in double quotes is kept as a phrase, takes no
    prefix, and suppresses the ``OR`` widening: the quotes are a request for
    exactness and are honoured.

    Degenerate inputs collapse to an empty list, which the caller treats as
    "FTS has nothing to say" and falls through to the LIKE keyword path:
    an empty query, whitespace, and text with no alphanumeric character at
    all (``...``).

    Returns:
        Zero, one or two FTS5 MATCH expressions.
    """
    if not query or not query.strip():
        return []

    phrases: list[str] = []
    loose: list[str] = []
    cursor = 0
    for match in _EXPLICIT_PHRASE_RE.finditer(query):
        loose.extend(query[cursor:match.start()].split())
        phrases.append(match.group(1))
        cursor = match.end()
    loose.extend(query[cursor:].split())

    terms: list[str] = []
    for phrase in phrases:
        if _HAS_ALNUM_RE.search(phrase):
            terms.append(_quote_term(phrase))
    for token in loose:
        if not _HAS_ALNUM_RE.search(token):
            continue
        prefix = "*" if _alnum_count(token) >= _MIN_PREFIX_ALNUM else ""
        terms.append(_quote_term(token) + prefix)

    if not terms:
        return []
    if len(terms) == 1:
        return [terms[0]]

    conjunction = " AND ".join(terms)
    if phrases:
        return [conjunction]
    return [conjunction, " OR ".join(terms)]


def _covered_word_count(query_words: set[str], symbol_words: set[str]) -> int:
    """Count query words this symbol's own name accounts for.

    A query word counts when a symbol word equals it or starts with it, which
    is the same rule the prefix terms in :func:`build_fts_queries` apply, so
    ``parse`` is accounted for by ``CodeParser``. Words shorter than the
    prefix threshold must match exactly, for the same reason a one or two
    character prefix term is not issued.
    """
    covered = 0
    for word in query_words:
        if word in symbol_words:
            covered += 1
        elif len(word) >= _MIN_PREFIX_ALNUM and any(
            other.startswith(word) for other in symbol_words
        ):
            covered += 1
    return covered


def _fts_column_weights(conn: sqlite3.Connection) -> str:
    """Return the BM25 weight arguments matching the live nodes_fts shape.

    Read from the table itself rather than hard-coded so an index created by
    an older schema version (no ``docstring``/``name_tokens`` columns) still
    gets a well-formed ``bm25()`` call instead of a wrong-arity error.
    """
    columns = [row[1] for row in conn.execute("PRAGMA table_info(nodes_fts)")]
    weights = [
        _FTS_COLUMN_WEIGHTS.get(str(column), _FTS_DEFAULT_WEIGHT)
        for column in columns
    ]
    return ", ".join(f"{weight:.1f}" for weight in weights)


def _fts_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
) -> list[tuple[int, float]]:
    """Run an FTS5 BM25 search against the nodes_fts table.

    Tries each expression from :func:`build_fts_queries` in turn and returns
    the first non-empty result, so the precise reading of a query wins and
    the widened one is only a fallback.

    Returns list of ``(node_id, bm25_score)`` tuples. The BM25 score is
    negated so higher = better (FTS5 returns negative BM25).
    """
    expressions = build_fts_queries(query)
    if not expressions:
        return []

    try:
        weights = _fts_column_weights(conn)
    except sqlite3.OperationalError as exc:
        logger.warning("FTS5 index unavailable: %s", exc)
        return []

    # The weights are floats formatted from a module constant, never from the
    # query, so there is no user-controlled text in this statement. The MATCH
    # expression itself stays a bound parameter.
    sql = (  # nosec B608
        f"SELECT rowid, bm25(nodes_fts, {weights}) AS score FROM nodes_fts "
        "WHERE nodes_fts MATCH ? ORDER BY score LIMIT ?"
    )
    for expression in expressions:
        try:
            rows = conn.execute(sql, (expression, limit)).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("FTS5 search failed for %r: %s", expression, e)
            return []
        if rows:
            # bm25() is negative (lower = better); negate for consistency.
            return [(row[0], -row[1]) for row in rows]
    return []


# ---------------------------------------------------------------------------
# Embedding search (optional)
# ---------------------------------------------------------------------------


def _embedding_search(
    store: GraphStore,
    query: str,
    limit: int = 50,
    model: str | None = None,
    provider: str | None = None,
) -> list[tuple[int, float]]:
    """Run a vector similarity search using the embedding store.

    Returns list of ``(node_id, similarity_score)`` tuples.
    Gracefully returns an empty list if embeddings are not available.
    """
    try:
        from .embeddings import EmbeddingStore
    except ImportError:
        return []

    try:
        emb_store = EmbeddingStore(store.db_path, provider=provider, model=model)
        try:
            if not emb_store.available or emb_store.count() == 0:
                return []

            results = emb_store.search(query, limit=limit)
            # Map qualified names back to node IDs
            id_scores: list[tuple[int, float]] = []
            for qn, score in results:
                node = store.get_node(qn)
                if node:
                    id_scores.append((node.id, score))
            return id_scores
        finally:
            emb_store.close()
    except Exception as e:
        logger.warning("Embedding search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Keyword LIKE fallback
# ---------------------------------------------------------------------------


def _keyword_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
) -> list[tuple[int, float]]:
    """Fall back to simple LIKE keyword matching.

    Each word in the query must match independently (AND logic).
    Returns ``(node_id, score)`` tuples with a basic relevance score.
    """
    words = query.lower().split()
    if not words:
        return []

    conditions: list[str] = []
    params: list[str | int] = []
    for word in words:
        conditions.append(
            "(LOWER(name) LIKE ? OR LOWER(qualified_name) LIKE ?)"
        )
        params.extend([f"%{word}%", f"%{word}%"])

    where = " AND ".join(conditions)
    params.append(limit)
    sql = f"SELECT id, name, qualified_name FROM nodes WHERE {where} LIMIT ?"  # nosec B608

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []

    # Assign a simple relevance score: exact name match > prefix > contains
    q_lower = query.lower()
    results: list[tuple[int, float]] = []
    for row in rows:
        name_lower = row["name"].lower()
        if name_lower == q_lower:
            score = 3.0
        elif name_lower.startswith(q_lower):
            score = 2.0
        else:
            score = 1.0
        results.append((row["id"], score))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Main hybrid search
# ---------------------------------------------------------------------------


def hybrid_search(
    store: GraphStore,
    query: str,
    kind: Optional[str] = None,
    limit: int = 20,
    context_files: Optional[list[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    _out_mode: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Hybrid search combining FTS5 BM25 and vector embeddings via RRF.

    Attempts FTS5 + embedding search first, falling back to FTS5-only,
    then keyword LIKE matching if FTS5 is unavailable.

    Args:
        store: The graph store to search.
        query: Search query string.
        kind: Optional node kind filter (e.g. ``"Function"``, ``"Class"``).
        limit: Maximum results to return (default 20).
        context_files: Optional list of file paths. Nodes in these files
            receive a 1.5x score boost.
        _out_mode: Optional output list. If provided, a single string is
            appended indicating which search path(s) contributed:
            ``"hybrid"`` (FTS + embeddings), ``"fts"`` (FTS only),
            ``"semantic"`` (embeddings only), ``"keyword"`` (LIKE fallback),
            or ``"none"`` (empty query, or all search paths returned 0 results).

    Returns:
        List of dicts with node metadata and ``score`` field.
    """
    if not query or not query.strip():
        if _out_mode is not None:
            _out_mode.append("none")
        return []

    # NOTE: hybrid_search uses store._conn for FTS5 and keyword queries
    # because those operate on the FTS virtual table or need raw Row
    # access for batch-fetch performance.  This is documented coupling.
    conn = store._conn
    fetch_limit = limit * 3  # Fetch extra to allow for filtering and boosting

    # ------ Phase 1: Gather ranked lists ------
    fts_results: list[tuple[int, float]] = []
    emb_results: list[tuple[int, float]] = []

    # Try FTS5 search
    try:
        fts_results = _fts_search(conn, query, limit=fetch_limit)
    except Exception as e:
        logger.warning("FTS5 unavailable, will use fallback: %s", e)

    # Try embedding search
    emb_results = _embedding_search(
        store, query, limit=fetch_limit, model=model, provider=provider,
    )

    # ------ Phase 2: Merge via RRF or fallback ------
    if fts_results or emb_results:
        lists_to_merge = []
        if fts_results:
            lists_to_merge.append(fts_results)
        if emb_results:
            lists_to_merge.append(emb_results)
        merged = rrf_merge(*lists_to_merge)
        if _out_mode is not None:
            if fts_results and emb_results:
                _out_mode.append("hybrid")
            elif fts_results:
                _out_mode.append("fts")
            else:
                _out_mode.append("semantic")
    else:
        # Fallback: keyword LIKE matching
        keyword_results = _keyword_search(conn, query, limit=fetch_limit)
        if not keyword_results:
            if _out_mode is not None:
                _out_mode.append("none")
            return []
        if _out_mode is not None:
            _out_mode.append("keyword")
        merged = keyword_results

    # ------ Phase 3+4: Batch-fetch nodes, apply boosting and kind filter ------
    kind_boosts = detect_query_kind_boost(query)
    # Stored file paths use POSIX separators (#774); bridge native spellings.
    context_set = (
        {normalize_file_path(p) for p in context_files} if context_files else set()
    )

    query_words = identifier_words(query)

    # Batch-fetch all candidate nodes in one query
    candidate_ids = [node_id for node_id, _ in merged]
    node_rows: dict[int, Any] = {}
    batch_size = 450
    for i in range(0, len(candidate_ids), batch_size):
        batch = candidate_ids[i:i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE id IN ({placeholders})",  # nosec B608
            batch,
        ).fetchall()
        for row in rows:
            node_rows[row["id"]] = row

    # Apply boosting
    boosted: list[tuple[int, float]] = []
    for node_id, score in merged:
        row = node_rows.get(node_id)
        if not row:
            continue

        node_kind = row["kind"]
        file_path = row["file_path"]
        qualified_name = row["qualified_name"]

        boost = 1.0
        if node_kind in kind_boosts:
            boost *= kind_boosts[node_kind]
        if "_qualified" in kind_boosts and '.' in query:
            if query.lower() in qualified_name.lower():
                boost *= kind_boosts["_qualified"]
        idents = kind_boosts.get("_qualified_identifiers")
        if idents:
            qn_lo = qualified_name.lower()
            if any(ident in qn_lo for ident in idents):
                boost *= 2.0
        if query_words:
            symbol = row["symbol"] if "symbol" in row.keys() else None
            symbol_words = identifier_words(symbol or row["name"])
            covered = _covered_word_count(query_words, symbol_words)
            if covered:
                boost *= 1.0 + _NAME_COVERAGE_BOOST * covered / len(query_words)
                if symbol_words == query_words:
                    boost *= _NAME_EXACT_COVERAGE_BOOST

        exact = kind_boosts.get("_exact_name")
        if exact:
            if row["name"].lower() == exact:
                boost *= _EXACT_NAME_BOOST
            elif qualified_name.lower().endswith("::" + exact):
                boost *= _EXACT_SYMBOL_BOOST
        if context_set and file_path in context_set:
            boost *= 1.5

        boosted.append((node_id, score * boost))

    boosted.sort(key=lambda x: x[1], reverse=True)

    # Build results from the already-fetched rows
    results: list[dict[str, Any]] = []
    for node_id, final_score in boosted:
        if len(results) >= limit:
            break

        row = node_rows.get(node_id)
        if not row:
            continue

        node_kind = row["kind"]
        if kind and node_kind != kind:
            continue

        results.append({
            "name": _sanitize_name(row["name"]),
            "qualified_name": _sanitize_name(row["qualified_name"]),
            "kind": node_kind,
            "file_path": row["file_path"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "language": row["language"] or "",
            "params": row["params"],
            "return_type": row["return_type"],
            "signature": row["signature"] if "signature" in row.keys() else None,
            "score": round(final_score, 6),
        })

    return results
