"""Python relative imports must resolve to the module FILE, not to a symbol.

``from .graph import GraphStore`` imports from the module ``.graph``. The
module half of a relative import lives in tree-sitter-python's
``relative_import`` node, reachable as the ``module_name`` field; the
``dotted_name`` children that follow ``import`` are the imported SYMBOLS.
Reading the first ``dotted_name`` therefore hands back ``GraphStore`` and
calls it a module, which a walk up the filesystem then "resolves" to
whatever ``GraphStore.py`` it meets first -- on a case-insensitive
filesystem, possibly a real but wrong file.

Every test here pins the exact ``IMPORTS_FROM`` target and asserts that the
target is a file that exists on disk.
"""

from pathlib import Path

import pytest

from code_review_graph.parser import CodeParser
from tests.import_audit import exists_case_exact, looks_like_a_path

# --------------------------------------------------------------------------
# Fixture package
#
#   pkg/__init__.py
#   pkg/graph.py            node_to_dict(), GraphStore
#   pkg/helpers.py
#   pkg/registry.py         lowercase on disk; imported as `Registry`
#   pkg/consumer.py         level-1 importer
#   pkg/sub/__init__.py
#   pkg/sub/deep.py         thing()
#   pkg/sub/consumer.py     level-2 importer
# --------------------------------------------------------------------------


@pytest.fixture
def pkg(tmp_path: Path) -> Path:
    """Build a small two-level package and return the repository root."""
    root = tmp_path / "repo"
    package = root / "pkg"
    sub = package / "sub"
    sub.mkdir(parents=True)

    (package / "__init__.py").write_text(
        "from .graph import GraphStore\n", encoding="utf-8",
    )
    (package / "graph.py").write_text(
        "class GraphStore:\n    pass\n\n\ndef node_to_dict(node):\n    return {}\n",
        encoding="utf-8",
    )
    (package / "helpers.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8",
    )
    (package / "registry.py").write_text(
        "class Registry:\n    pass\n", encoding="utf-8",
    )
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "deep.py").write_text(
        "def thing():\n    return 2\n", encoding="utf-8",
    )
    return root


def _parse(pkg_root: Path, relative: str, source: str):
    """Write *source* at *relative* inside *pkg_root* and parse it."""
    target = pkg_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    parser = CodeParser(repo_root=pkg_root)
    return parser.parse_file(target)


def _import_targets(edges) -> list[str]:
    return sorted(e.target for e in edges if e.kind == "IMPORTS_FROM")


def _call_targets(edges) -> set[str]:
    return {e.target for e in edges if e.kind == "CALLS"}


def _posix(path: Path) -> str:
    return path.resolve().as_posix()


# --------------------------------------------------------------------------
# from .m import <symbols>
# --------------------------------------------------------------------------


def test_single_symbol_targets_the_module_file(pkg):
    _, edges = _parse(pkg, "pkg/consumer.py", "from .graph import GraphStore\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "graph.py")]


def test_several_symbols_still_emit_one_edge(pkg):
    """One statement imports from one module, however many names it binds."""
    _, edges = _parse(
        pkg, "pkg/consumer.py", "from .graph import GraphStore, node_to_dict\n",
    )

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "graph.py")]


def test_aliased_symbol_emits_an_edge(pkg):
    """``import a as b`` wraps the name in ``aliased_import`` -- still an import."""
    _, edges = _parse(
        pkg, "pkg/consumer.py", "from .graph import GraphStore as GS\n",
    )

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "graph.py")]


def test_star_import_emits_an_edge(pkg):
    _, edges = _parse(pkg, "pkg/consumer.py", "from .graph import *\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "graph.py")]


def test_symbol_is_never_mistaken_for_a_module_on_a_case_insensitive_fs(pkg):
    """``Registry`` is a class; ``Registry.py`` does not exist anywhere."""
    _, edges = _parse(pkg, "pkg/consumer.py", "from .registry import Registry\n")

    targets = _import_targets(edges)
    assert targets == [_posix(pkg / "pkg" / "registry.py")]
    assert not any(Path(t).name == "Registry.py" for t in targets)


def test_sibling_module_never_shadows_the_named_module(pkg):
    """``from .cli import main`` must not land on the sibling ``main.py``."""
    (pkg / "pkg" / "cli.py").write_text(
        "def main():\n    return 0\n", encoding="utf-8",
    )
    (pkg / "pkg" / "main.py").write_text("X = 1\n", encoding="utf-8")

    _, edges = _parse(pkg, "pkg/entry.py", "from .cli import main\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "cli.py")]


# --------------------------------------------------------------------------
# from . import <submodules>  /  from .pkg import <submodule>
# --------------------------------------------------------------------------


def test_from_dot_import_submodule_targets_package_and_submodule(pkg):
    """``.`` is the caller's package; ``graph`` is a module in it."""
    _, edges = _parse(pkg, "pkg/consumer.py", "from . import graph\n")

    assert _import_targets(edges) == [
        _posix(pkg / "pkg" / "__init__.py"),
        _posix(pkg / "pkg" / "graph.py"),
    ]


def test_from_dot_import_several_submodules_emits_one_edge_per_symbol(pkg):
    _, edges = _parse(pkg, "pkg/consumer.py", "from . import graph, helpers\n")

    assert _import_targets(edges) == [
        _posix(pkg / "pkg" / "__init__.py"),
        _posix(pkg / "pkg" / "graph.py"),
        _posix(pkg / "pkg" / "helpers.py"),
    ]


def test_from_subpackage_import_submodule(pkg):
    _, edges = _parse(pkg, "pkg/consumer.py", "from .sub import deep\n")

    assert _import_targets(edges) == [
        _posix(pkg / "pkg" / "sub" / "__init__.py"),
        _posix(pkg / "pkg" / "sub" / "deep.py"),
    ]


def test_from_subpackage_import_plain_symbol_targets_init(pkg):
    """A name that is not a submodule leaves only the package ``__init__``."""
    _, edges = _parse(pkg, "pkg/consumer.py", "from .sub import NOT_A_MODULE\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "sub" / "__init__.py")]


def test_dotted_relative_module(pkg):
    _, edges = _parse(pkg, "pkg/consumer.py", "from .sub.deep import thing\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "sub" / "deep.py")]


# --------------------------------------------------------------------------
# Leading-dot level
# --------------------------------------------------------------------------


def test_level_two_walks_one_package_up(pkg):
    _, edges = _parse(pkg, "pkg/sub/consumer.py", "from ..graph import GraphStore\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "graph.py")]


def test_level_two_with_a_dotted_tail(pkg):
    _, edges = _parse(pkg, "pkg/sub/consumer.py", "from ..sub.deep import thing\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "sub" / "deep.py")]


def test_level_is_not_silently_dropped(pkg):
    """``..graph`` and ``.graph`` name different modules from the same file."""
    (pkg / "pkg" / "sub" / "graph.py").write_text("Y = 1\n", encoding="utf-8")

    _, level_one = _parse(pkg, "pkg/sub/consumer.py", "from .graph import Y\n")
    _, level_two = _parse(
        pkg, "pkg/sub/consumer2.py", "from ..graph import GraphStore\n",
    )

    assert _import_targets(level_one) == [_posix(pkg / "pkg" / "sub" / "graph.py")]
    assert _import_targets(level_two) == [_posix(pkg / "pkg" / "graph.py")]


# --------------------------------------------------------------------------
# Repository-root clamp
# --------------------------------------------------------------------------


def test_level_above_the_repository_root_emits_no_path(pkg, tmp_path):
    """A target outside the repository root must never be emitted."""
    outside = tmp_path / "escape.py"
    outside.write_text("Z = 1\n", encoding="utf-8")

    _, edges = _parse(pkg, "pkg/consumer.py", "from ...escape import Z\n")

    targets = _import_targets(edges)
    assert _posix(outside) not in targets
    for target in targets:
        assert "/" not in target, f"leaked a path outside the repo root: {target}"


def test_unresolvable_relative_import_never_invents_a_path(pkg):
    _, edges = _parse(pkg, "pkg/consumer.py", "from .nope import Thing\n")

    for target in _import_targets(edges):
        assert "/" not in target
        assert not Path(target).is_absolute()


def test_every_path_shaped_target_exists_on_disk(pkg):
    source = (
        "from .graph import GraphStore, node_to_dict\n"
        "from .graph import GraphStore as GS\n"
        "from .graph import *\n"
        "from . import graph, helpers\n"
        "from .sub import deep\n"
        "from .sub.deep import thing\n"
        "from .registry import Registry\n"
        "from .nope import Missing\n"
        "import os\n"
        "from pathlib import Path\n"
    )
    _, edges = _parse(pkg, "pkg/consumer.py", source)

    for target in _import_targets(edges):
        if looks_like_a_path(target):
            # Case-exact: ``Path.is_file()`` would say yes to ``Registry.py``
            # on APFS, which is exactly how this bug stayed invisible here.
            assert exists_case_exact(target), f"{target} does not exist"


# --------------------------------------------------------------------------
# Absolute imports must not regress
# --------------------------------------------------------------------------


def test_absolute_stdlib_imports_unchanged(pkg):
    _, edges = _parse(
        pkg, "pkg/consumer.py", "import os\nfrom pathlib import Path\n",
    )

    assert _import_targets(edges) == ["os", "pathlib"]


def test_absolute_in_repo_import_resolves_to_the_module_file(pkg):
    _, edges = _parse(pkg, "entry.py", "from pkg.graph import GraphStore\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "graph.py")]


def test_absolute_dotted_module_import_unchanged(pkg):
    _, edges = _parse(
        pkg, "pkg/consumer.py", "import a.b\nimport b as B\nimport x, y as z\n",
    )

    assert _import_targets(edges) == ["a.b", "b", "x", "y"]


# --------------------------------------------------------------------------
# Cross-module CALLS (the import_map half of the same bug)
# --------------------------------------------------------------------------


def test_relative_import_resolves_cross_module_calls(pkg):
    _, edges = _parse(
        pkg,
        "pkg/consumer.py",
        "from .graph import node_to_dict\n\n\ndef run(n):\n    return node_to_dict(n)\n",
    )

    expected = f"{_posix(pkg / 'pkg' / 'graph.py')}::node_to_dict"
    assert expected in _call_targets(edges)


def test_level_two_relative_import_resolves_cross_module_calls(pkg):
    _, edges = _parse(
        pkg,
        "pkg/sub/consumer.py",
        "from ..graph import node_to_dict\n\n\ndef run(n):\n    return node_to_dict(n)\n",
    )

    expected = f"{_posix(pkg / 'pkg' / 'graph.py')}::node_to_dict"
    assert expected in _call_targets(edges)


def test_aliased_relative_import_resolves_cross_module_calls(pkg):
    _, edges = _parse(
        pkg,
        "pkg/consumer.py",
        "from .graph import node_to_dict as n2d\n\n\ndef run(n):\n    return n2d(n)\n",
    )

    graph_file = _posix(pkg / "pkg" / "graph.py")
    assert any(
        target.startswith(f"{graph_file}::") for target in _call_targets(edges)
    ), _call_targets(edges)


def test_star_relative_import_still_resolves_cross_module_calls(pkg):
    _, edges = _parse(
        pkg,
        "pkg/consumer.py",
        "from .graph import *\n\n\ndef run(n):\n    return node_to_dict(n)\n",
    )

    expected = f"{_posix(pkg / 'pkg' / 'graph.py')}::node_to_dict"
    assert expected in _call_targets(edges)


def test_absolute_import_calls_unchanged(pkg):
    _, edges = _parse(
        pkg,
        "entry.py",
        "from pkg.graph import node_to_dict\n\n\ndef run(n):\n"
        "    return node_to_dict(n)\n",
    )

    expected = f"{_posix(pkg / 'pkg' / 'graph.py')}::node_to_dict"
    assert expected in _call_targets(edges)


# --------------------------------------------------------------------------
# Other languages share _extract_import / _collect_file_scope / _do_resolve_module
# --------------------------------------------------------------------------


def test_typescript_relative_imports_unchanged(tmp_path):
    root = tmp_path / "ts"
    (root / "src").mkdir(parents=True)
    dep = root / "src" / "dep.ts"
    dep.write_text("export function helper() { return 1; }\n", encoding="utf-8")
    caller = root / "src" / "main.ts"
    caller.write_text(
        "import { helper } from './dep';\nexport function run() { return helper(); }\n",
        encoding="utf-8",
    )

    parser = CodeParser(repo_root=root)
    _, edges = parser.parse_file(caller)

    assert _import_targets(edges) == [_posix(dep)]
    assert f"{_posix(dep)}::helper" in _call_targets(edges)


def test_javascript_relative_imports_unchanged(tmp_path):
    root = tmp_path / "js"
    root.mkdir()
    dep = root / "dep.js"
    dep.write_text("export function helper() { return 1; }\n", encoding="utf-8")
    caller = root / "main.js"
    caller.write_text("import { helper } from './dep';\n", encoding="utf-8")

    parser = CodeParser(repo_root=root)
    _, edges = parser.parse_file(caller)

    assert _import_targets(edges) == [_posix(dep)]


def test_java_imports_unchanged(tmp_path):
    root = tmp_path / "java"
    pkg_dir = root / "com" / "ex"
    pkg_dir.mkdir(parents=True)
    helper = pkg_dir / "Helper.java"
    helper.write_text(
        "package com.ex;\npublic class Helper {}\n", encoding="utf-8",
    )
    caller = root / "Main.java"
    caller.write_text(
        "import com.ex.Helper;\npublic class Main {}\n", encoding="utf-8",
    )

    parser = CodeParser(repo_root=root)
    _, edges = parser.parse_file(caller)

    assert _import_targets(edges) == [_posix(helper)]


def test_kotlin_imports_unchanged(tmp_path):
    root = tmp_path / "kt"
    pkg_dir = root / "app"
    pkg_dir.mkdir(parents=True)
    helper = pkg_dir / "Helper.kt"
    helper.write_text("package app\nclass Helper\n", encoding="utf-8")
    caller = root / "Main.kt"
    caller.write_text("import app.Helper\nfun main() {}\n", encoding="utf-8")

    parser = CodeParser(repo_root=root)
    _, edges = parser.parse_file(caller)

    assert _import_targets(edges) == [_posix(helper)]


def test_go_imports_unchanged(tmp_path):
    root = tmp_path / "go"
    root.mkdir()
    caller = root / "main.go"
    caller.write_text(
        'package main\n\nimport (\n\t"fmt"\n\t"example.com/m/other"\n)\n',
        encoding="utf-8",
    )

    parser = CodeParser(repo_root=root)
    _, edges = parser.parse_file(caller)

    assert _import_targets(edges) == ["example.com/m/other", "fmt"]


def test_rust_imports_unchanged(tmp_path):
    root = tmp_path / "rs"
    src = root / "src"
    src.mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8",
    )
    helper = src / "helper.rs"
    helper.write_text("pub fn thing() {}\n", encoding="utf-8")
    caller = src / "main.rs"
    caller.write_text(
        "mod helper;\nuse crate::helper::thing;\n\nfn main() { thing(); }\n",
        encoding="utf-8",
    )

    parser = CodeParser(repo_root=root)
    _, edges = parser.parse_file(caller)

    assert _posix(helper) in _import_targets(edges)
