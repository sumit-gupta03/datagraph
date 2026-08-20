"""Deterministic dbt manifest.json extractor.

Emits: dbt model/seed/snapshot nodes, source nodes, exposure nodes,
DEPENDS_ON edges from the manifest's resolved DAG, WRITES_TO edges to the
materialized warehouse relations, and CONTAINS edges from the model's SQL
file so a git diff on ``models/customer.sql`` maps to its model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Union

from ..graph import Edge, EdgeType, ImpactGraph, Node, NodeType
from .base import Extractor

_RESOURCE_NODE_TYPES = {
    "model": NodeType.DBT_MODEL,
    "seed": NodeType.DBT_SEED,
    "snapshot": NodeType.DBT_SNAPSHOT,
}

_EXPOSURE_TYPES = {
    "dashboard": NodeType.DASHBOARD,
    "notebook": NodeType.REPORT,
    "analysis": NodeType.REPORT,
    "ml": NodeType.REPORT,
    "application": NodeType.API,
}


class DbtExtractor(Extractor):
    name = "dbt"

    def __init__(self, manifest_path: Union[str, Path]) -> None:
        self.manifest_path = Path(manifest_path)

    def extract(self) -> ImpactGraph:
        graph = ImpactGraph()
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8-sig"))

        unique_to_graph_id: Dict[str, str] = {}

        # Models / seeds / snapshots
        for unique_id, node in manifest.get("nodes", {}).items():
            resource_type = node.get("resource_type")
            if resource_type not in _RESOURCE_NODE_TYPES:
                continue
            name = node.get("name", unique_id)
            gid = f"dbt:{name}"
            unique_to_graph_id[unique_id] = gid
            graph.add_node(
                Node(
                    id=gid,
                    type=_RESOURCE_NODE_TYPES[resource_type],
                    name=name,
                    path=node.get("original_file_path"),
                    meta={
                        "unique_id": unique_id,
                        "schema": node.get("schema"),
                        "database": node.get("database"),
                        "materialized": (node.get("config") or {}).get("materialized"),
                        "description": node.get("description") or "",
                    },
                )
            )
            # SQL file -> model, so changed files map to models
            file_path = node.get("original_file_path")
            if file_path:
                rel = Path(file_path).as_posix()
                file_id = f"file:{rel}"
                graph.add_node(Node(id=file_id, type=NodeType.FILE, name=rel, path=rel))
                graph.add_edge(Edge(src=file_id, dst=gid, type=EdgeType.CONTAINS))

            # Materialized relation
            relation = _relation_id(node)
            if relation:
                graph.add_node(
                    Node(
                        id=relation,
                        type=NodeType.VIEW
                        if (node.get("config") or {}).get("materialized") == "view"
                        else NodeType.TABLE,
                        name=relation.split(":", 1)[1],
                    )
                )
                graph.add_edge(Edge(src=gid, dst=relation, type=EdgeType.WRITES_TO))

            # Declared columns
            for col_name in (node.get("columns") or {}).keys():
                col_id = f"column:{name}.{col_name}"
                graph.add_node(
                    Node(id=col_id, type=NodeType.COLUMN, name=col_name, meta={"parent": gid})
                )
                graph.add_edge(Edge(src=gid, dst=col_id, type=EdgeType.CONTAINS))

        # Sources
        for unique_id, source in manifest.get("sources", {}).items():
            name = source.get("name", unique_id)
            gid = f"source:{source.get('source_name', 'src')}.{name}"
            unique_to_graph_id[unique_id] = gid
            graph.add_node(
                Node(
                    id=gid,
                    type=NodeType.DBT_SOURCE,
                    name=f"{source.get('source_name', 'src')}.{name}",
                    meta={"unique_id": unique_id},
                )
            )

        # Exposures (dashboards, reports, apps)
        for unique_id, exposure in manifest.get("exposures", {}).items():
            name = exposure.get("name", unique_id)
            gid = f"exposure:{name}"
            unique_to_graph_id[unique_id] = gid
            graph.add_node(
                Node(
                    id=gid,
                    type=_EXPOSURE_TYPES.get(exposure.get("type", ""), NodeType.REPORT),
                    name=name,
                    meta={"unique_id": unique_id, "owner": (exposure.get("owner") or {}).get("name")},
                )
            )
            for dep in (exposure.get("depends_on") or {}).get("nodes", []):
                upstream = unique_to_graph_id.get(dep) or _fallback_id(dep)
                graph.add_edge(Edge(src=upstream, dst=gid, type=EdgeType.EXPOSES))

        # DAG edges: model depends on its upstream nodes
        for unique_id, node in manifest.get("nodes", {}).items():
            if unique_id not in unique_to_graph_id:
                continue
            gid = unique_to_graph_id[unique_id]
            for dep in (node.get("depends_on") or {}).get("nodes", []):
                upstream = unique_to_graph_id.get(dep) or _fallback_id(dep)
                graph.add_edge(Edge(src=gid, dst=upstream, type=EdgeType.DEPENDS_ON))

        return graph


def _relation_id(node: dict) -> Optional[str]:
    database = node.get("database")
    schema = node.get("schema")
    name = node.get("alias") or node.get("name")
    if not (schema and name):
        return None
    parts = [p for p in (database, schema, name) if p]
    return "table:" + ".".join(parts).lower()


def _fallback_id(dbt_unique_id: str) -> str:
    """Map an unresolved dbt unique_id (e.g. model.proj.foo) to a graph id."""
    parts = dbt_unique_id.split(".")
    kind = parts[0] if parts else "model"
    name = parts[-1] if parts else dbt_unique_id
    if kind == "source" and len(parts) >= 3:
        return f"source:{parts[-2]}.{parts[-1]}"
    if kind == "exposure":
        return f"exposure:{name}"
    return f"dbt:{name}"
