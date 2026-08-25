import json

from datagraph import Edge, EdgeType, ImpactGraph
from datagraph.analysis import analyze_impact


def test_merge_unifies_code_and_data(py_graph, dbt_graph):
    unified = ImpactGraph()
    unified.merge(py_graph)
    unified.merge(dbt_graph)
    # Bridge the worlds: the API function reads the fact_booking table.
    unified.add_edge(
        Edge(
            src="func:api.py::customers_endpoint",
            dst="table:prod.analytics.fact_booking",
            type=EdgeType.DEPENDS_ON,
        )
    )
    # Changing the dbt customer model now reaches Python code.
    affected = unified.impact("dbt:customer")
    assert "func:api.py::customers_endpoint" in affected


def test_save_and_load_roundtrip(tmp_path, dbt_graph):
    path = tmp_path / "graph.json"
    dbt_graph.save(path)
    loaded = ImpactGraph.load(path)
    assert len(loaded) == len(dbt_graph)
    assert len(loaded.edges()) == len(dbt_graph.edges())
    assert loaded.impact("dbt:customer") == dbt_graph.impact("dbt:customer")


def test_impact_tree_shape(dbt_graph):
    tree = dbt_graph.impact_tree("dbt:customer")
    assert tree["id"] == "dbt:customer"
    child_ids = {c["id"] for c in tree["children"]}
    assert "dbt:dim_customer" in child_ids


def test_impact_paths(dbt_graph):
    paths = dbt_graph.impact_paths("dbt:customer", "exposure:revenue_report")
    assert paths, "expected at least one propagation path"
    assert all(p[0] == "dbt:customer" and p[-1] == "exposure:revenue_report" for p in paths)


def test_max_depth_limits_blast_radius(dbt_graph):
    shallow = dbt_graph.impact("dbt:customer", max_depth=1)
    deep = dbt_graph.impact("dbt:customer")
    assert set(shallow) < set(deep)
    assert all(d == 1 for d in shallow.values())


def test_analysis_to_dict_is_json_serializable(dbt_graph):
    analysis = analyze_impact(dbt_graph, ["customer"])
    payload = json.dumps(analysis.to_dict())
    assert "revenue_report" in payload
