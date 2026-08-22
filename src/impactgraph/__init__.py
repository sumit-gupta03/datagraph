"""impactgraph — AI-powered Change Impact Graph for data and code systems.

Build a deterministic dependency graph from real engineering artifacts
(Python AST, dbt manifest, SQL lineage, git diff, OpenLineage / DataHub
lineage files, warehouse information_schema), then ask:
"if I change this, what can break?"
"""

from .graph import EXTRACTED, INFERRED, Edge, EdgeType, ImpactGraph, Node, NodeType, diff_graphs
from .analysis import ImpactAnalysis, analyze_impact
from .extractors import (
    DbtExtractor,
    LineageFileExtractor,
    OpenLineageExtractor,
    PythonExtractor,
    SqlExtractor,
    WarehouseExtractor,
    changed_node_ids,
    collect_changes,
)

__version__ = "0.2.0"

__all__ = [
    "Edge",
    "EdgeType",
    "ImpactGraph",
    "Node",
    "NodeType",
    "EXTRACTED",
    "INFERRED",
    "diff_graphs",
    "ImpactAnalysis",
    "analyze_impact",
    "DbtExtractor",
    "PythonExtractor",
    "SqlExtractor",
    "OpenLineageExtractor",
    "LineageFileExtractor",
    "WarehouseExtractor",
    "changed_node_ids",
    "collect_changes",
    "__version__",
]
