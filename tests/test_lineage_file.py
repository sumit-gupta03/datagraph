import json

import pytest

from impactgraph import LineageFileExtractor

DOC = {
    "version": 1,
    "lineage": [
        {
            "entity": {"name": "analytics.fact_booking", "type": "dataset", "platform": "snowflake", "env": "PROD"},
            "owner": "finance",
            "upstream": [
                {"entity": {"name": "analytics.dim_customer", "type": "dataset", "platform": "snowflake"}},
                {"entity": {"name": "raw.bookings", "type": "dataset", "platform": "snowflake"}},
            ],
            "columns": {"customer_key": [{"entity": {"name": "analytics.dim_customer"}, "column": "customer_key"}]},
        },
        {
            "entity": {"name": "analytics.dim_customer", "type": "dataset", "platform": "snowflake"},
            "upstream": [{"entity": {"name": "raw.customers", "type": "dataset"}}],
            "fineGrainedLineages": [{"upstreams": ["raw.customers.customer_id"], "downstreams": ["customer_key"]}],
        },
        {
            "entity": {"name": "revenue", "type": "dashboard", "platform": "looker"},
            "upstream": [{"entity": {"name": "analytics.fact_booking", "type": "dataset"}}],
        },
    ],
}


def test_json_lineage_file(tmp_path):
    p = tmp_path / "lineage.json"
    p.write_text(json.dumps(DOC), encoding="utf-8")
    graph = LineageFileExtractor(p).extract()
    affected = graph.impact("table:raw.customers")
    assert "table:analytics.dim_customer" in affected
    assert "table:analytics.fact_booking" in affected
    assert "exposure:revenue" in affected
    assert graph.get_node("table:analytics.fact_booking").owner == "finance"


def test_column_lineage_both_forms(tmp_path):
    p = tmp_path / "lineage.json"
    p.write_text(json.dumps(DOC), encoding="utf-8")
    graph = LineageFileExtractor(p).extract()
    affected = graph.impact("column:raw.customers.customer_id")
    assert "column:analytics.dim_customer.customer_key" in affected   # fineGrainedLineages
    assert "column:analytics.fact_booking.customer_key" in affected   # columns mapping


def test_yaml_lineage_file(tmp_path):
    yaml = pytest.importorskip("yaml")
    p = tmp_path / "lineage.yml"
    p.write_text(yaml.safe_dump(DOC), encoding="utf-8")
    graph = LineageFileExtractor(p).extract()
    assert "table:analytics.fact_booking" in graph.impact("table:raw.bookings")
