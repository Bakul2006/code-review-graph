"""Shared constants for code-review-graph."""

from __future__ import annotations

import math
import os
from pathlib import Path


def _bounded_float_env(
    name: str,
    default: float,
    *,
    lower: float,
    upper: float,
) -> float:
    """Read a finite float strictly inside ``(lower, upper)``.

    Invalid environment configuration falls back to the documented default
    instead of making graph traversal unbounded or failing during import.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or not lower < value < upper:
        return default
    return value

SECURITY_KEYWORDS: frozenset[str] = frozenset({
    "auth", "login", "password", "token", "session", "crypt", "secret",
    "credential", "permission", "sql", "query", "execute", "connect",
    "socket", "request", "http", "sanitize", "validate", "encrypt",
    "decrypt", "hash", "sign", "verify", "admin", "privilege",
})

# ---------------------------------------------------------------------------
# Configurable limits (override via environment variables)
# ---------------------------------------------------------------------------
MAX_IMPACT_NODES = int(os.environ.get("CRG_MAX_IMPACT_NODES", "500"))
MAX_IMPACT_DEPTH = int(os.environ.get("CRG_MAX_IMPACT_DEPTH", "2"))
MAX_BFS_DEPTH = int(os.environ.get("CRG_MAX_BFS_DEPTH", "15"))
MAX_SEARCH_RESULTS = int(os.environ.get("CRG_MAX_SEARCH_RESULTS", "20"))

# Impact traversal engine: "sql" (bounded SQLite relaxation) or "networkx".
BFS_ENGINE = os.environ.get("CRG_BFS_ENGINE", "sql")

# ---------------------------------------------------------------------------
# Impact-radius scoring
# ---------------------------------------------------------------------------
# Each hop multiplies the best score so strongly coupled nodes rank first.
# These review-risk weights intentionally differ from community-clustering
# affinity weights.
IMPACT_EDGE_WEIGHTS: dict[str, float] = {
    "CALLS": 1.0,
    "INHERITS": 0.9,
    "OVERRIDES": 0.9,
    "IMPLEMENTS": 0.9,
    "TESTED_BY": 0.7,
    "REFERENCES": 0.6,
    "DEPENDS_ON": 0.6,
    "IMPORTS_FROM": 0.5,
    "CONTAINS": 0.3,
}
IMPACT_DEFAULT_EDGE_WEIGHT = 0.5

# Stored dependency edges point from the dependent to its dependency, so impact
# normally propagates against the stored edge (target -> source). TESTED_BY is
# intentionally stored in the opposite orientation (production -> test).
# CONTAINS is not traversed: changing a file already seeds every node in it, and
# following containment can bridge into unrelated structure through stale edges.
IMPACT_DIRECTION_INCOMING = "incoming"
IMPACT_DIRECTION_OUTGOING = "outgoing"
IMPACT_DIRECTION_NONE = "none"
IMPACT_EDGE_DIRECTIONS: dict[str, str] = {
    "CALLS": IMPACT_DIRECTION_INCOMING,
    "INHERITS": IMPACT_DIRECTION_INCOMING,
    "OVERRIDES": IMPACT_DIRECTION_INCOMING,
    "IMPLEMENTS": IMPACT_DIRECTION_INCOMING,
    "TESTED_BY": IMPACT_DIRECTION_OUTGOING,
    "REFERENCES": IMPACT_DIRECTION_INCOMING,
    "DEPENDS_ON": IMPACT_DIRECTION_INCOMING,
    "IMPORTS_FROM": IMPACT_DIRECTION_INCOMING,
    "CONTAINS": IMPACT_DIRECTION_NONE,
}
# Unknown relationships conservatively follow the dominant graph convention:
# source depends on target. This includes possible dependents without claiming
# that a changed node's own unclassified dependency is impacted.
IMPACT_DEFAULT_EDGE_DIRECTION = IMPACT_DIRECTION_INCOMING

IMPACT_DEPTH_DECAY = _bounded_float_env(
    "CRG_IMPACT_DEPTH_DECAY", 0.6, lower=0.0, upper=1.0,
)
IMPACT_SCORE_FLOOR = _bounded_float_env(
    "CRG_IMPACT_SCORE_FLOOR", 0.05, lower=0.0, upper=1.0,
)


#: Overrides the per-user state directory that holds ``registry.json``,
#: ``watch.toml``, ``daemon.pid``, ``daemon-state.json`` and ``logs/``.
#: Follows the same convention as CRG_DATA_DIR.
CRG_HOME_ENV = "CRG_HOME"

_DEFAULT_CRG_HOME = Path.home() / ".code-review-graph"


def crg_home() -> Path:
    """Return the per-user state directory for code-review-graph.

    ``$CRG_HOME`` wins when set and non-empty; otherwise
    ``~/.code-review-graph``.

    Resolved per call rather than captured in a module-level constant. An
    import-time constant cannot be redirected afterwards, which is what let
    the test suite write into the real home directory of whoever ran it: by
    the time a fixture set the variable, the value had already been frozen.
    """
    override = os.environ.get(CRG_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _DEFAULT_CRG_HOME


# ---------------------------------------------------------------------------
# Directory-scoped import targets
# ---------------------------------------------------------------------------

#: ``edges.extra`` key that marks an ``IMPORTS_FROM`` target as a DIRECTORY
#: rather than a file. Two import forms name a directory: a Go import names a
#: package, and Ruby's ``require_all`` names a tree. Fanning either one out to
#: one edge per member file makes the edge count grow with imports times
#: package size -- 73,507 of kubernetes' import edges came from a single such
#: fan-out -- and makes an incremental update disagree with a rebuild, because
#: the edge's target set then depends on which files were in the package when
#: the importing file happened to be parsed. One edge names the directory and
#: the read path expands it; see ``expand_import_scope`` in graph.py.
IMPORT_SCOPE_KEY = "import_scope"

#: The target directory's own files are the imported unit; subdirectories are
#: separate packages and are NOT members. This is Go's rule.
IMPORT_SCOPE_PACKAGE = "package"

#: Every file below the target directory is a member, at any depth. This is
#: what the ``require_all`` gem loads.
IMPORT_SCOPE_TREE = "tree"

IMPORT_SCOPES = (IMPORT_SCOPE_PACKAGE, IMPORT_SCOPE_TREE)
