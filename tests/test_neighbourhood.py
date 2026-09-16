"""Tests for seeded k-hop neighbourhood views and the path-between query.

The whole-repo view stops being useful well before a real repository stops
growing: the renderer falls back to one bubble per community past 3000 nodes
or 9000 edges.  These tests pin the alternative — a payload that carries only
the k-hop neighbourhood of a seed, and nothing else.

The hop assertions here deliberately recompute the expected node set with a
breadth-first search written inside the test, over the *exported* edge list,
rather than reusing anything from ``code_review_graph.neighbourhood``.  A test
that called the implementation to compute its own expectation would pass for
any implementation.
"""

from __future__ import annotations

import json

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo

# ---------------------------------------------------------------------------
# Fixture graph
# ---------------------------------------------------------------------------
#
#   a.py::f_a --CALLS--> b.py::f_b --CALLS--> c.py::f_c --CALLS--> d.py::f_d
#                            |                                        |
#                            +--CALLS--> b.py::f_b2                   |
#                                                                     v
#                                                            e.py::f_e
#   c.py::C_child --INHERITS--> d.py::C_base
#   b.py --IMPORTS_FROM--> c.py
#   z.py::f_z                       (island: reachable from nothing)
#
# Every file CONTAINS its own symbols.  CONTAINS is structural, not a semantic
# hop: a File must not act as a shortcut that drags in every sibling symbol.


def _fn(name: str, file_path: str, kind: str = "Function") -> NodeInfo:
    return NodeInfo(
        kind=kind,
        name=name,
        file_path=file_path,
        line_start=1,
        line_end=5,
        language="python",
        parent_name=None,
        params=None,
        return_type=None,
        modifiers=None,
        is_test=kind == "Test",
        extra={},
    )


def _file(file_path: str) -> NodeInfo:
    return NodeInfo(
        kind="File",
        name=file_path.rsplit("/", 1)[-1],
        file_path=file_path,
        line_start=1,
        line_end=100,
        language="python",
        parent_name=None,
        params=None,
        return_type=None,
        modifiers=None,
        is_test=False,
        extra={},
    )


def _edge(kind: str, source: str, target: str, file_path: str) -> EdgeInfo:
    return EdgeInfo(
        kind=kind,
        source=source,
        target=target,
        file_path=file_path,
        line=1,
        extra={},
    )


SYMBOLS = {
    "src/a.py": ["f_a"],
    "src/b.py": ["f_b", "f_b2"],
    "src/c.py": ["f_c", "C_child"],
    "src/d.py": ["f_d", "C_base"],
    "src/e.py": ["f_e"],
    "src/z.py": ["f_z"],
}

CALL_EDGES = [
    ("src/a.py::f_a", "src/b.py::f_b"),
    ("src/b.py::f_b", "src/c.py::f_c"),
    ("src/b.py::f_b", "src/b.py::f_b2"),
    ("src/c.py::f_c", "src/d.py::f_d"),
    ("src/d.py::f_d", "src/e.py::f_e"),
]


@pytest.fixture
def chain_store(tmp_path) -> GraphStore:
    store = GraphStore(tmp_path / "chain.db")
    for file_path, symbols in SYMBOLS.items():
        store.upsert_node(_file(file_path))
        for sym in symbols:
            kind = "Class" if sym.startswith("C_") else "Function"
            store.upsert_node(_fn(sym, file_path, kind=kind))
            store.upsert_edge(
                _edge("CONTAINS", file_path, f"{file_path}::{sym}", file_path)
            )
    for source, target in CALL_EDGES:
        store.upsert_edge(_edge("CALLS", source, target, source.split("::")[0]))
    store.upsert_edge(
        _edge("INHERITS", "src/c.py::C_child", "src/d.py::C_base", "src/c.py")
    )
    store.upsert_edge(_edge("IMPORTS_FROM", "src/b.py", "src/c.py", "src/b.py"))
    store.commit()
    return store


# ---------------------------------------------------------------------------
# Independent expectation helpers (no implementation code reused)
# ---------------------------------------------------------------------------


def _reference_hops(edges: list[dict], seeds: list[str], depth: int) -> dict[str, int]:
    """Breadth-first hop distances over non-CONTAINS edges, undirected."""
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        if edge["kind"] == "CONTAINS":
            continue
        adjacency.setdefault(edge["source"], set()).add(edge["target"])
        adjacency.setdefault(edge["target"], set()).add(edge["source"])
    hops = {seed: 0 for seed in seeds}
    frontier = list(seeds)
    for distance in range(1, depth + 1):
        nxt: list[str] = []
        for node in frontier:
            for neighbour in adjacency.get(node, ()):
                if neighbour not in hops:
                    hops[neighbour] = distance
                    nxt.append(neighbour)
        frontier = nxt
    return hops


def _reference_parent_files(edges: list[dict], members: set[str]) -> set[str]:
    """Files that CONTAIN any member, i.e. the structural attachment set."""
    parents = set()
    for edge in edges:
        if edge["kind"] == "CONTAINS" and edge["target"] in members:
            parents.add(edge["source"])
    return parents


# ---------------------------------------------------------------------------
# Hop-exactness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_neighbourhood_contains_exactly_the_nodes_within_k_hops(chain_store, depth):
    from code_review_graph.visualization import export_graph_data

    full = export_graph_data(chain_store)
    seed = "src/a.py::f_a"

    expected_symbols = set(_reference_hops(full["edges"], [seed], depth))
    expected = expected_symbols | _reference_parent_files(
        full["edges"], expected_symbols
    )

    view = export_graph_data(chain_store, seed_symbols=[seed], depth=depth)
    actual = {node["qualified_name"] for node in view["nodes"]}

    assert actual == expected


def test_hop_labels_match_an_independent_bfs(chain_store):
    from code_review_graph.visualization import export_graph_data

    full = export_graph_data(chain_store)
    seed = "src/a.py::f_a"
    expected = _reference_hops(full["edges"], [seed], 3)

    view = export_graph_data(chain_store, seed_symbols=[seed], depth=3)
    hops = view["neighbourhood"]["hops"]

    for qualified_name, distance in expected.items():
        assert hops[qualified_name] == distance


def test_contains_edges_are_not_a_hop_shortcut(chain_store):
    """f_b2 shares a file with f_b but is 2 CALLS hops from the seed."""
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, seed_symbols=["src/a.py::f_a"], depth=1
    )
    names = {node["qualified_name"] for node in view["nodes"]}

    assert "src/b.py::f_b" in names
    assert "src/b.py" in names, "the containing file is attached for clustering"
    assert "src/b.py::f_b2" not in names, "CONTAINS must not shortcut to siblings"


def test_rest_of_the_graph_is_absent_from_the_payload(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, seed_symbols=["src/a.py::f_a"], depth=2
    )
    serialized = json.dumps(view)

    assert "f_z" not in serialized, "unreachable island must not be shipped"
    assert "f_e" not in serialized, "hop 4 must not be shipped at depth 2"
    assert len(view["nodes"]) < len(export_graph_data(chain_store)["nodes"])


def test_every_payload_edge_has_both_endpoints_in_the_payload(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, seed_symbols=["src/a.py::f_a"], depth=2
    )
    names = {node["qualified_name"] for node in view["nodes"]}
    for edge in view["edges"]:
        assert edge["source"] in names
        assert edge["target"] in names


def test_unseeded_export_is_unchanged(chain_store):
    from code_review_graph.visualization import export_graph_data

    data = export_graph_data(chain_store)

    assert "neighbourhood" not in data
    expected = len(SYMBOLS) + sum(len(v) for v in SYMBOLS.values())
    assert len(data["nodes"]) == expected  # files + symbols


# ---------------------------------------------------------------------------
# Seed resolution
# ---------------------------------------------------------------------------


def test_seed_accepts_a_bare_symbol_name(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(chain_store, seed_symbols=["f_a"], depth=1)

    assert view["neighbourhood"]["seeds"] == ["src/a.py::f_a"]


def test_unresolvable_seed_raises(chain_store):
    from code_review_graph.neighbourhood import SeedResolutionError
    from code_review_graph.visualization import export_graph_data

    with pytest.raises(SeedResolutionError) as excinfo:
        export_graph_data(chain_store, seed_symbols=["no_such_symbol"])

    assert "no_such_symbol" in str(excinfo.value)


def test_seed_from_changed_files_includes_the_files_symbols(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(chain_store, seed_files=["src/b.py"], depth=0)
    names = {node["qualified_name"] for node in view["nodes"]}

    assert names == {"src/b.py", "src/b.py::f_b", "src/b.py::f_b2"}
    assert view["neighbourhood"]["seed_kind"] == "file"


def test_seed_file_matching_tolerates_a_repo_relative_prefix(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(chain_store, seed_files=["./src/b.py"], depth=0)
    names = {node["qualified_name"] for node in view["nodes"]}

    assert "src/b.py::f_b" in names


def test_seed_from_flow(chain_store):
    from code_review_graph.flows import store_flows
    from code_review_graph.visualization import export_graph_data

    path_qns = ["src/a.py::f_a", "src/b.py::f_b", "src/c.py::f_c"]
    ids = [chain_store.get_node(qn).id for qn in path_qns]
    store_flows(
        chain_store,
        [
            {
                "name": "a to c",
                "entry_point_id": ids[0],
                "path": ids,
                "depth": 3,
                "node_count": 3,
                "file_count": 3,
                "criticality": 1.0,
            }
        ],
    )

    view = export_graph_data(chain_store, seed_flow="a to c", depth=0)
    names = {node["qualified_name"] for node in view["nodes"]}

    assert set(path_qns) <= names
    assert view["neighbourhood"]["seed_kind"] == "flow"


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_max_nodes_trims_the_outermost_hop_first(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, seed_symbols=["src/a.py::f_a"], depth=3, max_nodes=4
    )
    hops = view["neighbourhood"]["hops"]

    assert len(view["nodes"]) <= 4
    assert view["neighbourhood"]["truncated"] is True
    assert hops["src/a.py::f_a"] == 0
    assert max(hops[n["qualified_name"]] for n in view["nodes"]) < 3


# ---------------------------------------------------------------------------
# Path between two symbols
# ---------------------------------------------------------------------------


def test_path_between_two_symbols_follows_calls(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, path_from="f_a", path_to="f_e", depth=0
    )
    neighbourhood = view["neighbourhood"]

    assert neighbourhood["path"] == [
        "src/a.py::f_a",
        "src/b.py::f_b",
        "src/c.py::f_c",
        "src/d.py::f_d",
        "src/e.py::f_e",
    ]
    assert neighbourhood["path_directed"] is True
    assert neighbourhood["seed_kind"] == "path"


def test_path_can_traverse_inherits_and_imports(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, path_from="C_child", path_to="C_base", depth=0
    )

    assert view["neighbourhood"]["path"] == [
        "src/c.py::C_child",
        "src/d.py::C_base",
    ]


def test_path_ignores_contains_edges(chain_store):
    """f_b and f_b2 share a file; the only legal link is the CALLS edge."""
    from code_review_graph.neighbourhood import PATH_EDGE_KINDS
    from code_review_graph.visualization import export_graph_data

    assert "CONTAINS" not in PATH_EDGE_KINDS

    view = export_graph_data(
        chain_store, path_from="f_b2", path_to="f_c", depth=0
    )

    assert view["neighbourhood"]["path"] == [
        "src/b.py::f_b2",
        "src/b.py::f_b",
        "src/c.py::f_c",
    ]
    assert view["neighbourhood"]["path_directed"] is False


def test_path_with_no_connection_reports_it(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, path_from="f_a", path_to="f_z", depth=0
    )

    assert view["neighbourhood"]["path"] == []
    assert view["neighbourhood"]["path_error"]


def test_path_nodes_are_seeds_so_context_expands_around_them(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, path_from="f_a", path_to="f_c", depth=1
    )
    hops = view["neighbourhood"]["hops"]

    assert hops["src/b.py::f_b"] == 0
    assert hops["src/b.py::f_b2"] == 1


# ---------------------------------------------------------------------------
# generate_html wiring
# ---------------------------------------------------------------------------


def test_generate_html_neighbourhood_stays_one_file(chain_store, tmp_path):
    from code_review_graph.visualization import generate_html

    out = tmp_path / "graph.html"
    generate_html(chain_store, out, seed_symbols=["f_a"], depth=2)
    content = out.read_text(encoding="utf-8")

    assert '"neighbourhood"' in content
    assert "f_z" not in content
    assert not (tmp_path / "graph.data.js").exists()
    assert "window.__CRG_GRAPH_DATA__" not in content


def test_generate_html_sidecar_is_opt_in(chain_store, tmp_path):
    from code_review_graph.visualization import generate_html

    out = tmp_path / "graph.html"
    generate_html(chain_store, out, seed_symbols=["f_a"], depth=2, sidecar=True)
    content = out.read_text(encoding="utf-8")
    sidecar = tmp_path / "graph.data.js"

    assert sidecar.exists()
    assert '<script src="graph.data.js"></script>' in content
    assert "window.__CRG_GRAPH_DATA__" in content
    assert "src/a.py::f_a" not in content, "payload moved out of the page"
    payload = sidecar.read_text(encoding="utf-8")
    assert payload.startswith("window.__CRG_GRAPH_DATA__ =")
    assert "src/a.py::f_a" in payload


def test_neighbourhood_never_falls_back_to_bubble_aggregation(chain_store, tmp_path):
    """A seeded view is small by construction; auto must not aggregate it."""
    from code_review_graph.visualization import generate_html

    out = tmp_path / "graph.html"
    generate_html(
        chain_store,
        out,
        mode="auto",
        seed_symbols=["f_a"],
        depth=2,
        max_full_nodes=1,
        max_full_edges=1,
    )
    content = out.read_text(encoding="utf-8")

    assert "Drill down" not in content
    assert 'id="filter-panel"' in content


def test_neighbourhood_page_keeps_the_existing_interaction_surface(
    chain_store, tmp_path
):
    from code_review_graph.visualization import generate_html

    out = tmp_path / "graph.html"
    generate_html(chain_store, out, seed_symbols=["f_a"], depth=2)
    content = out.read_text(encoding="utf-8")

    for marker in (
        'id="search"',
        'id="flow-select"',
        'id="btn-community"',
        'id="detail-panel"',
        'id="help-overlay"',
        'data-edge-kind="CALLS"',
        'data-kind="Function"',
    ):
        assert marker in content, marker


def test_neighbourhood_page_has_expand_on_click(chain_store, tmp_path):
    from code_review_graph.visualization import generate_html

    out = tmp_path / "graph.html"
    generate_html(chain_store, out, seed_symbols=["f_a"], depth=2)
    content = out.read_text(encoding="utf-8")

    assert "nbExpand" in content
    assert "render_depth" in content


def test_render_depth_defaults_below_the_payload_depth(chain_store, tmp_path):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(chain_store, seed_symbols=["f_a"], depth=3)

    assert view["neighbourhood"]["render_depth"] == 1
    assert view["neighbourhood"]["depth"] == 3


def test_payload_reports_the_whole_graph_size_for_comparison(chain_store):
    from code_review_graph.visualization import export_graph_data

    full = export_graph_data(chain_store)
    view = export_graph_data(chain_store, seed_symbols=["f_a"], depth=1)

    assert view["neighbourhood"]["total_nodes"] == len(full["nodes"])
    assert view["neighbourhood"]["total_edges"] == len(full["edges"])


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_exposes_the_neighbourhood_flags():
    from code_review_graph.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "visualize",
            "--seed-symbol",
            "f_a",
            "--depth",
            "3",
            "--render-depth",
            "2",
            "--max-nodes",
            "500",
            "--sidecar",
        ]
    )

    assert args.seed_symbol == ["f_a"]
    assert args.depth == 3
    assert args.render_depth == 2
    assert args.max_nodes == 500
    assert args.sidecar is True


def test_cli_exposes_file_flow_and_path_seeds():
    from code_review_graph.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "visualize",
            "--seed-file",
            "src/a.py",
            "--seed-file",
            "src/b.py",
            "--seed-changed",
            "--seed-flow",
            "a to c",
            "--path-from",
            "f_a",
            "--path-to",
            "f_e",
        ]
    )

    assert args.seed_file == ["src/a.py", "src/b.py"]
    assert args.seed_changed is True
    assert args.seed_flow == "a to c"
    assert args.path_from == "f_a"
    assert args.path_to == "f_e"


def test_cli_path_flags_must_come_in_pairs():
    from code_review_graph.neighbourhood import NeighbourhoodSpec

    with pytest.raises(ValueError):
        NeighbourhoodSpec(path_from="f_a").validate()
    with pytest.raises(ValueError):
        NeighbourhoodSpec(path_to="f_e").validate()
    NeighbourhoodSpec(path_from="f_a", path_to="f_e").validate()


def test_negative_depth_is_rejected():
    from code_review_graph.neighbourhood import NeighbourhoodSpec

    with pytest.raises(ValueError):
        NeighbourhoodSpec(symbols=("f_a",), depth=-1).validate()
