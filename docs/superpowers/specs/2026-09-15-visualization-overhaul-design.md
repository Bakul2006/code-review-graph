# Visualization overhaul — design

Date: 2026-09-15. Status: draft for maintainer review. Branch target: `staging`.

## 1. Goal

Make `code-review-graph visualize` useful on real repositories (10k–100k+ nodes)
without giving up the property that made it adoptable: one offline HTML file,
no server, no account. Four phases, each its own PR series into `staging`:

1. **Maintenance** — fix confirmed bugs, make the size caps real, document what exists.
2. **Sigma.js renderer** — WebGL rendering with worker layout; raises the full-mode
   ceiling from 3k to ~30k nodes; D3 canvas fallback when WebGL is unavailable.
3. **UX pack** — focus+context, path highlight, hierarchical collapse, heat overlays,
   deep links, minimap. Renderer-independent.
4. **Served lazy mode** — `visualize --serve` becomes a read-only local API so the page
   loads only what is viewed; the same renderer bundle backs the VS Code webview.

Non-goals: a hosted service, multi-repo rendering (tracked in the multi-repo spec),
GPU-only renderers as the default, editing the graph from the UI.

Sequencing: starts after PR #988 merges (it touches `cli.py`, `tools/`, and the schema).

## 2. Current state (what the code does today)

- `code_review_graph/visualization.py` (2355 lines) dumps the whole graph with
  `export_graph_data(store)` and splices it into one of two inline templates:
  `_HTML_TEMPLATE` (full D3 v7 SVG force layout) and `_AGGREGATED_HTML_TEMPLATE`
  (one super-node per community or file, drill-down).
- `generate_html(store, output_path, mode="auto", max_full_nodes=3000,
  max_full_edges=9000)`; `_resolve_auto_mode` picks full only under both caps
  (`DEFAULT_MAX_FULL_NODES`, `DEFAULT_MAX_FULL_EDGES`, lines 94–95).
- `--mode full` and community drill-down bypass the caps and render everything as
  SVG DOM, which is the #609 stall at ~10k nodes.
- D3 is vendored at `code_review_graph/assets/d3.v7.min.js`, copied next to the HTML
  by `_write_d3_asset`, SRI-pinned, with a `document.write` CDN fallback (#475).
- `visualize --serve` (cli.py:2046) serves the **whole data directory** on a fixed
  port 8765 with `SimpleHTTPRequestHandler`, which exposes `graph.db` and, with a
  shared `--data-dir`, other repositories' files.
- Exporters in `exports.py`: `export_json`, `export_graphml`, `export_neo4j_cypher`,
  `export_obsidian_vault`, `export_svg`. GraphML uses a wrong namespace; Cypher emits
  Python `True`/`False`; none of the four non-JSON exporters has a test.
- Confirmed UI bugs from reading the JS: edges to hidden communities are not hidden;
  label display is not reset when the community filter clears; the legend is not
  restored on every Escape path; `moveTooltip` reads `pageX` from a `FocusEvent`.
- Tests (`tests/test_visualization.py`) are string-presence and `node --check`
  syntax assertions; nothing runs in a browser.
- The VS Code extension has a second D3 SVG renderer (`src/views/graphWebview.ts`,
  `src/webview/graph.ts`) reading `graph.db` with a naive first-N node slice.
- Demand: zero open visualization issues. Every reported breakage is fixed on main.

## 3. Phase 1 — Maintenance (small)

**Bugs.** Fix the four UI bugs above in `_HTML_TEMPLATE`. Fix GraphML namespace to
`http://graphml.graphdrawing.org/xmlns` with the standard `schemaLocation`. Make
`_cypher_props` emit `true`/`false` for booleans. Precompute a file → community map
in `_aggregate_file` (currently a nested scan), build `community_details` in one
pass, and use a set for Obsidian slug collisions.

**Caps become real.** `--mode full` honours `max_full_nodes`/`max_full_edges`.
Exceeding them requires an explicit `--max-nodes N --max-edges M` and prints the
counts. Community drill-down renders at most `max_full_nodes` members and shows
"N of M rendered". New `--include GLOB` / `--exclude GLOB` filters (repeatable,
matched against the file path) so the existing ">50k nodes, consider filtering"
warning finally points at something that exists.

**Payload.** Drop fields the page never reads from the embedded JSON (edge `id`,
`file_path`, `line`, `confidence_tier`, `ambiguous`/`unresolved` lists; node
`is_test`, `parent_name`; node `id` when there are no flows). `community_details`
references node indices instead of duplicating every node.

**`--serve` hardening.** Add `--port` (default 8765) and `--open`. Serve from a
temporary directory that contains only `graph.html` and the vendored JS; never the
data directory.

**Docs.** `docs/COMMANDS.md`, `docs/USAGE.md`, `docs/FEATURES.md`: document
`--mode auto|full|community|file`, the caps, `--serve`, every `--format`, the
vendored-D3 offline behaviour. Fix the stale "starts collapsed" sentence.

**Tests.** Exporter tests for graphml/cypher/obsidian/svg (parse the output);
`--mode` dispatch and cap-override tests; a `--serve` handler test proving only the
allowed files are served.

## 4. Phase 2 — Sigma.js renderer (medium)

**Assets.** Vendor `sigma.min.js` (3.x), `graphology.umd.min.js`, and
`graphology-library.min.js` under `code_review_graph/assets/` with SHA-384 pins,
following the `_write_d3_asset` pattern generalised to `_write_viz_assets()`.
CDN fallback tags carry the same integrity hashes (cdnjs for sigma and graphology,
jsdelivr for graphology-library). The `__D3_SCRIPTS__`-before-`__GRAPH_DATA__`
substitution order guard extends to a single `__VIZ_SCRIPTS__` placeholder.
Uncompressed cost is about 420 KB next to the HTML; `--inline-assets` embeds
everything for a true single file when someone needs to email it.

**Template split.** `_HTML_TEMPLATE` becomes a model layer (graph build, collapse
state, search index, filters, keyboard handling) plus a renderer interface:
`mount(container)`, `update(nodes, edges)`, `focus(id)`, `fit()`,
`setColorBy(key)`, `destroy()`. Two renderers: `SigmaRenderer` (WebGL) and
`D3CanvasRenderer` (canvas with `simulation.find` hit-testing; replaces the SVG
path in full mode). The aggregated template stays on D3 until Phase 3 folds it in.

**Layout.** ForceAtlas2 from graphology in a worker created from an inline Blob so it
works from `file://`. Seed positions with `circlepack` grouped by `community_id` so
communities cluster from the first frame. Stop on settle or after a bounded number
of iterations; a "re-layout" button restarts.

**Selection.** `--renderer auto|sigma|d3` (env `CRG_VIZ_RENDERER` for headless).
Auto picks sigma when WebGL is available, else D3 canvas. Caps become
renderer-dependent: sigma 30000/90000, D3 canvas 8000/24000, aggregation above.

**Interaction.** Sigma `nodeReducer`/`edgeReducer` implement hover, search, flow and
community dimming. Labels shown by degree threshold and zoom level.

**Security.** SRI on every script tag; no `eval`; `</` escaping unchanged. A CSP
`<meta>` (`script-src 'self'` plus the inline bootstrap hash) is emitted only in
`--inline-assets` mode because the `document.write` CDN fallback is incompatible
with it.

**Tests.** Asset hash tests; syntax checks for both renderers; and a browser test
job. Decision: Playwright, as an optional pytest marker `browser` and a separate CI
job `viz-browser` that skips when Playwright is not installed. The tests load a
generated page, assert rendered node counts, and exercise collapse, search, edge
toggles and the renderer fallback.

## 5. Phase 3 — UX pack (medium, one small PR per item)

a. **Focus+context.** Select a node → local graph with a depth slider (1–3);
   double-click expands neighbours. Obsidian-style.
b. **Path highlight.** Two-symbol picker; BFS over CALLS/IMPORTS_FROM/INHERITS in
   the page; highlights the path and lists the hops.
c. **Hierarchical collapse.** Directory and community super-nodes as first-class
   objects with a breadcrumb; replaces the "auto-collapse above 300 nodes" heuristic.
d. **Heat overlays.** "Color by: kind | community | churn | risk | criticality".
   `export_graph_data` gains an optional `heat` payload; `visualize --heat
   churn,risk,criticality` is opt-in because churn needs `git log`
   (`compute_file_churn`), risk uses `compute_risk_score`, criticality comes from
   flows.
e. **Deep links.** URL hash `#node=<qualified>&depth=2&mode=full&color=risk` so MCP
   tool output and PR comments can link into the graph.
f. **Minimap** canvas, `+`/`-` zoom, help overlay in both templates.
g. **Aggregated-mode parity.** Edge toggles, kind shapes, keyboard navigation, and a
   "rendered N of M" counter in the stats bar.

## 6. Phase 4 — Served lazy mode (large)

`visualize --serve` becomes an API-backed page for repositories past the full-mode
caps. Read-only endpoints, all bounded and parameterised, names sanitised via
`_sanitize_name`:

- `GET /api/summary` — counts, communities, top-degree nodes
- `GET /api/neighbors?qn=&depth=&kinds=`
- `GET /api/paths?from=&to=&max=`
- `GET /api/search?q=&limit=`
- `GET /api/node?qn=`

Bound to 127.0.0.1 only; Host/Origin checked with the existing
`LoopbackOriginGuard` (`http_origin_guard.py`). The page opens on the aggregated
view and fetches neighbourhoods on expand; the path tool uses `/api/paths`.

**One renderer.** The model and renderer JS move to
`code_review_graph/assets/viz-core.js`; the HTML template references it and the VS
Code extension bundles the same file through esbuild, replacing its private D3 copy.
Its `maxNodes` slice becomes "top N by degree" and edge fetches are batched.

Follow-up, not in this phase: an opt-in `--renderer cosmos` GPU overview.

## 7. Testing and CI

- Existing string and syntax tests stay.
- Phase 1 adds exporter, dispatch, cap and `--serve` tests.
- Phase 2 adds the optional Playwright job; it must be green before `--renderer auto`
  defaults to sigma.
- Phase 4 adds endpoint tests against a built fixture graph, including bound
  enforcement and origin rejection.
- Schema is untouched; no migration. `action.yml` cache key unaffected.

## 8. Rollout

Phase 1 → 2 → 3 (items in parallel) → 4. Each phase lands behind a flag where it
changes default behaviour, and `CHANGELOG.md` `[Unreleased]` grows per PR. Docs
for each phase ship in the same PR.

## 9. Decisions recorded

- Keep one offline HTML as the default deliverable. Server mode is additive.
- WebGL via Sigma with automatic D3 canvas fallback; never WebGL-only.
- Layout is computed in the browser worker, not persisted in `graph.db`.
- Dark theme stays the standalone default; `prefers-color-scheme` support is not
  planned.
- Playwright is acceptable as an optional CI dependency.
