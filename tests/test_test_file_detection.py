"""Test-file detection and its effect on the untested-code report.

Test helpers, fixtures and test classes are test code. The parser must mark
every node it emits from a test file with ``is_test``, and ``analyze_changes``
must keep test-file nodes out of ``test_gaps`` even when the stored row says
``is_test = 0`` (a graph built before that parser fix).

Originating PR: #1014, whose review comment listed
``tests/test_upgrade_path.py::_check``, ``_base_env``, ``_venv_python`` and
``_venv_script`` as test gaps.
"""

import tempfile
from pathlib import Path

import pytest

from code_review_graph.changes import analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, NodeInfo, is_test_file


# ---------------------------------------------------------------------------
# is_test_file: per-language conventions
# ---------------------------------------------------------------------------

TEST_PATHS = [
    # Python
    "/repo/tests/helpers.py",
    "/repo/tests/unit/support.py",
    "/repo/test_upgrade_path.py",
    "/repo/pkg/test_thing.py",
    "/repo/pkg/thing_test.py",
    "/repo/conftest.py",
    "/repo/tests/conftest.py",
    # JavaScript / TypeScript
    "/repo/src/button.test.ts",
    "/repo/src/button.test.tsx",
    "/repo/src/button.spec.ts",
    "/repo/src/button.spec.js",
    "/repo/src/__tests__/button.ts",
    # Go
    "/repo/pkg/server_test.go",
    # Java / Kotlin
    "/repo/src/test/java/com/acme/Thing.java",
    "/repo/src/test/kotlin/com/acme/Thing.kt",
    "/repo/src/main/java/com/acme/ThingTest.java",
    "/repo/src/main/java/com/acme/ThingTests.java",
    "/repo/src/main/kotlin/com/acme/ThingTest.kt",
    # Ruby
    "/repo/spec/models/user_spec.rb",
    "/repo/lib/user_spec.rb",
    "/repo/lib/user_test.rb",
    # C#
    "/repo/src/OrderTests.cs",
    "/repo/src/OrderTest.cs",
]

PRODUCTION_PATHS = [
    "/repo/code_review_graph/changes.py",
    "/repo/src/button.ts",
    "/repo/src/latest/index.ts",
    "/repo/pkg/server.go",
    "/repo/src/main/java/com/acme/Thing.java",
    "/repo/lib/user.rb",
    "/repo/src/Order.cs",
    # "contest" is not "test": the directory pattern must respect path
    # boundaries rather than matching anywhere in the string.
    "/repo/contests/runner.py",
    "/repo/src/greatest.py",
]


@pytest.mark.parametrize("path", TEST_PATHS)
def test_is_test_file_recognises_test_conventions(path):
    assert is_test_file(path) is True
    assert is_test_file(path.replace("/", "\\")) is True


@pytest.mark.parametrize("path", PRODUCTION_PATHS)
def test_is_test_file_rejects_production_paths(path):
    assert is_test_file(path) is False
    assert is_test_file(path.replace("/", "\\")) is False


# ---------------------------------------------------------------------------
# Parser: every node emitted from a test file is test code
# ---------------------------------------------------------------------------

_PY_TEST_SOURCE = '''\
import pytest


@pytest.fixture
def corpus_repo():
    return 1


def _check(value):
    """Private helper, not a test."""
    return value


class TestUpgradePath:
    def test_one(self):
        assert _check(1) == 1

    def helper(self):
        return 2
'''


@pytest.fixture
def parsed_python_test_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tests" / "test_upgrade_path.py"
        path.parent.mkdir(parents=True)
        path.write_text(_PY_TEST_SOURCE, encoding="utf-8")
        nodes, _edges = CodeParser().parse_file(path)
        yield {n.name: n for n in nodes}


def test_class_in_test_file_is_marked_as_test(parsed_python_test_file):
    """Class nodes from a test file carry is_test (the #1014 regression)."""
    assert parsed_python_test_file["TestUpgradePath"].is_test is True


def test_private_helper_in_test_file_is_marked_as_test(parsed_python_test_file):
    assert parsed_python_test_file["_check"].is_test is True


def test_fixture_in_test_file_is_marked_as_test(parsed_python_test_file):
    assert parsed_python_test_file["corpus_repo"].is_test is True


def test_non_test_method_in_test_class_is_marked_as_test(parsed_python_test_file):
    assert parsed_python_test_file["helper"].is_test is True


def test_every_node_from_a_test_file_is_marked(parsed_python_test_file):
    unmarked = [n.name for n in parsed_python_test_file.values() if not n.is_test]
    assert unmarked == []


def test_production_file_nodes_are_not_marked_as_test():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "app.py"
        path.write_text(
            "class Service:\n"
            "    def handle(self):\n"
            "        return 1\n"
            "\n"
            "def _private():\n"
            "    return 2\n",
            encoding="utf-8",
        )
        nodes, _ = CodeParser().parse_file(path)
    by_name = {n.name: n for n in nodes}
    assert by_name["Service"].is_test is False
    assert by_name["handle"].is_test is False
    assert by_name["_private"].is_test is False


def test_typescript_spec_file_class_is_marked_as_test():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "button.spec.ts"
        path.write_text(
            "export class Fixture {\n"
            "  build(): number { return 1; }\n"
            "}\n",
            encoding="utf-8",
        )
        nodes, _ = CodeParser().parse_file(path)
    by_name = {n.name: n for n in nodes}
    assert by_name["Fixture"].is_test is True


# ---------------------------------------------------------------------------
# analyze_changes: test-file nodes never reach test_gaps
# ---------------------------------------------------------------------------


class TestGapsExcludeTestFiles:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _add(self, kind, name, path, is_test=False, line_start=1, line_end=10):
        self.store.upsert_node(
            NodeInfo(
                kind=kind,
                name=name,
                file_path=path,
                line_start=line_start,
                line_end=line_end,
                language="python",
                is_test=is_test,
            ),
            file_hash="h",
        )
        self.store.commit()

    def test_stale_graph_test_file_rows_are_not_reported(self):
        """A graph built before the parser fix stores is_test = 0 on test
        classes and helpers. The report must still exclude them."""
        self._add("Class", "TestUpgradePath", "tests/test_upgrade_path.py",
                  line_start=1, line_end=20)
        self._add("Function", "_check", "tests/test_upgrade_path.py",
                  line_start=21, line_end=30)
        self._add("Function", "_base_env", "tests/test_upgrade_path.py",
                  line_start=31, line_end=40)
        self._add("Function", "_venv_python", "tests/test_upgrade_path.py",
                  line_start=41, line_end=50)

        result = analyze_changes(
            self.store,
            changed_files=["tests/test_upgrade_path.py"],
            changed_ranges={"tests/test_upgrade_path.py": [(1, 50)]},
        )
        assert result["test_gaps"] == []

    def test_production_gaps_are_still_reported(self):
        self._add("Function", "analyze_changes", "code_review_graph/changes.py",
                  line_start=1, line_end=10)
        self._add("Class", "GraphStore", "code_review_graph/graph.py",
                  line_start=1, line_end=10)

        result = analyze_changes(
            self.store,
            changed_files=[
                "code_review_graph/changes.py",
                "code_review_graph/graph.py",
            ],
            changed_ranges={
                "code_review_graph/changes.py": [(1, 10)],
                "code_review_graph/graph.py": [(1, 10)],
            },
        )
        names = {g["name"] for g in result["test_gaps"]}
        assert names == {"analyze_changes", "GraphStore"}

    def test_mixed_diff_keeps_production_and_drops_test_files(self):
        self._add("Function", "compute_risk_score",
                  "code_review_graph/changes.py", line_start=1, line_end=10)
        self._add("Class", "TestChanges", "tests/test_changes.py",
                  line_start=1, line_end=10)
        self._add("Function", "_add_func", "tests/test_changes.py",
                  line_start=11, line_end=20)

        result = analyze_changes(
            self.store,
            changed_files=[
                "code_review_graph/changes.py",
                "tests/test_changes.py",
            ],
            changed_ranges={
                "code_review_graph/changes.py": [(1, 10)],
                "tests/test_changes.py": [(1, 20)],
            },
        )
        names = {g["name"] for g in result["test_gaps"]}
        assert names == {"compute_risk_score"}


# ---------------------------------------------------------------------------
# review guidance: same rule, same stale-graph guard
# ---------------------------------------------------------------------------


def _graph_node(node_id, kind, name, qualified_name, file_path, is_test=False):
    from code_review_graph.graph import GraphNode

    return GraphNode(
        id=node_id,
        kind=kind,
        name=name,
        qualified_name=qualified_name,
        file_path=file_path,
        line_start=1,
        line_end=10,
        language="python",
        parent_name=None,
        params=None,
        return_type=None,
        is_test=is_test,
        file_hash=None,
        extra={},
    )


def test_review_guidance_ignores_test_file_helpers():
    """Review guidance must not ask for tests for the tests (#1014)."""
    from code_review_graph.tools.review import _generate_review_guidance

    impact = {
        "changed_nodes": [
            _graph_node(1, "Function", "_check",
                        "tests/test_upgrade_path.py::_check",
                        "tests/test_upgrade_path.py"),
            _graph_node(2, "Function", "analyze_changes",
                        "code_review_graph/changes.py::analyze_changes",
                        "code_review_graph/changes.py"),
        ],
        "edges": [],
        "impacted_nodes": [],
        "impacted_files": [],
    }
    guidance = _generate_review_guidance(impact, ["tests/test_upgrade_path.py"])
    assert "_check" not in guidance
    assert "analyze_changes" in guidance
    assert "1 changed function(s) lack test coverage" in guidance
