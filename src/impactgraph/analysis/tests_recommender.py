"""Rule-based test recommendations for a blast radius."""

from __future__ import annotations

from typing import Dict, List

from ..graph import ImpactGraph, NodeType


def recommend_tests(graph: ImpactGraph, affected: Dict[str, int]) -> List[str]:
    """Deterministic, de-duplicated test plan derived from what is affected."""
    recs: List[str] = []
    seen = set()

    def add(rec: str) -> None:
        if rec not in seen:
            seen.add(rec)
            recs.append(rec)

    dbt_models = []
    for node_id in affected:
        node = graph.get_node(node_id)
        if node is None:
            continue
        if node.type in (NodeType.DBT_MODEL, NodeType.DBT_SNAPSHOT):
            dbt_models.append(node.name)
        elif node.type == NodeType.COLUMN:
            parent = node.meta.get("parent", "")
            add(
                f"Add/verify a dbt relationship + not_null test for column "
                f"'{node.name}' ({parent})"
            )
        elif node.type in (NodeType.TABLE, NodeType.VIEW):
            add(f"Run a schema/contract check on {node.name} (column names & types)")
        elif node.type == NodeType.FUNCTION:
            module = node.path or node.name
            add(f"Run unit tests covering {module} (function '{node.name}')")
        elif node.type == NodeType.API:
            add(f"Run the API contract test for '{node.name}'")
        elif node.type == NodeType.LAMBDA:
            add(f"Run an integration test invoking lambda '{node.name}'")
        elif node.type in (NodeType.DASHBOARD, NodeType.REPORT, NodeType.EXPOSURE):
            add(f"Manually validate '{node.name}' after deploy (numbers & filters)")
        elif node.type == NodeType.DAG:
            add(f"Trigger a test run of DAG '{node.name}' in a non-prod environment")

    if dbt_models:
        shown = " ".join(f"{m}+" for m in sorted(set(dbt_models))[:5])
        add(f"dbt build --select {shown}  (rebuild affected models and their tests)")

    if not recs:
        recs.append("No downstream artifacts detected; run the standard test suite.")
    return recs
