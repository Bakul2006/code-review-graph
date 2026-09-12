"""Focused regressions for the August PR-sweep follow-ups (#866)."""

from pathlib import Path
from unittest.mock import MagicMock

from code_review_graph.parser import CodeParser
from code_review_graph.tools.review import get_affected_flows_func


def test_affected_flows_empty_result_includes_truncated(monkeypatch, tmp_path):
    store = MagicMock()
    monkeypatch.setattr(
        "code_review_graph.tools.review._get_store",
        lambda _root: (store, tmp_path),
    )

    result = get_affected_flows_func(changed_files=[], repo_root=str(tmp_path))

    assert result["truncated"] is False


def test_js_specifier_resolves_jsx_source(tmp_path: Path) -> None:
    caller = tmp_path / "app.mts"
    caller.write_text('import "./foo.js"\n', encoding="utf-8")
    (tmp_path / "foo.jsx").write_text("export {}\n", encoding="utf-8")

    resolved = CodeParser()._resolve_module_to_file("./foo.js", str(caller), "typescript")

    assert resolved == (tmp_path / "foo.jsx").as_posix()


def test_mjs_specifier_resolves_mts_source(tmp_path: Path) -> None:
    caller = tmp_path / "app.mts"
    caller.write_text('import "./foo.mjs"\n', encoding="utf-8")
    (tmp_path / "foo.mts").write_text("export {}\n", encoding="utf-8")

    resolved = CodeParser()._resolve_module_to_file("./foo.mjs", str(caller), "typescript")

    assert resolved == (tmp_path / "foo.mts").as_posix()


def test_cjs_specifier_resolves_cts_source(tmp_path: Path) -> None:
    caller = tmp_path / "app.cts"
    caller.write_text('import "./foo.cjs"\n', encoding="utf-8")
    (tmp_path / "foo.cts").write_text("export {}\n", encoding="utf-8")

    resolved = CodeParser()._resolve_module_to_file("./foo.cjs", str(caller), "typescript")

    assert resolved == (tmp_path / "foo.cts").as_posix()
