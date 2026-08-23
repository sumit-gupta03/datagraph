import json

import pytest

from datagraph import DbtExtractor, analyze_impact


@pytest.fixture
def compiled_manifest(tmp_path):
    manifest = {
        "metadata": {"adapter_type": "snowflake"},
        "nodes": {
            "model.p.customer": {
                "resource_type": "model", "name": "customer", "schema": "analytics", "database": "prod",
                "original_file_path": "models/customer.sql", "config": {"materialized": "view", "meta": {"owner": "data-platform"}},
                "columns": {"customer_id": {}, "email": {}},
                "depends_on": {"nodes": ["source.p.raw.customers"]},
                "compiled_code": "select c.customer_id, c.email from prod.raw.customers c",
            },
            "model.p.dim_customer": {
                "resource_type": "model", "name": "dim_customer", "schema": "analytics", "database": "prod",
                "original_file_path": "models/dim_customer.sql", "config": {"materialized": "table"},
                "meta": {"owner": "analytics-eng"},
                "columns": {},
                "depends_on": {"nodes": ["model.p.customer"]},
                "compiled_code": "select customer_id as customer_key, email from prod.analytics.customer",
            },
            "model.p.fact_booking": {
                "resource_type": "model", "name": "fact_booking", "schema": "analytics", "database": "prod",
                "original_file_path": "models/fact_booking.sql", "config": {"materialized": "table"},
                "columns": {},
                "depends_on": {"nodes": ["model.p.dim_customer"]},
                "compiled_code": "select d.customer_key, d.email as customer_email from prod.analytics.dim_customer d",
            },
        },
        "sources": {
            "source.p.raw.customers": {"source_name": "raw", "name": "customers", "database": "prod", "schema": "raw", "identifier": "customers"}
        },
        "exposures": {
            "exposure.p.revenue_report": {
                "name": "revenue_report", "type": "dashboard", "owner": {"name": "finance", "email": "fin@x.com"},
                "depends_on": {"nodes": ["model.p.fact_booking"]},
            }
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_compiled_sql_gives_column_edges_between_models(compiled_manifest):
    pytest.importorskip("sqlglot")
    graph = DbtExtractor(compiled_manifest).extract()
    edges = {(e.src, e.dst) for e in graph.edges() if e.type.value == "depends_on"}
    assert ("column:dim_customer.customer_key", "column:customer.customer_id") in edges
    assert ("column:fact_booking.customer_key", "column:dim_customer.customer_key") in edges
    assert ("column:fact_booking.customer_email", "column:dim_customer.email") in edges
    # source columns resolved to the dbt source node, not a raw table id
    assert ("column:customer.customer_id", "column:raw.customers.customer_id") in edges


def test_renamed_column_impact_through_dbt_models(compiled_manifest):
    pytest.importorskip("sqlglot")
    graph = DbtExtractor(compiled_manifest).extract()
    affected = graph.impact("column:customer.customer_id")
    assert "column:dim_customer.customer_key" in affected      # renamed — found by lineage
    assert "column:fact_booking.customer_key" in affected
    assert "column:dim_customer.email" not in affected          # unrelated


def test_owners_collected_and_reported(compiled_manifest):
    graph = DbtExtractor(compiled_manifest).extract()
    assert graph.get_node("dbt:customer").owner == "data-platform"
    assert graph.get_node("dbt:dim_customer").owner == "analytics-eng"
    assert graph.get_node("exposure:revenue_report").owner == "finance"
    analysis = analyze_impact(graph, ["dbt:customer"])
    assert "analytics-eng" in analysis.owners
    assert "finance" in analysis.owners
    assert "revenue_report" in analysis.owners["finance"]
    assert "owners" in analysis.to_dict()


def test_column_lineage_can_be_disabled(compiled_manifest):
    graph = DbtExtractor(compiled_manifest, column_lineage=False).extract()
    edges = {(e.src, e.dst) for e in graph.edges() if e.type.value == "depends_on"}
    assert ("column:dim_customer.customer_key", "column:customer.customer_id") not in edges
