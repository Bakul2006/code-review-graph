"""Risk-ordered allocation of the ``get_review_context`` source-line budget.

``_MAX_REVIEW_SOURCE_LINES`` used to be spent first come first served over
the changed-file list, which is the order Git happened to emit. One
alphabetically early 500-line file could take 500 of the 800 lines while the
riskiest changed function in the pull request got nothing at all.

These tests pin the replacement contract:

* the file list is ranked by the same risk score ``changes.py`` computes,
* every served file gets a floor so nothing silently drops to zero,
* no single file may take more than its capped share,
* the allocation is computed once from the full ranked list,
* ``truncated`` / ``source_truncated`` still describe what really happened,
* the 800-line total is never exceeded.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools import review as review_mod
from code_review_graph.tools.review import get_review_context

_NUMBERED_LINE = re.compile(r"^\d+: ")

# Four low-risk padded modules sort before the risky one alphabetically, and
# each is long enough to take the whole 200-line default per-file share. Under
# first-come-first-served that is 4 x 200 = 800 lines, leaving exactly zero for
# ``zzz_session_token.py``.
_PADDING_FUNCS = 30
_PADDING_BODY = 8


def _emitted_lines(snippet: str) -> int:
    """Count real source lines, ignoring the ``...`` range separators."""
    return sum(1 for line in snippet.splitlines() if _NUMBERED_LINE.match(line))


def _total_emitted(snippets: dict[str, str]) -> int:
    return sum(_emitted_lines(s) for s in snippets.values())


def _padded_module(prefix: str) -> str:
    """A long, well-tested, non-security module: low risk, many lines."""
    lines = [f'"""Padding module {prefix}."""', ""]
    for fn in range(_PADDING_FUNCS):
        lines.append(f"def {prefix}_step_{fn}(value):")
        lines.append(f'    """Step {fn}."""')
        for step in range(_PADDING_BODY):
            lines.append(f"    value = value + {step}  # step {step}")
        lines.append("    return value")
        lines.append("")
    return "\n".join(lines)


def _padding_tests(prefix: str) -> str:
    lines = [f"from {prefix} import *", ""]
    for fn in range(_PADDING_FUNCS):
        lines.append(f"def test_{prefix}_step_{fn}():")
        lines.append(f"    assert {prefix}_step_{fn}(1) is not None")
        lines.append("")
    return "\n".join(lines)


@pytest.fixture(scope="module")
def risky_repo(tmp_path_factory) -> dict[str, Any]:
    """A repo whose riskiest changed file sorts last in the diff order."""
    root = tmp_path_factory.mktemp("risk-budget-repo")
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)

    padding = [f"aaa_pad{i}" for i in range(4)]
    for prefix in padding:
        (root / f"{prefix}.py").write_text(_padded_module(prefix), encoding="utf-8")
        (root / f"test_{prefix}.py").write_text(
            _padding_tests(prefix), encoding="utf-8",
        )

    # The risky file: security-sensitive names, no tests, many cross-file
    # callers. Short enough that a fair share covers most of it.
    risky = ['"""Session token handling."""', ""]
    risky.append("def validate_session_token(token):")
    risky.append('    """Validate an auth token."""')
    for step in range(20):
        risky.append(f"    token = token + {step}  # check {step}")
    risky.append("    return token")
    risky.append("")
    risky.append("def decrypt_password_hash(secret):")
    risky.append('    """Decrypt a stored credential."""')
    for step in range(20):
        risky.append(f"    secret = secret + {step}  # round {step}")
    risky.append("    return validate_session_token(secret)")
    risky.append("")
    (root / "zzz_session_token.py").write_text(
        "\n".join(risky), encoding="utf-8",
    )

    # Callers in every padding module raise the risky file's caller count and
    # make it cross-community.
    for prefix in padding:
        path = root / f"{prefix}.py"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n\nfrom zzz_session_token import validate_session_token\n"
            + f"\n\ndef {prefix}_forward(value):\n"
            + "    return validate_session_token(value)\n",
            encoding="utf-8",
        )

    db_path = root / ".code-review-graph" / "graph.db"
    os.environ["CRG_SERIAL_PARSE"] = "1"
    with GraphStore(db_path) as store:
        full_build(root, store)

    changed = [f"{p}.py" for p in padding] + ["zzz_session_token.py"]
    return {"root": str(root), "changed": changed}


def _context(risky_repo: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    result = get_review_context(
        changed_files=list(risky_repo["changed"]),
        repo_root=risky_repo["root"],
        include_source=True,
        **kwargs,
    )
    assert result["status"] == "ok"
    return result["context"]


class TestRiskOrderedAllocation:
    def test_riskiest_file_receives_source(self, risky_repo):
        """The file that got nothing under first-come-first-served is served."""
        snippets = _context(risky_repo)["source_snippets"]
        assert "zzz_session_token.py" in snippets
        assert _emitted_lines(snippets["zzz_session_token.py"]) > 0

    def test_changed_file_list_is_risk_ranked(self, risky_repo):
        context = _context(risky_repo)
        assert context["changed_files"][0] == "zzz_session_token.py"
        risk = context["file_risk"]
        assert risk["zzz_session_token.py"] > max(
            score for name, score in risk.items()
            if name != "zzz_session_token.py"
        )

    def test_riskiest_file_is_served_whole(self, risky_repo):
        """Ranking must drive the allocation, not just the list order."""
        snippets = _context(risky_repo)["source_snippets"]
        emitted = _emitted_lines(snippets["zzz_session_token.py"])
        whole = len(
            (Path(risky_repo["root"]) / "zzz_session_token.py")
            .read_text(encoding="utf-8").splitlines()
        )
        assert emitted == whole

    def test_every_served_file_gets_at_least_the_floor(self, risky_repo):
        snippets = _context(risky_repo)["source_snippets"]
        for name, text in snippets.items():
            emitted = _emitted_lines(text)
            source_lines = len(
                (Path(risky_repo["root"]) / name)
                .read_text(encoding="utf-8").splitlines()
            )
            assert emitted >= min(
                review_mod._MIN_SOURCE_LINES_PER_FILE, source_lines,
            ), f"{name} fell below the per-file floor"

    def test_total_budget_is_never_exceeded(self, risky_repo):
        context = _context(risky_repo, max_lines_per_file=10_000)
        assert _total_emitted(context["source_snippets"]) <= (
            review_mod._MAX_REVIEW_SOURCE_LINES
        )

    def test_no_single_file_takes_more_than_its_share(self, risky_repo):
        context = _context(risky_repo, max_lines_per_file=10_000)
        cap = review_mod._MAX_REVIEW_SOURCE_LINES * (
            review_mod._MAX_SOURCE_SHARE_PER_FILE
        )
        for name, text in context["source_snippets"].items():
            assert _emitted_lines(text) <= cap + 1, (
                f"{name} starved the rest of the ranked list"
            )

    def test_source_truncated_is_honest_when_files_are_trimmed(self, risky_repo):
        context = _context(risky_repo)
        assert context["source_truncated"] is True
        assert context["truncated"] is True

    def test_source_truncated_absent_when_everything_fits(self, risky_repo):
        result = get_review_context(
            changed_files=["zzz_session_token.py"],
            repo_root=risky_repo["root"],
            include_source=True,
        )
        assert result["status"] == "ok"
        context = result["context"]
        assert context.get("source_truncated") is not True
        emitted = _emitted_lines(context["source_snippets"]["zzz_session_token.py"])
        whole = len(
            (Path(risky_repo["root"]) / "zzz_session_token.py")
            .read_text(encoding="utf-8").splitlines()
        )
        assert emitted == whole


class TestAllocationUnit:
    """The allocator is a pure function: one pass over the ranked list."""

    def test_allocates_once_in_rank_order(self):
        ranked = ["hot.py", "warm.py", "cold.py"]
        alloc = review_mod._allocate_source_lines(ranked, 800, 500)
        assert alloc["hot.py"] >= alloc["warm.py"] >= alloc["cold.py"]
        assert sum(alloc.values()) <= 800

    def test_every_file_gets_a_floor_while_the_budget_lasts(self):
        ranked = [f"f{i}.py" for i in range(10)]
        alloc = review_mod._allocate_source_lines(ranked, 800, 500)
        assert set(alloc) == set(ranked)
        assert min(alloc.values()) >= review_mod._MIN_SOURCE_LINES_PER_FILE

    def test_single_file_share_is_capped(self):
        alloc = review_mod._allocate_source_lines(["only.py", "other.py"], 800, 500)
        cap = int(800 * review_mod._MAX_SOURCE_SHARE_PER_FILE)
        assert alloc["only.py"] <= cap
        assert alloc["other.py"] >= review_mod._MIN_SOURCE_LINES_PER_FILE

    def test_more_files_than_floors_serves_the_top_of_the_ranking(self):
        ranked = [f"f{i}.py" for i in range(200)]
        alloc = review_mod._allocate_source_lines(ranked, 800, 500)
        served = [f for f in ranked if alloc.get(f, 0) > 0]
        assert len(served) == 800 // review_mod._MIN_SOURCE_LINES_PER_FILE
        assert served == ranked[:len(served)]
        assert sum(alloc.values()) <= 800

    def test_per_file_limit_is_respected(self):
        alloc = review_mod._allocate_source_lines(["a.py", "b.py"], 800, 25)
        assert max(alloc.values()) <= 25

    def test_empty_inputs_allocate_nothing(self):
        assert review_mod._allocate_source_lines([], 800, 500) == {}
        assert review_mod._allocate_source_lines(["a.py"], 0, 500) == {}
