from impactgraph import INFERRED, Edge, EdgeType, ImpactGraph, Node, NodeType, diff_graphs
from impactgraph.analysis import analyze_impact
from impactgraph.html_report import render_html


def test_call_edges_are_inferred_and_can_be_excluded(py_graph):
    calls = [e for e in py_graph.edges() if e.type == EdgeType.CALLS]
    assert calls and all(e.provenance == INFERRED for e in calls)
    with_inf = py_graph.impact("func:db.py::load_customers")
    without = py_graph.impact("func:db.py::load_customers", include_inferred=False)
    assert "func:api.py::customers_endpoint" in with_inf
    assert "func:api.py::customers_endpoint" not in without
    tree = py_graph.impact_tree("func:db.py::load_customers")
    assert tree["children"][0]["provenance"] == INFERRED


def test_dbt_edges_are_extracted(dbt_graph):
    assert all(e.provenance == "extracted" for e in dbt_graph.edges())
    analysis = analyze_impact(dbt_graph, ["dbt:customer"], include_inferred=False)
    assert "exposure:revenue_report" in analysis.affected
    assert analysis.to_dict()["include_inferred"] is False


def test_exports(tmp_path, dbt_graph):
    gml = tmp_path / "g.graphml"
    dbt_graph.to_graphml(gml)
    assert "<graphml" in gml.read_text(encoding="utf-8")
    dot = dbt_graph.to_dot()
    assert "digraph impactgraph" in dot and "dbt:customer" in dot
    cypher = dbt_graph.to_cypher()
    assert "MERGE (n:DbtModel {id: 'dbt:customer'})" in cypher
    assert "DEPENDS_ON" in cypher


def test_hotspots_rank_by_blast_radius(dbt_graph):
    rows = dbt_graph.hotspots(top=3)
    assert rows[0]["id"] in {"source:raw.customers", "file:models/customer.sql"}
    assert rows[0]["blast_radius"] >= rows[-1]["blast_radius"]
    assert all(r["type"] != "column" for r in rows)


def test_diff_graphs_detects_schema_drift(dbt_graph):
    new = ImpactGraph.from_dict(dbt_graph.to_dict())
    new.add_node(Node(id="column:customer.phone", type=NodeType.COLUMN, name="phone", meta={"parent": "dbt:customer"}))
    new.add_edge(Edge(src="dbt:customer", dst="column:customer.phone", type=EdgeType.CONTAINS))
    # simulate a dropped column by rebuilding without it
    old = ImpactGraph.from_dict(dbt_graph.to_dict())
    d = diff_graphs(old, new)
    assert "column:customer.phone" in d["added_columns"]
    d2 = diff_graphs(new, old)
    assert "column:customer.phone" in d2["removed_columns"]
    assert any(e["dst"] == "column:customer.phone" for e in d2["removed_edges"])


def test_html_report_is_self_contained(dbt_graph):
    analysis = analyze_impact(dbt_graph, ["dbt:customer"])
    html = render_html(dbt_graph, analysis)
    assert "<svg" in html and "exposure:revenue_report" in html
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "") and "https://" not in html
    assert "Risk HIGH" in html or "Risk MEDIUM" in html or "Risk CRITICAL" in html
