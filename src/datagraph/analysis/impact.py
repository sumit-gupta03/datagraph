"""High-level impact analysis: blast radius + risk + owners + test plan in one call."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..graph import ImpactGraph
from ..metadata import deprecation_warnings
from .risk import risk_score
from .tests_recommender import recommend_tests


@dataclass
class ImpactAnalysis:
    changed: List[str]
    affected: Dict[str, int]  # node id -> depth
    risk: Dict  # {"score": float, "level": str}
    recommended_tests: List[str]
    trees: List[Dict] = field(default_factory=list)
    owners: Dict[str, List[str]] = field(default_factory=dict)  # owner -> affected node names
    include_inferred: bool = True
    warnings: List[str] = field(default_factory=list)   # deprecated assets, failing tests downstream
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
            "owners": self.owners,
            "recommended_tests": self.recommended_tests,
            "warnings": self.warnings,
            "include_inferred": self.include_inferred,
            "trees": self.trees,
        }


def analyze_impact(
    graph: ImpactGraph,
    changed: List[str],
    max_depth: Optional[int] = None,
    include_inferred: bool = True,
) -> ImpactAnalysis:
    """Compute the blast radius, risk level, owners to notify, and test plan."""
    resolved: List[str] = []
    for ref in changed:
        node = graph.resolve(ref)
        if node is not None:
            if node.id not in resolved:
                resolved.append(node.id)
        elif ref in graph and ref not in resolved:
            resolved.append(ref)

    affected = graph.impact(resolved, max_depth=max_depth, include_inferred=include_inferred)
    risk = risk_score(graph, affected)
    tests = recommend_tests(graph, affected)
    trees = [graph.impact_tree(nid, max_depth=max_depth, include_inferred=include_inferred) for nid in resolved]

    owners: Dict[str, List[str]] = {}
    for nid in affected:
        node = graph.get_node(nid)
        if node and node.owner:
            owners.setdefault(str(node.owner), []).append(node.name)
    for k in owners:
        owners[k] = sorted(set(owners[k]))

    warnings = deprecation_warnings(graph, affected)
    for nid in list(affected) + [r for r in resolved if r not in affected]:   # dbt run_results outcomes
        node = graph.get_node(nid)
        status = (node.meta.get("status") or {}) if node else {}
        if status.get("tests_failed"):
            failing = ", ".join(status.get("failing_tests", [])[:3])
            warnings.append(f"'{node.name}' has {status['tests_failed']} failing dbt test(s)"
                            + (f": {failing}" if failing else ""))
        if status.get("freshness") in ("error", "warn"):
            warnings.append(f"source '{node.name}' freshness is {status['freshness']}"
                            + (f" (last loaded {status['max_loaded_at']})" if status.get("max_loaded_at") else ""))

    return ImpactAnalysis(
        changed=resolved,
        affected=affected,
        risk=risk,
        recommended_tests=tests,
        trees=trees,
        owners=dict(sorted(owners.items())),
        include_inferred=include_inferred,
        warnings=warnings,
        _graph=graph,
    )
