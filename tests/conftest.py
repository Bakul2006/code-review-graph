"""Shared test fixtures.

Keeps code-review-graph's own per-user state out of the developer's real
home directory. Scoped deliberately: the editor-integration installers in
``skills.py`` write to other user-level locations (``~/.codex``,
``~/.cursor``, ``~/.config/opencode``) that are outside CRG state and are
not covered here — those tests patch ``Path.home()`` themselves.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_crg_home(tmp_path_factory, monkeypatch):
    """Redirect the per-user state directory into a temporary directory.

    ``~/.code-review-graph`` holds ``registry.json``, ``watch.toml``,
    ``daemon.pid``, ``daemon-state.json`` and ``logs/``. Two paths reached
    the real one:

    * ``Registry()`` defaults there, and ``incremental.get_data_dir()``
      constructs one internally — so any test touching data-dir resolution
      both read and wrote the registry of whoever ran the suite. That put
      pytest tmp paths into a developer's home directory, and made those
      tests depend on machine state: a developer with a registered repo
      could get different results from one without.
    * ``daemon`` built its config/PID/state paths from ``Path.home()``.

    Autouse and unconditional: an opt-in fixture would silently stop
    protecting a test the day someone forgets to request it.
    """
    home = tmp_path_factory.mktemp("crg-home")
    monkeypatch.setenv("CRG_HOME", str(home))
    # The Hermes Agent installer resolves its config from ``HERMES_HOME``,
    # falling back to ``~/.hermes``. That fallback reaches the real user
    # config in any test that does not also patch ``Path.home()``, so pin
    # the variable to a temp directory instead of merely clearing it:
    # unset, a miss would be silently destructive; set, it cannot be.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path_factory.mktemp("hermes-home")))
    return home


def pytest_collection_modifyitems(config, items):
    """Keep the determinism gate out of an ordinary test run.

    ``tests/test_determinism.py`` builds one corpus roughly nine times in
    separate subprocesses; it is minutes of work and is meant to be run on
    demand, not on every pull request. Selecting the marker explicitly
    (``pytest -m determinism``) runs it; anything else skips it, visibly,
    rather than silently dropping it from collection.
    """
    selected = config.getoption("-m", default="") or ""
    if "determinism" in selected:
        return
    skip_slow = pytest.mark.skip(
        reason="slow determinism gate; run it with `pytest -m determinism`"
    )
    for item in items:
        if "determinism" in item.keywords:
            item.add_marker(skip_slow)
