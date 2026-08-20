"""impactgraph — AI-powered Change Impact Graph for data and code systems.

Build a deterministic dependency graph from real engineering artifacts
(Python AST, dbt manifest, SQL lineage, git diff), then ask:
"if I change this, what can break?"
"""

from .graph import Edge, EdgeType, ImpactGraph, Node, NodeType
from .analysis import ImpactAnalysis, analyze_impact
from .extractors import (
    DbtExtractor,
    PythonExtractor,
    changed_node_ids,
    collect_changes,
)

__version__ = "0.1.0"

__all__ = [
    "Edge",
    "EdgeType",
    "ImpactGraph",
    "Node",
    "NodeType",
    "ImpactAnalysis",
    "analyze_impact",
    "DbtExtractor",
    "PythonExtractor",
    "changed_node_ids",
    "collect_changes",
    "__version__",
]
