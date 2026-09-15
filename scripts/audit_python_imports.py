#!/usr/bin/env python3
"""Audit the Python import edges in a built code-review-graph database.

Ground truth comes from CPython's own ``ast`` module, never from the graph:
every ``from ... import ...`` statement is re-resolved here with the import
semantics the interpreter uses, and the resulting file is compared against
the ``IMPORTS_FROM`` edge the parser actually wrote at that line.

Sections
--------
1. relative imports  -- statements with ``level > 0`` (``from .x import y``)
2. absolute imports  -- statements with ``level == 0`` that name a
   repository-local module (stdlib/third-party modules are reported
   separately because they legitimately have no file in the repository)
3. cross-module calls -- ``CALLS`` edges whose callee was bound by a
   relative import; resolved means the target names a file
4. dangling edges    -- share of all edges whose target matches no node
5. path-shaped targets that do not exist on disk, compared case-exactly so
   a case-insensitive filesystem (APFS, NTFS) cannot hide a wrong target

Usage
-----
    uv run --python 3.13 python scripts/audit_python_imports.py \
        --repo . --package code_review_graph \
        --calls-module code_review_graph/changes.py \
        --build-log build.log --json audit.json
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Path helpers -- shared with tests/test_import_target_integrity.py
# ---------------------------------------------------------------------------

_BUILD_LOG_COUNTER = re.compile(r"Python import resolution: (\{[^}]*\})")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def looks_like_a_path(target: str) -> bool:
    """Return whether an edge target claims a file on this filesystem.

    A *resolved* target is always absolute -- the resolver writes the file it
    found. Everything else is a module specifier copied out of the source and
    makes no claim about this machine: ``os``, ``pkg.mod``, ``.relative``,
    ``./dep`` (an unresolved JS import), ``vars/common.yml``,
    ``package:flutter/material.dart``. Only absolute targets are checkable,
    so only absolute targets are checked.
    """
    if not target:
        return False
    base = target.split("::", 1)[0].replace("\\", "/")
    return base.startswith("/") or bool(_WINDOWS_ABSOLUTE.match(base))


def exists_case_exact(path: str) -> bool:
    """Return whether *path* exists with exactly this spelling.

    ``Path.is_file()`` answers "yes" for ``Registry.py`` on a case-insensitive
    filesystem when only ``registry.py`` exists, which is how a wrong import
    target stayed invisible on macOS while being plainly wrong on Linux. Every
    component is therefore compared against the real directory listing.
    """
    candidate = Path(path.split("::", 1)[0])
    if not candidate.is_absolute():
        return False
    if not candidate.is_file():
        return False
    current = candidate
    while current.parent != current:
        try:
            entries = os.listdir(current.parent)
        except OSError:
            return False
        if current.name not in entries:
            return False
        current = current.parent
    return True


# ---------------------------------------------------------------------------
# Ground truth from ast
# ---------------------------------------------------------------------------


def _python_files(root: Path) -> list[Path]:
    skip = {
        ".git", ".venv", "venv", "node_modules", "__pycache__",
        ".code-review-graph", ".mypy_cache", ".pytest_cache", "build", "dist",
    }
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for filename in filenames:
            if filename.endswith(".py"):
                out.append(Path(dirpath) / filename)
    return sorted(out)


def _module_file(base: Path, dotted: Optional[str]) -> Optional[Path]:
    """Resolve ``base`` + a dotted tail to ``.py`` or ``__init__.py``.

    Package before module. ``FileFinder`` checks whether the name is a
    directory holding ``__init__`` *before* it tries any file loader, so with
    both ``pkg/m/__init__.py`` and ``pkg/m.py`` on disk ``import pkg.m`` binds
    the package. Verified against the interpreter:

        $ python3 -c "import pkg.m; print(pkg.m.__file__)"
        .../pkg/m/__init__.py

    The reverse order (what this helper used to do, and what the parser used
    to do) makes the audit agree with the bug instead of catching it.
    """
    target = base if not dotted else base.joinpath(*dotted.split("."))
    candidates = (
        [target / "__init__.py", target.with_suffix(".py")]
        if dotted
        else [target / "__init__.py"]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _expected_module_file(
    source_file: Path, node: ast.ImportFrom, boundary: Path,
) -> Optional[Path]:
    """The file CPython would import from, or None when it is not in the repo."""
    base = source_file.resolve().parent
    for _ in range(max(node.level - 1, 0)):
        if base == boundary:
            return None
        base = base.parent
    if not base.is_relative_to(boundary):
        return None
    if node.level == 0:
        # Absolute: look for the module under the repository root only.
        return _module_file(boundary, node.module)
    return _module_file(base, node.module)


def _relative_names(tree: ast.AST) -> set[str]:
    """Local names bound by relative imports (alias included)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name)
    return names


def _call_root_and_symbol(func: ast.expr) -> Optional[tuple[str, str]]:
    """(root name, called symbol) for ``a()``/``a.b()``/``a.b.c()``."""
    if isinstance(func, ast.Name):
        return func.id, func.id
    if isinstance(func, ast.Attribute):
        current: ast.expr = func
        while isinstance(current, ast.Attribute):
            current = current.value
        if isinstance(current, ast.Name):
            return current.id, func.attr
    return None


def _relative_call_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, symbol)`` for every call made through a relative import.

    Ground truth taken from the source, so the denominator does not move
    when the graph gets better at resolving these calls.
    """
    names = _relative_names(tree)
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parsed = _call_root_and_symbol(node.func)
        if parsed is None:
            continue
        root, symbol = parsed
        if root in names:
            sites.append((node.lineno, symbol))
    return sites


# ---------------------------------------------------------------------------
# Graph access
# ---------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _import_edges(conn: sqlite3.Connection) -> dict[tuple[str, int], list[str]]:
    edges: dict[tuple[str, int], list[str]] = defaultdict(list)
    for row in conn.execute(
        "SELECT file_path, line, target_qualified FROM edges "
        "WHERE kind = 'IMPORTS_FROM'"
    ):
        edges[(row["file_path"], row["line"])].append(row["target_qualified"])
    return edges


def _posix(path: Path) -> str:
    return path.resolve().as_posix()


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def audit_imports(
    conn: sqlite3.Connection, package_root: Path, boundary: Path,
) -> dict[str, Any]:
    edges = _import_edges(conn)
    stats = {
        "relative_total": 0,
        "relative_correct": 0,
        "relative_wrong": 0,
        "relative_missing": 0,
        "relative_unresolvable_on_disk": 0,
        "absolute_local_total": 0,
        "absolute_local_correct": 0,
        "absolute_external_total": 0,
    }
    wrong_examples: list[dict[str, Any]] = []

    for source_file in _python_files(package_root):
        try:
            tree = ast.parse(source_file.read_bytes(), filename=str(source_file))
        except (SyntaxError, ValueError):
            continue
        key_file = _posix(source_file)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            expected = _expected_module_file(source_file, node, boundary)
            targets = edges.get((key_file, node.lineno), [])
            if node.level > 0:
                stats["relative_total"] += 1
                if expected is None:
                    stats["relative_unresolvable_on_disk"] += 1
                    continue
                if _posix(expected) in targets:
                    stats["relative_correct"] += 1
                elif not targets:
                    stats["relative_missing"] += 1
                else:
                    stats["relative_wrong"] += 1
                    if len(wrong_examples) < 15:
                        wrong_examples.append({
                            "file": key_file,
                            "line": node.lineno,
                            "expected": _posix(expected),
                            "actual": targets,
                        })
            elif expected is None:
                stats["absolute_external_total"] += 1
            else:
                stats["absolute_local_total"] += 1
                if _posix(expected) in targets:
                    stats["absolute_local_correct"] += 1

    stats["relative_correct_pct"] = _pct(
        stats["relative_correct"], stats["relative_total"],
    )
    stats["wrong_examples"] = wrong_examples
    return stats


def audit_calls(
    conn: sqlite3.Connection, repo_root: Path, modules: Iterable[str],
) -> dict[str, Any]:
    total = 0
    resolved = 0
    per_module: dict[str, str] = {}
    for rel in modules:
        source_file = (repo_root / rel).resolve()
        if not source_file.is_file():
            continue
        try:
            tree = ast.parse(source_file.read_bytes(), filename=str(source_file))
        except (SyntaxError, ValueError):
            continue
        sites = _relative_call_sites(tree)
        if not sites:
            continue
        by_line: dict[int, list[str]] = defaultdict(list)
        for row in conn.execute(
            "SELECT line, target_qualified FROM edges "
            "WHERE kind = 'CALLS' AND file_path = ?",
            (_posix(source_file),),
        ):
            by_line[row["line"]].append(row["target_qualified"])

        module_total = len(sites)
        module_resolved = 0
        for line, symbol in sites:
            for target in by_line.get(line, ()):
                head, sep, bound = target.partition("::")
                if not sep or not looks_like_a_path(head):
                    continue
                if bound == symbol or bound.split(".")[-1] == symbol:
                    module_resolved += 1
                    break
        total += module_total
        resolved += module_resolved
        per_module[rel] = f"{module_resolved}/{module_total}"
    return {
        "calls_total": total,
        "calls_resolved": resolved,
        "calls_resolved_pct": _pct(resolved, total),
        "per_module": per_module,
    }


def audit_python_import_edges(conn: sqlite3.Connection) -> dict[str, Any]:
    """How many Python IMPORTS_FROM edges actually name a repository file.

    This is the number ``imports_resolved`` in the build log is a proxy for.
    The counter itself only reports what the *post-build* suffix index
    recovered, so it stays at zero once the parser resolves these at parse
    time -- once because nothing could be recovered, once because nothing is
    left to recover. This metric distinguishes the two.
    """
    total = 0
    resolved = 0
    for row in conn.execute(
        "SELECT e.target_qualified FROM edges e "
        "JOIN nodes f ON f.kind = 'File' AND f.file_path = e.file_path "
        "WHERE e.kind = 'IMPORTS_FROM' AND f.language = 'python'"
    ):
        total += 1
        if looks_like_a_path(row["target_qualified"]):
            resolved += 1
    return {
        "python_import_edges": total,
        "python_import_edges_resolved": resolved,
        "python_import_edges_resolved_pct": _pct(resolved, total),
    }


def audit_dangling(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    dangling = conn.execute(
        "SELECT COUNT(*) FROM edges e "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM nodes n WHERE n.qualified_name = e.target_qualified"
        ")"
    ).fetchone()[0]
    imports_total = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = 'IMPORTS_FROM'"
    ).fetchone()[0]
    imports_dangling = conn.execute(
        "SELECT COUNT(*) FROM edges e WHERE e.kind = 'IMPORTS_FROM' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM nodes n WHERE n.qualified_name = e.target_qualified"
        ")"
    ).fetchone()[0]
    return {
        "edges_total": total,
        "edges_dangling": dangling,
        "edges_dangling_pct": _pct(dangling, total),
        "imports_total": imports_total,
        "imports_dangling": imports_dangling,
        "imports_dangling_pct": _pct(imports_dangling, imports_total),
    }


def missing_path_targets(
    conn: sqlite3.Connection, limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """IMPORTS_FROM targets shaped like a path that no such file answers to."""
    offenders: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT DISTINCT file_path, line, target_qualified FROM edges "
        "WHERE kind = 'IMPORTS_FROM'"
    ):
        target = row["target_qualified"]
        if not looks_like_a_path(target):
            continue
        if exists_case_exact(target):
            continue
        offenders.append({
            "file": row["file_path"],
            "line": row["line"],
            "target": target,
        })
        if limit is not None and len(offenders) >= limit:
            break
    return offenders


def build_log_counters(log_path: Path) -> dict[str, Any]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    match = None
    for match in _BUILD_LOG_COUNTER.finditer(text):
        pass
    if match is None:
        return {}
    try:
        return ast.literal_eval(match.group(1))
    except (SyntaxError, ValueError):
        return {}


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_CALLS_MODULES = [
    "code_review_graph/changes.py",
    "code_review_graph/flows.py",
    "code_review_graph/hints.py",
    "code_review_graph/refactor.py",
    "code_review_graph/search.py",
    "code_review_graph/communities.py",
    "code_review_graph/wiki.py",
]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".", help="repository root")
    ap.add_argument(
        "--db", default=None,
        help="graph.db (default: <repo>/.code-review-graph/graph.db)",
    )
    ap.add_argument(
        "--package", default=None,
        help="subdirectory to audit imports in (default: the whole repo)",
    )
    ap.add_argument(
        "--calls-module", action="append", default=None, dest="calls_modules",
        help="repo-relative .py file to audit cross-module CALLS in (repeatable)",
    )
    ap.add_argument("--build-log", default=None, help="build output to read counters from")
    ap.add_argument("--json", default=None, help="write the full report here")
    ap.add_argument("--label", default="", help="label printed with the report")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    db_path = (
        Path(args.db).resolve()
        if args.db
        else repo_root / ".code-review-graph" / "graph.db"
    )
    if not db_path.is_file():
        print(f"no graph database at {db_path}", file=sys.stderr)
        return 2
    package_root = (
        (repo_root / args.package).resolve() if args.package else repo_root
    )
    calls_modules = args.calls_modules or DEFAULT_CALLS_MODULES

    conn = _connect(db_path)
    try:
        report: dict[str, Any] = {
            "label": args.label,
            "repo": repo_root.as_posix(),
            "package": package_root.as_posix(),
            "imports": audit_imports(conn, package_root, repo_root),
            "calls": audit_calls(conn, repo_root, calls_modules),
            "edges": audit_dangling(conn),
            "python_edges": audit_python_import_edges(conn),
        }
        offenders = missing_path_targets(conn)
        report["missing_path_targets"] = {
            "count": len(offenders),
            "examples": offenders[:15],
        }
    finally:
        conn.close()

    if args.build_log:
        report["build_log"] = build_log_counters(Path(args.build_log).resolve())

    imports = report["imports"]
    calls = report["calls"]
    edges = report["edges"]
    print(f"== python import audit {args.label} ==")
    print(f"repo:    {report['repo']}")
    print(f"package: {report['package']}")
    print(
        f"relative imports correct: {imports['relative_correct']}"
        f"/{imports['relative_total']} ({imports['relative_correct_pct']}%)"
        f"  wrong={imports['relative_wrong']} missing={imports['relative_missing']}"
        f" not-on-disk={imports['relative_unresolvable_on_disk']}"
    )
    print(
        f"absolute repo-local imports correct: "
        f"{imports['absolute_local_correct']}/{imports['absolute_local_total']}"
        f"  (external modules seen: {imports['absolute_external_total']})"
    )
    python_edges = report["python_edges"]
    print(
        "python IMPORTS_FROM edges naming a repository file: "
        f"{python_edges['python_import_edges_resolved']}"
        f"/{python_edges['python_import_edges']}"
        f" ({python_edges['python_import_edges_resolved_pct']}%)"
    )
    print(
        f"cross-module CALLS resolved: {calls['calls_resolved']}"
        f"/{calls['calls_total']} ({calls['calls_resolved_pct']}%)"
    )
    print(
        f"edges with no matching node: {edges['edges_dangling']}"
        f"/{edges['edges_total']} ({edges['edges_dangling_pct']}%)"
    )
    print(
        f"  of which IMPORTS_FROM: {edges['imports_dangling']}"
        f"/{edges['imports_total']} ({edges['imports_dangling_pct']}%)"
    )
    print(
        "path-shaped IMPORTS_FROM targets missing on disk: "
        f"{report['missing_path_targets']['count']}"
    )
    if report.get("build_log"):
        print(f"build log counters: {report['build_log']}")
    for example in imports["wrong_examples"][:5]:
        print(
            f"  WRONG {example['file']}:{example['line']} "
            f"expected {example['expected']} got {example['actual']}"
        )
    for example in report["missing_path_targets"]["examples"][:5]:
        print(f"  MISSING {example['file']}:{example['line']} -> {example['target']}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
