"""CI guard: a path-shaped IMPORTS_FROM target must name a file that exists.

An import edge target is either a bare module name the resolver could not
place (``os``, ``example.com/m/other`` has a slash but no repository file --
see below) or a claim about the filesystem. When it is a claim, it has to be
true, and true *case-exactly*: ``Path.is_file()`` answers "yes" to
``.../Registry.py`` on APFS when only ``registry.py`` exists, which is how
``from .registry import Registry`` resolving to the class name rather than
the module shipped unnoticed from a macOS workstation.

The guard builds a real graph over a repository laid out with the
import forms that used to break, then asserts:

* every path-shaped target exists case-exactly, and
* no target escapes the repository root.

It is meaningful on Linux (where the wrong spelling simply does not exist)
and on macOS/Windows (where ``exists_case_exact`` compares against the real
directory listing rather than asking the filesystem to match).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import _run_python_resolver, full_build
from tests.import_audit import (
    audit_imports,
    exists_case_exact,
    looks_like_a_path,
    missing_path_targets,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def built_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A tiny git repository exercising every Python import form, built."""
    repo = tmp_path / "repo"
    package = repo / "app"
    sub = package / "sub"
    sub.mkdir(parents=True)

    (package / "__init__.py").write_text(
        "from .registry import Registry\n", encoding="utf-8",
    )
    # Lowercase on disk, class name capitalised: the exact shape that a
    # case-insensitive filesystem used to paper over.
    (package / "registry.py").write_text(
        "class Registry:\n    pass\n", encoding="utf-8",
    )
    (package / "graph.py").write_text(
        "def node_to_dict(node):\n    return {}\n", encoding="utf-8",
    )
    (package / "cli.py").write_text(
        "def main():\n    return 0\n", encoding="utf-8",
    )
    # A sibling whose name collides with a symbol imported from cli.py.
    (package / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "deep.py").write_text("def thing():\n    return 1\n", encoding="utf-8")

    (package / "consumer.py").write_text(
        "from . import graph\n"
        "from .registry import Registry\n"
        "from .cli import main\n"
        "from .graph import node_to_dict as n2d\n"
        "from .sub import deep\n"
        "from .sub.deep import thing\n"
        "from .graph import *\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def run(node):\n"
        "    return n2d(node), thing(), main(), Registry(), os, Path, graph, deep\n",
        encoding="utf-8",
    )
    (sub / "consumer.py").write_text(
        "from ..graph import node_to_dict\n"
        "from ..sub.deep import thing\n"
        "\n"
        "\n"
        "def run(node):\n"
        "    return node_to_dict(node), thing()\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    db_path = repo / ".code-review-graph" / "graph.db"
    store = GraphStore(db_path)
    try:
        full_build(repo, store)
        _run_python_resolver(store)
    finally:
        store.close()
    return repo, db_path


def test_no_import_target_claims_a_file_that_does_not_exist(built_repo):
    repo, db_path = built_repo
    store = GraphStore(db_path)
    try:
        offenders = missing_path_targets(store._conn)
    finally:
        store.close()

    assert offenders == [], (
        "IMPORTS_FROM targets that look like paths but name no existing file "
        f"(case-exact): {offenders}"
    )


def test_no_import_target_escapes_the_repository_root(built_repo):
    repo, db_path = built_repo
    store = GraphStore(db_path)
    try:
        rows = store._conn.execute(
            "SELECT DISTINCT target_qualified FROM edges WHERE kind = 'IMPORTS_FROM'"
        ).fetchall()
    finally:
        store.close()

    root = repo.resolve()
    for row in rows:
        target = row["target_qualified"]
        if not looks_like_a_path(target):
            continue
        path = Path(target.split("::", 1)[0])
        assert path.is_relative_to(root), f"{target} is outside {root}"


def test_every_relative_import_resolves_to_the_right_file(built_repo):
    repo, db_path = built_repo
    store = GraphStore(db_path)
    try:
        stats = audit_imports(store._conn, repo / "app", repo.resolve())
    finally:
        store.close()

    assert stats["relative_total"] >= 9
    assert stats["relative_wrong"] == 0, stats["wrong_examples"]
    assert stats["relative_missing"] == 0
    assert stats["relative_correct"] == stats["relative_total"]


def test_exists_case_exact_rejects_a_wrong_spelling(tmp_path):
    """The guard's own teeth: this is what fails on a case-insensitive FS."""
    real = tmp_path / "registry.py"
    real.write_text("x = 1\n", encoding="utf-8")

    assert exists_case_exact(real.as_posix())
    assert not exists_case_exact((tmp_path / "Registry.py").as_posix())
    assert not exists_case_exact((tmp_path / "nope.py").as_posix())
    assert not exists_case_exact("registry.py")


def test_looks_like_a_path_only_flags_resolved_targets():
    assert looks_like_a_path("/repo/app/graph.py")
    assert looks_like_a_path("/repo/app/graph.py::node_to_dict")
    assert looks_like_a_path("C:/repo/app/graph.py")
    # Unresolved module specifiers copied out of the source: not a claim
    # about this filesystem, and not this guard's business.
    assert not looks_like_a_path("os")
    assert not looks_like_a_path("pkg.module")
    assert not looks_like_a_path(".relative")
    assert not looks_like_a_path("./dep")
    assert not looks_like_a_path("vars/common.yml")
    assert not looks_like_a_path("package:flutter/material.dart")
    assert not looks_like_a_path("")


def test_this_repository_has_no_missing_import_targets():
    """Run the guard over code-review-graph itself when a fresh graph exists.

    Opportunistic: the database is gitignored, so CI normally skips this and
    relies on ``built_repo`` above. A stale database is skipped rather than
    failed -- it describes a tree that no longer exists, so its targets say
    nothing about the current parser.
    """
    db_path = REPO_ROOT / ".code-review-graph" / "graph.db"
    if not db_path.is_file():
        pytest.skip("no graph built for this repository")
    built_at = db_path.stat().st_mtime
    newest_source = max(
        (path.stat().st_mtime for path in (REPO_ROOT / "code_review_graph").rglob("*.py")),
        default=0.0,
    )
    if newest_source > built_at:
        pytest.skip("graph is older than the sources it describes")
    store = GraphStore(db_path)
    try:
        offenders = [
            offender
            for offender in missing_path_targets(store._conn)
            if Path(offender["target"].split("::", 1)[0]).is_relative_to(REPO_ROOT)
        ]
    finally:
        store.close()

    assert offenders == [], offenders
