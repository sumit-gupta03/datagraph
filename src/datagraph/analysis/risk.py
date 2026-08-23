"""Deterministic risk scoring over a blast radius."""

from __future__ import annotations

from typing import Dict

from ..graph import ImpactGraph, NodeType

# How much a single affected node of each type contributes to risk.
NODE_WEIGHTS: Dict[NodeType, int] = {
    NodeType.DASHBOARD: 8,
    NodeType.REPORT: 8,
    NodeType.EXPOSURE: 8,
    NodeType.API: 6,
    NodeType.LAMBDA: 4,
    NodeType.TABLE: 3,
    NodeType.VIEW: 3,
    NodeType.DBT_MODEL: 3,
    NodeType.DBT_SNAPSHOT: 3,
    NodeType.DBT_SEED: 2,
    NodeType.DBT_SOURCE: 2,
    NodeType.DAG: 3,
    NodeType.TASK: 2,
    NodeType.FUNCTION: 1,
    NodeType.CLASS: 1,
    NodeType.COLUMN: 1,
    NodeType.MODULE: 1,
    NodeType.FILE: 1,
}

LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


def risk_score(graph: ImpactGraph, affected: Dict[str, int]) -> Dict:
    """Score a blast radius (node id -> depth) into a risk level.

    Deterministic: weight affected nodes by type, add a small bonus for wide
    direct impact, and cap the depth discount so far-away dashboards still count.
    """
    score = 0.0
    for node_id, depth in affected.items():
        node = graph.get_node(node_id)
        if node is None:
            continue
        weight = NODE_WEIGHTS.get(node.type, 1)
        # data-aware adjustment when the node has been profiled
        prof = (node.meta or {}).get("profile") or {}
        rows = prof.get("row_count")
        if isinstance(rows, int):
            if rows == 0:
                weight *= 0.5           # empty table: low stakes
            elif rows >= 1_000_000:
                weight *= 1.5           # big table: higher stakes
        # depth discount: direct hits count full, deeper hits at least half
        discount = max(0.5, 1.0 - 0.1 * (depth - 1))
        score += weight * discount

    direct_hits = sum(1 for d in affected.values() if d == 1)
    score += min(direct_hits, 10)  # breadth bonus

    if score >= 40:
        level = "CRITICAL"
    elif score >= 18:
        level = "HIGH"
    elif score >= 6:
        level = "MEDIUM"
    else:
        level = "LOW"

    return {"score": round(score, 1), "level": level}
