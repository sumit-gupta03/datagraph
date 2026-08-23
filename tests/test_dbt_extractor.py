from datagraph import NodeType
from datagraph.analysis import analyze_impact


def test_models_sources_exposures_extracted(dbt_graph):
    models = {n.name for n in dbt_graph.nodes(NodeType.DBT_MODEL)}
    assert {"customer", "dim_customer", "fact_booking"} <= models
    assert dbt_graph.get_node("source:raw.customers") is not None
    assert dbt_graph.get_node("exposure:revenue_report") is not None


def test_downstream_impact_of_model_change(dbt_graph):
    affected = dbt_graph.impact("dbt:customer")
    assert "dbt:dim_customer" in affected
    assert "dbt:fact_booking" in affected
    assert "exposure:revenue_report" in affected
    assert "exposure:customer_dashboard" in affected


def test_sql_file_maps_to_model(dbt_graph):
    affected = dbt_graph.impact("file:models/customer.sql")
    assert "dbt:customer" in affected
    assert "exposure:revenue_report" in affected


def test_source_change_hits_everything(dbt_graph):
    affected = dbt_graph.impact("source:raw.customers")
    assert "dbt:customer" in affected
    assert "exposure:customer_dashboard" in affected


def test_upstream_not_affected_by_downstream_change(dbt_graph):
    affected = dbt_graph.impact("dbt:fact_booking")
    assert "dbt:customer" not in affected
    assert "dbt:dim_customer" not in affected


def test_materialized_tables_linked(dbt_graph):
    affected = dbt_graph.impact("dbt:fact_booking")
    assert "table:prod.analytics.fact_booking" in affected


def test_analysis_risk_and_tests(dbt_graph):
    analysis = analyze_impact(dbt_graph, ["dbt:customer"])
    assert analysis.risk["level"] in {"MEDIUM", "HIGH", "CRITICAL"}
    assert any("dbt build" in r for r in analysis.recommended_tests)
    assert any("revenue_report" in r or "customer_dashboard" in r
               for r in analysis.recommended_tests)


def test_resolve_by_name(dbt_graph):
    node = dbt_graph.resolve("dim_customer")
    assert node is not None and node.id == "dbt:dim_customer"
