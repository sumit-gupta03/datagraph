"""Column-level changes propagate through the owning model to downstream artifacts."""

from impactgraph import Edge, EdgeType, Node, NodeType


def test_column_change_reaches_downstream_models_and_exposures(dbt_graph):
    affected = dbt_graph.impact("column:customer.customer_id")
    assert "dbt:customer" in affected            # owning model (contract changed)
    assert "dbt:dim_customer" in affected        # downstream
    assert "dbt:fact_booking" in affected
    assert "exposure:revenue_report" in affected
    # sibling columns of the owning model are NOT flagged
    assert "column:customer.email" not in affected


def test_same_named_downstream_column_is_flagged(dbt_graph):
    # give dim_customer a column with the same name as the changed one
    dbt_graph.add_node(
        Node(id="column:dim_customer.email", type=NodeType.COLUMN, name="email", meta={"parent": "dbt:dim_customer"})
    )
    dbt_graph.add_edge(Edge(src="dbt:dim_customer", dst="column:dim_customer.email", type=EdgeType.CONTAINS))
    dbt_graph.add_node(
        Node(id="column:dim_customer.other", type=NodeType.COLUMN, name="other", meta={"parent": "dbt:dim_customer"})
    )
    dbt_graph.add_edge(Edge(src="dbt:dim_customer", dst="column:dim_customer.other", type=EdgeType.CONTAINS))

    affected = dbt_graph.impact("column:customer.email")
    assert "column:dim_customer.email" in affected       # same name downstream
    assert "column:dim_customer.other" not in affected   # unrelated column


def test_column_tree_roots_at_column(dbt_graph):
    tree = dbt_graph.impact_tree("column:customer.customer_id")
    assert tree["id"] == "column:customer.customer_id"
    assert len(tree["children"]) == 1
    assert tree["children"][0]["id"] == "dbt:customer"
    assert tree["children"][0]["via"] == "contains"


def test_model_change_still_flags_its_own_columns(dbt_graph):
    affected = dbt_graph.impact("dbt:customer")
    assert "column:customer.customer_id" in affected
    assert "column:customer.email" in affected
