import json

from datagraph import OpenLineageExtractor, NodeType


def _events():
    return [
        {
            "eventType": "COMPLETE",
            "job": {"namespace": "airflow", "name": "load_dim_customer"},
            "inputs": [
                {"namespace": "snowflake://acct", "name": "prod.raw.customers",
                 "facets": {"schema": {"fields": [{"name": "customer_id", "type": "INT"}, {"name": "email", "type": "STRING"}]},
                            "ownership": {"owners": [{"name": "data-platform", "type": "TEAM"}]}}},
            ],
            "outputs": [
                {"namespace": "snowflake://acct", "name": "prod.analytics.dim_customer",
                 "facets": {
                     "schema": {"fields": [{"name": "customer_key"}, {"name": "email"}]},
                     "columnLineage": {"fields": {
                         "customer_key": {"inputFields": [{"namespace": "snowflake://acct", "name": "prod.raw.customers", "field": "customer_id"}]},
                         "email": {"inputFields": [{"namespace": "snowflake://acct", "name": "prod.raw.customers", "field": "email"}]},
                     }}}},
            ],
        },
        {
            "eventType": "COMPLETE",
            "job": {"namespace": "airflow", "name": "load_fact_booking"},
            "inputs": [{"namespace": "snowflake://acct", "name": "prod.analytics.dim_customer"}],
            "outputs": [{"namespace": "snowflake://acct", "name": "prod.analytics.fact_booking"}],
        },
    ]


def test_json_array_events(tmp_path):
    p = tmp_path / "events.json"
    p.write_text(json.dumps(_events()), encoding="utf-8")
    graph = OpenLineageExtractor(p).extract()
    assert graph.get_node("job:airflow/load_dim_customer").type == NodeType.DAG
    assert graph.get_node("table:prod.raw.customers").owner == "data-platform"
    affected = graph.impact("table:prod.raw.customers")
    assert "table:prod.analytics.dim_customer" in affected
    assert "table:prod.analytics.fact_booking" in affected
    assert "job:airflow/load_dim_customer" in affected           # job reads it


def test_ndjson_and_column_lineage_facet(tmp_path):
    p = tmp_path / "events.ndjson"
    p.write_text("\n".join(json.dumps(e) for e in _events()), encoding="utf-8")
    graph = OpenLineageExtractor(p).extract()
    affected = graph.impact("column:prod.raw.customers.customer_id")
    assert "column:prod.analytics.dim_customer.customer_key" in affected
    assert "column:prod.analytics.dim_customer.email" not in affected
