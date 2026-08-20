"""High-level impact analysis: blast radius + risk + test plan in one call."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..graph import ImpactGraph
from .risk import risk_score
from .tests_recommender import recommend_tests


@dataclass
class ImpactAnalysis:
    changed: List[str]
    affected: Dict[str, int]  # node id -> depth
    risk: Dict  # {"score": float, "level": str}
    recommended_tests: List[str]
    trees: List[Dict] = field(default_factory=list)
    _graph: Optional[ImpactGraph] = None

    def summary_by_type(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        if self._graph is None:
            return counts
        for node_id in self.affected:
            node = self._graph.get_node(node_id)
            if node:
                counts[node.type.value] = counts.get(node.type.value, 0) + 1
        return counts

    def to_dict(self) -> Dict:
        return {
            "changed": self.changed,
            "affected": self.affected,
            "affected_by_type": self.summary_by_type(),
            "risk": self.risk,
            "recommended_tests": self.recommended_tests,
            "trees": self.trees,
        }


def analyze_impact(
    graph: ImpactGraph,
    changed: List[str],
    max_depth: Optional[int] = None,
) -> ImpactAnalysis:
    """Compute the blast radius, risk level, and test plan for changed nodes."""
    resolved: List[str] = []
    for ref in changed:
        node = graph.resolve(ref)
        if node is not None:
            resolved.append(node.id)
        elif ref in graph:
            resolved.append(ref)

    affected = graph.impact(resolved, max_depth=max_depth)
    risk = risk_score(graph, affected)
    tests = recommend_tests(graph, affected)
    trees = [graph.impact_tree(nid, max_depth=max_depth) for nid in resolved]
    return ImpactAnalysis(
        changed=resolved,
        affected=affected,
        risk=risk,
        recommended_tests=tests,
        trees=trees,
        _graph=graph,
    )
