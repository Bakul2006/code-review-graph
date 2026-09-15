# Multi-repo organisation references — design

Date: 2026-09-15. Status: draft for maintainer review. Branch target: `staging`.

## 1. Goal

Let a repository reference symbols that live in sibling repositories of the same
organisation, so that `import x from "../../ui/src/button"` or
`from org_lib import thing` in repo A produces an edge that queries can follow into
repo B's graph, and so that cross-repo tools can be scoped to a named group.

Constraints chosen by the maintainer:

- **Group lives on registry entries.** No second config file.
- **Tag at build, resolve at query.** Cross-repo edges are ordinary edges whose
  `extra` JSON names the target repository. No schema migration.
- **Cross only into same-group siblings.** Resolvers clamp to the repo root unless
  the target is inside a registered sibling of the same group.
- Build on PR #880 (`npm_alias_resolver.py`) for npm package-name resolution.

Non-goals: a shared server-side graph (#931), a shared base graph across worktrees
(#464), per-call `data_dir` (#950, declined in #951), rendering several repos in one
visualization.

Sequencing: after PR #988 merges (it rewrites `tools/registry_tools.py` with
`_select_repos` and the `repos` filter from #915, moves the schema to v10 and
changes the pre-commit hook). Then review and merge #880 into `staging` first.

## 2. Current state

- `~/.code-review-graph/registry.json` holds `{"repos": [{path, alias?, data_dir?}]}`
  (`registry.py`: `Registry.register/unregister/list_repos/find_by_alias/
  find_by_path/set_data_dir/get_data_dir_for_repo`). `ConnectionPool` and
  `resolve_repo` exist but nothing outside tests calls them. `Registry.register`
  does not enforce alias uniqueness; `daemon.add_repo_to_config` does.
- Two tools cross repos, both search-only: `list_repos_tool` and
  `cross_repo_search_tool` (rank-interleave per repo). Every other tool takes one
  `repo_root`, resolved by `main._resolve_repo_root` → `tools/_common._resolve_root`
  → `_validate_repo_root`; aliases are not accepted.
- Node identity is `<absolute file path>::<symbol>`; each `graph.db` is isolated.
- JS/TS relative imports, `TsconfigResolver` path aliases (walks up to find
  `tsconfig.json`), and the Python/Java ancestor walks already resolve outside the
  repo root and emit `IMPORTS_FROM` edges whose target has no node anywhere. Rust and
  PHP resolvers are bounded to the root.
- `edges` already has `extra TEXT` (graph.py:101) and `GraphEdge.extra` is a dict
  serialised on insert; `confidence`/`confidence_tier` are derived from it.
- Daemon: `watch.toml` entries are auto-registered on start.

## 3. Registry

**Entry fields.** Existing `path`, `alias`, `data_dir` plus:

- `group: str | None` — organisation name, free text, case-sensitive.
- `packages: list[str]` — package names this repository publishes. Auto-detected at
  register time from `package.json` (`name`, plus each `workspaces` member's name),
  `pyproject.toml` (`[project].name`), `setup.cfg` (`metadata.name`), `go.mod`
  (`module`), `Cargo.toml` (`[package].name`), `composer.json` (`name`). Detection
  reads manifests as text/TOML/JSON only; never executes `setup.py`. Users can add
  `--package NAME` and the union is stored.

The file gains `"version": 2`. Version-1 files (no `version`) load unchanged with the
new fields defaulting to `None`/`[]`; the next save writes version 2.

**API.**

```
Registry.register(path, alias=None, data_dir=None, group=None, packages=None)
    # idempotent for an already-registered path: updates alias/group/packages
    # when given, returns the entry with "updated": True; raises ValueError when
    # alias is already used by a different path
Registry.list_repos(group=None)
Registry.find_repo_containing(abs_path)   # longest registered path prefix
Registry.find_by_package(name, group=None)
Registry.siblings_of(path)                # same group, excluding self
```

**CLI.** `register <path> [--alias A] [--group G] [--package P ...]`,
`repos [--group G]`. `daemon` config entries accept `group` and pass it through.

**Alias uniqueness** becomes an error in `Registry.register`, matching the daemon.
This is a behaviour change for registry files that already contain duplicates; the
loader logs a warning naming both entries and keeps the first.

## 4. Build-time tagging

At build and update time (`incremental.py` build path) the parser receives
`sibling_roots: dict[str, str]` (resolved root → alias) and
`sibling_packages: dict[str, tuple[str, str]]` (package name → (alias, root)) built
from `Registry.siblings_of(repo_root)`. Both are empty when the repository is not
registered or has no group, so unregistered users see no change.

**Boundary rule**, applied in every resolver that can leave the root (JS/TS relative
imports, `TsconfigResolver.resolve_alias` and its tsconfig walk-up,
`_resolve_python_module_in_repo`'s ancestor walk, the Java walk, and #880's
`npm_alias_resolver`):

1. Resolve as today.
2. If the resolved path is under the repo root: unchanged.
3. Else if it is under a sibling root: keep the edge. Target becomes
   `<sibling root as registered, resolved>/<relative path>::<symbol>`, which is the
   qualified name the sibling's own build produces. `edge.extra` gains
   `{"cross_repo": true, "target_repo": "<alias>", "target_root": "<root>"}`.
4. Else: drop the edge and count it under `clamped_out_of_root` in the build summary.

**Bare package specifiers** (`@org/ui`, `org_lib`, `github.com/org/lib`) that do not
resolve in-repo (including #880's workspace lookup) are looked up in
`sibling_packages`; a hit resolves to the sibling package's entry point (`main`/
`exports` from `package.json`, the package directory for Python, the module root for
Go) and is tagged the same way.

No placeholder nodes are inserted for cross-repo targets; tools already tolerate
edge targets without a local node (bare-name targets do this today).

## 5. Query-time resolution

**Sibling graphs.** `ConnectionPool` (registry.py:228) becomes the basis of a
`SiblingGraphs` helper: opens each sibling's `graph.db` read-only using its entry's
`data_dir` (via `get_data_dir_for_repo`) or default location, LRU-bounded,
`check_same_thread=False`, closed on server shutdown. A missing or foreign-root
sibling database yields a caveat, never an exception.

**`query_graph_tool`** gains `cross_repo: bool = False`.

- Forward patterns (`callees_of`, `imports_of`, `dependencies_of`): edges with
  `extra.cross_repo` are followed into the sibling database to fetch the target node
  (kind, file, line). Each such result carries `repo: "<alias>"`.
- Reverse patterns (`callers_of`, `importers_of`, `dependents_of`): each sibling
  database is queried for edges whose `target_qualified` is a node of this repo and
  whose `extra` names this repo's alias:
  `SELECT ... FROM edges WHERE target_qualified = ? AND json_extract(extra,
  '$.target_repo') = ?`. The existing target index keeps this cheap.

**`get_impact_radius_tool`** gains `cross_repo: bool = False`; the boundary is
crossed at most once per path (depth budget shared), and the response adds
`cross_repo_summary: {repos: [...], nodes_by_repo: {...}}`.

**`cross_repo_search_tool`** and **`list_repos_tool`** gain `group: str | None`,
composed with #915's `repos` list inside `_select_repos`.

**`repo_root` accepts a registry alias** on every tool: `_resolve_root` tries
`Registry.find_by_alias` before treating the value as a path, then applies
`_validate_repo_root` to the result as today.

**Honesty.** Cross-repo is off by default. When it is on and a sibling graph is
stale (its `git_head_sha` metadata differs from the sibling's current HEAD) or
absent, the response carries a `caveats` entry naming the alias. Lists stay bounded
like every other response (#888, #895). The empty-result `confidence` sentence
mentions "cross-repo resolution is off" when the local repo has a group and the
query found tagged edges it did not follow.

## 6. Security

- Sibling roots and packages come only from the registry file under `CRG_HOME`,
  which the user controls. Every sibling root passes `_validate_repo_root`.
- Resolved targets must lie under a registered root after `Path.resolve()`; symlink
  escapes are clamped.
- Sibling connections are opened with `mode=ro` URIs; all SQL is parameterised; all
  returned names go through `_sanitize_name`.
- Manifest detection never executes code.

## 7. Testing

New fixture `tests/fixtures/org/` with two mini repos, `app` and `lib`:

- JS relative import `../../lib/src/button`, a tsconfig `paths` alias into `lib`,
  a bare `@org/lib` specifier, and a Python `from org_lib import helper`.

Tests (registry path redirected to `tmp_path` as existing tests do):

- registry: v1 file loads, v2 round-trips, alias uniqueness error, `group` listing,
  `find_repo_containing`, package auto-detection per manifest, idempotent re-register.
- parser: out-of-root target clamped when no group; tagged with the sibling alias
  when grouped; qualified name matches what building `lib` produces.
- end-to-end: build both repos, `query_graph_tool(cross_repo=True)` for callees_of
  and callers_of across the boundary, `get_impact_radius_tool(cross_repo=True)`
  summary, `cross_repo_search_tool(group=...)`, alias accepted as `repo_root`.
- caveats: stale sibling `git_head_sha`, missing sibling database.
- daemon/hook: `group` round-trips through `watch.toml`.

## 8. Documentation

README multi-repo section, `docs/FAQ.md` registry and monorepo guidance,
`docs/COMMANDS.md` for the new parameters, `docs/schema.md` for the `extra` keys
(`cross_repo`, `target_repo`, `target_root`), `CHANGELOG.md` `[Unreleased]`.

## 9. Rollout (each a PR into `staging`)

0. Merge #988; review and merge #880.
1. Registry v2 + CLI + daemon `group` (no behaviour change for existing users).
2. Parser boundary rule + tagging, with the two-repo fixture.
3. Query-time resolution: `SiblingGraphs`, `query_graph`, `impact_radius`,
   `cross_repo_search(group=)`, alias as `repo_root`.
4. Docs and FAQ.

## 10. Follow-ups outside this spec

- Strict multi-root `serve --http` with no cwd fallback (#311, #607).
- Shared graph backends (#931) and the worktree base-graph design (#464) need a
  maintainer policy answer on the threads.
