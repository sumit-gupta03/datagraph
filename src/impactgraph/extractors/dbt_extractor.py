"""Deterministic dbt manifest.json extractor.

Emits: dbt model/seed/snapshot nodes, source nodes, exposure nodes,
DEPENDS_ON edges from the manifest's resolved DAG, WRITES_TO edges to the
materialized warehouse relations, CONTAINS edges from the model's SQL file,
declared columns, **owners** (``meta.owner`` / ``config.meta.owner`` /
exposure owner), and — when the manifest carries compiled SQL and sqlglot is
installed — true **column-to-column lineage** between models and sources.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

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

    def __init__(
        self,
        manifest_path: Union[str, Path],
        column_lineage: bool = True,
        dialect: Optional[str] = None,
        catalog_path: Optional[Union[str, Path]] = None,
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self.column_lineage = column_lineage
        self.dialect = dialect
        # dbt's catalog.json (from `dbt docs generate`): real columns per relation,
        # used to expand `select *` in compiled SQL. Auto-detected next to the manifest.
        cp = Path(catalog_path) if catalog_path else self.manifest_path.with_name("catalog.json")
        self.catalog_path = cp if cp.exists() else None
        self.unparsed: list = []  # compiled SQL that sqlglot could not parse (LLM fallback candidates)

    def extract(self) -> ImpactGraph:
        graph = ImpactGraph()
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8-sig"))
        dialect = self.dialect or (manifest.get("metadata") or {}).get("adapter_type")

        unique_to_graph_id: Dict[str, str] = {}
        relation_to_node: Dict[str, Tuple[str, str]] = {}  # lower relation name -> (graph id, display)

        def register_relation(node_id: str, display: str, database, schema, name) -> None:
            if not name:
                return
            variants = [name]
            if schema:
                variants.append(f"{schema}.{name}")
                if database:
                    variants.append(f"{database}.{schema}.{name}")
            for v in variants:
                relation_to_node.setdefault(v.lower(), (node_id, display))

        # Models / seeds / snapshots
        for unique_id, node in manifest.get("nodes", {}).items():
            resource_type = node.get("resource_type")
            if resource_type not in _RESOURCE_NODE_TYPES:
                continue
            name = node.get("name", unique_id)
            gid = f"dbt:{name}"
            unique_to_graph_id[unique_id] = gid
            config = node.get("config") or {}
            owner = _owner_of(node)
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
                        "materialized": config.get("materialized"),
                        "description": node.get("description") or "",
                        "owner": owner,
                        "tags": node.get("tags") or [],
                    },
                )
            )
            register_relation(gid, name, node.get("database"), node.get("schema"), node.get("alias") or name)

            file_path = node.get("original_file_path")
            if file_path:
                rel = Path(file_path).as_posix()
                file_id = f"file:{rel}"
                graph.add_node(Node(id=file_id, type=NodeType.FILE, name=rel, path=rel))
                graph.add_edge(Edge(src=file_id, dst=gid, type=EdgeType.CONTAINS))

            relation = _relation_id(node)
            if relation:
                graph.add_node(
                    Node(
                        id=relation,
                        type=NodeType.VIEW if config.get("materialized") == "view" else NodeType.TABLE,
                        name=relation.split(":", 1)[1],
                        meta={"owner": owner},
                    )
                )
                graph.add_edge(Edge(src=gid, dst=relation, type=EdgeType.WRITES_TO))

            for col_name in (node.get("columns") or {}).keys():
                col_id = f"column:{name}.{col_name.lower()}"
                graph.add_node(Node(id=col_id, type=NodeType.COLUMN, name=col_name.lower(), meta={"parent": gid}))
                graph.add_edge(Edge(src=gid, dst=col_id, type=EdgeType.CONTAINS))

        # Sources
        for unique_id, source in manifest.get("sources", {}).items():
            name = source.get("name", unique_id)
            source_name = source.get("source_name", "src")
            gid = f"source:{source_name}.{name}"
            unique_to_graph_id[unique_id] = gid
            graph.add_node(
                Node(
                    id=gid,
                    type=NodeType.DBT_SOURCE,
                    name=f"{source_name}.{name}",
                    meta={"unique_id": unique_id, "owner": _owner_of(source)},
                )
            )
            register_relation(
                gid, f"{source_name}.{name}", source.get("database"), source.get("schema"), source.get("identifier") or name
            )
            for col_name in (source.get("columns") or {}).keys():
                col_id = f"column:{source_name}.{name}.{col_name.lower()}"
                graph.add_node(Node(id=col_id, type=NodeType.COLUMN, name=col_name.lower(), meta={"parent": gid}))
                graph.add_edge(Edge(src=gid, dst=col_id, type=EdgeType.CONTAINS))

        # Exposures
        for unique_id, exposure in manifest.get("exposures", {}).items():
            name = exposure.get("name", unique_id)
            gid = f"exposure:{name}"
            unique_to_graph_id[unique_id] = gid
            owner_obj = exposure.get("owner") or {}
            graph.add_node(
                Node(
                    id=gid,
                    type=_EXPOSURE_TYPES.get(exposure.get("type", ""), NodeType.REPORT),
                    name=name,
                    meta={
                        "unique_id": unique_id,
                        "owner": owner_obj.get("name") or owner_obj.get("email"),
                        "owner_email": owner_obj.get("email"),
                        "url": exposure.get("url"),
                    },
                )
            )
            for dep in (exposure.get("depends_on") or {}).get("nodes", []):
                upstream = unique_to_graph_id.get(dep) or _fallback_id(dep)
                graph.add_edge(Edge(src=upstream, dst=gid, type=EdgeType.EXPOSES))

        # DAG edges
        for unique_id, node in manifest.get("nodes", {}).items():
            if unique_id not in unique_to_graph_id:
                continue
            gid = unique_to_graph_id[unique_id]
            for dep in (node.get("depends_on") or {}).get("nodes", []):
                upstream = unique_to_graph_id.get(dep) or _fallback_id(dep)
                graph.add_edge(Edge(src=gid, dst=upstream, type=EdgeType.DEPENDS_ON))

        # Column-level lineage from compiled SQL (optional; needs sqlglot)
        if self.column_lineage:
            self._add_column_lineage(graph, manifest, unique_to_graph_id, relation_to_node, dialect)
        return graph

    def _schema_mapping(self, manifest) -> dict:
        """{database: {schema: {relation: {column: type}}}} from catalog.json + manifest columns."""
        schema: dict = {}

        def put(database, sch, name, columns: dict) -> None:
            if not (sch and name and columns):
                return
            schema.setdefault(str(database or ""), {}).setdefault(str(sch), {}).setdefault(str(name), {}).update(columns)
            if database:  # also allow schema.table lookups
                schema.setdefault("", {}).setdefault(str(sch), {}).setdefault(str(name), {}).update(columns)

        catalog = {}
        if self.catalog_path:
            try:
                catalog = json.loads(self.catalog_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                catalog = {}
        for section in ("nodes", "sources"):
            for uid, entry in (catalog.get(section) or {}).items():
                md = entry.get("metadata") or {}
                cols = {c: (v.get("type") or "UNKNOWN") for c, v in (entry.get("columns") or {}).items()}
                put(md.get("database"), md.get("schema"), md.get("name"), cols)
        for uid, node in manifest.get("nodes", {}).items():
            if node.get("resource_type") in _RESOURCE_NODE_TYPES:
                cols = {c: "UNKNOWN" for c in (node.get("columns") or {})}
                put(node.get("database"), node.get("schema"), node.get("alias") or node.get("name"), cols)
        for uid, src in manifest.get("sources", {}).items():
            cols = {c: "UNKNOWN" for c in (src.get("columns") or {})}
            put(src.get("database"), src.get("schema"), src.get("identifier") or src.get("name"), cols)
        # drop the empty-database level if nothing used it, keep sqlglot happy
        if "" in schema and not schema[""]:
            del schema[""]
        return schema

    def _add_column_lineage(self, graph, manifest, unique_to_graph_id, relation_to_node, dialect) -> None:
        try:
            import sqlglot
            from .sql_extractor import add_column_lineage
        except ImportError:
            return
        schema = self._schema_mapping(manifest)

        def resolve_relation(name_lower: str):
            hit = relation_to_node.get(name_lower)
            if hit:
                return hit
            # try progressively shorter suffixes (db.schema.name -> schema.name -> name)
            parts = name_lower.split(".")
            for i in range(1, len(parts)):
                hit = relation_to_node.get(".".join(parts[i:]))
                if hit:
                    return hit
            return None

        for unique_id, node in manifest.get("nodes", {}).items():
            gid = unique_to_graph_id.get(unique_id)
            if gid is None or node.get("resource_type") != "model":
                continue
            sql = node.get("compiled_code") or node.get("compiled_sql")
            if not sql:
                continue
            try:
                query = sqlglot.parse_one(sql, read=dialect)
            except Exception as e:
                self.unparsed.append({"where": f"dbt model {node.get('name', gid)}", "sql": sql, "error": str(e)[:200]})
                continue
            if query is None:
                continue
            try:
                add_column_lineage(graph, gid, node.get("name", gid), query, dialect,
                                   resolve_relation=resolve_relation, schema=schema or None)
            except Exception as e:
                self.unparsed.append({"where": f"dbt model {node.get('name', gid)}", "sql": sql, "error": str(e)[:200]})


def _owner_of(node: dict) -> Optional[str]:
    meta = node.get("meta") or {}
    config_meta = (node.get("config") or {}).get("meta") or {}
    owner = meta.get("owner") or config_meta.get("owner")
    if isinstance(owner, dict):
        owner = owner.get("name") or owner.get("email")
    return owner


def _relation_id(node: dict) -> Optional[str]:
    database = node.get("database")
    schema = node.get("schema")
    name = node.get("alias") or node.get("name")
    if not (schema and name):
        return None
    parts = [p for p in (database, schema, name) if p]
    return "table:" + ".".join(parts).lower()


def _fallback_id(dbt_unique_id: str) -> str:
    parts = dbt_unique_id.split(".")
    kind = parts[0] if parts else "model"
    name = parts[-1] if parts else dbt_unique_id
    if kind == "source" and len(parts) >= 3:
        return f"source:{parts[-2]}.{parts[-1]}"
    if kind == "exposure":
        return f"exposure:{name}"
    return f"dbt:{name}"
