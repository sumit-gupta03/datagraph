from impactgraph import DataHubExtractor, NodeType


def _urn(name):
    return f"urn:li:dataset:(urn:li:dataPlatform:snowflake,{name},PROD)"


PAGE = {
    "data": {"search": {"total": 2, "searchResults": [
        {"entity": {
            "urn": _urn("prod.analytics.dim_customer"), "name": "prod.analytics.dim_customer",
            "platform": {"name": "snowflake"},
            "schemaMetadata": {"fields": [{"fieldPath": "customer_key", "nativeDataType": "NUMBER"}, {"fieldPath": "email", "nativeDataType": "VARCHAR"}]},
            "ownership": {"owners": [{"owner": {"username": "analytics-eng"}}]},
            "upstream": {"relationships": [
                {"type": "DownstreamOf", "entity": {"urn": _urn("prod.raw.customers"), "name": "prod.raw.customers"}},
                {"type": "Produces", "entity": {"urn": "urn:li:dataJob:(urn:li:dataFlow:(airflow,nightly,prod),build_dim)"}},
            ]},
            "fineGrainedLineages": [{"upstreams": [{"urn": _urn("prod.raw.customers"), "path": "customer_id"}],
                                     "downstreams": [{"urn": _urn("prod.analytics.dim_customer"), "path": "customer_key"}]}],
        }},
        {"entity": {
            "urn": _urn("prod.raw.customers"), "name": "prod.raw.customers", "platform": {"name": "snowflake"},
            "schemaMetadata": {"fields": [{"fieldPath": "customer_id"}, {"fieldPath": "email"}]},
            "upstream": {"relationships": []}, "fineGrainedLineages": [],
        }},
    ]}}
}


def test_datahub_import_with_stub_transport():
    calls = []

    def transport(query, variables):
        calls.append(variables)
        return PAGE

    graph = DataHubExtractor("https://datahub.example.com", token="t", transport=transport).extract()
    assert calls and calls[0]["start"] == 0
    assert graph.get_node("table:prod.analytics.dim_customer").owner == "analytics-eng"
    assert graph.get_node("job:nightly/build_dim").type == NodeType.DAG
    edges = {(e.src, e.dst, e.type.value) for e in graph.edges()}
    assert ("table:prod.analytics.dim_customer", "table:prod.raw.customers", "depends_on") in edges
    assert ("job:nightly/build_dim", "table:prod.analytics.dim_customer", "writes_to") in edges
    assert ("column:prod.analytics.dim_customer.customer_key", "column:prod.raw.customers.customer_id", "depends_on") in edges
    # impact flows from the raw column to the derived (renamed) column
    assert "column:prod.analytics.dim_customer.customer_key" in graph.impact("column:prod.raw.customers.customer_id")


def test_datahub_graphql_error_is_raised():
    import pytest

    def transport(query, variables):
        return {"errors": [{"message": "Unauthorized"}]}

    with pytest.raises(RuntimeError, match="Unauthorized"):
        DataHubExtractor("https://datahub.example.com", transport=transport).extract()
