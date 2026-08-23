"""Lineage-file importer (DataHub "lineage file" format and a simple superset).

DataHub lets you declare lineage in a YAML/JSON file (its ``datahub-lineage-file``
source). datagraph reads the same shape, so lineage curated for DataHub can be
dropped straight into the impact graph::

    version: 1
    lineage:
      - entity: {name: analytics.fact_booking, type: dataset, platform: snowflake, env: PROD}
        upstream:
          - entity: {name: analytics.dim_customer, type: dataset, platform: snowflake}
          - entity: {name: raw.bookings, type: dataset, platform: snowflake}
        # optional extensions understood by datagraph:
        owner: finance
        columns:                       # column-level lineage
          customer_key:
            - {entity: {name: analytics.dim_customer}, column: customer_key}
        fineGrainedLineages:           # DataHub-style, also accepted
          - upstreams: [analytics.dim_customer.customer_key]
            downstreams: [customer_key]

YAML needs PyYAML (``pip install datagraph[all]``); JSON always works.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Union

from ..graph import Edge, EdgeType, ImpactGraph, Node, NodeType
from .base import Extractor


class LineageFileExtractor(Extractor):
    name = "lineage-file"

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)

    def extract(self) -> ImpactGraph:
        graph = ImpactGraph()
        data = _load(self.path)
        for item in data.get("lineage") or []:
            entity = item.get("entity") or {}
            ds_id = _entity_id(entity)
            if ds_id is None:
                continue
            _ensure_dataset(graph, entity, owner=item.get("owner"))
            for up in item.get("upstream") or item.get("upstreams") or []:
                up_entity = up.get("entity") if isinstance(up, dict) and "entity" in up else up
                up_id = _entity_id(up_entity) if isinstance(up_entity, dict) else ("table:" + str(up_entity).lower())
                if up_id is None or up_id == ds_id:
                    continue
                if isinstance(up_entity, dict):
                    _ensure_dataset(graph, up_entity)
                else:
                    graph.add_node(Node(id=up_id, type=NodeType.TABLE, name=str(up_entity)))
                graph.add_edge(Edge(src=ds_id, dst=up_id, type=EdgeType.DEPENDS_ON, meta={"via": "lineage-file"}))

            # column lineage, simple form
            for out_col, sources in (item.get("columns") or {}).items():
                out_col_id = _column(graph, ds_id, out_col)
                for src in sources or []:
                    src_entity = src.get("entity") or {}
                    src_ds = _entity_id(src_entity)
                    src_col = src.get("column")
                    if src_ds is None or not src_col:
                        continue
                    _ensure_dataset(graph, src_entity)
                    src_col_id = _column(graph, src_ds, src_col)
                    graph.add_edge(Edge(src=out_col_id, dst=src_col_id, type=EdgeType.DEPENDS_ON, meta={"via": "lineage-file"}))

            # DataHub-style fine-grained lineage: "dataset.column" strings or bare downstream column names
            for fg in item.get("fineGrainedLineages") or []:
                for down in fg.get("downstreams") or []:
                    down_ds, down_col = _split_col(str(down), default_ds=ds_id)
                    down_col_id = _column(graph, down_ds, down_col)
                    for up in fg.get("upstreams") or []:
                        up_ds, up_col = _split_col(str(up), default_ds=None)
                        if up_ds is None:
                            continue
                        graph.add_node(Node(id=up_ds, type=NodeType.TABLE, name=up_ds.split(":", 1)[1]))
                        up_col_id = _column(graph, up_ds, up_col)
                        graph.add_edge(Edge(src=down_col_id, dst=up_col_id, type=EdgeType.DEPENDS_ON, meta={"via": "lineage-file"}))
        return graph


def _load(path: Path) -> Dict:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as e:
            raise ImportError("PyYAML is required for YAML lineage files: pip install pyyaml") from e
        return yaml.safe_load(text) or {}
    return json.loads(text)


def _entity_id(entity: Dict):
    name = entity.get("name") if isinstance(entity, dict) else None
    if not name:
        return None
    etype = str(entity.get("type", "dataset")).lower()
    prefix = {"dataset": "table", "datajob": "job", "job": "job", "dashboard": "exposure", "chart": "exposure"}.get(etype, "table")
    return f"{prefix}:{str(name).lower()}"


def _ensure_dataset(graph: ImpactGraph, entity: Dict, owner=None) -> str:
    ds_id = _entity_id(entity)
    etype = str(entity.get("type", "dataset")).lower()
    ntype = {"dataset": NodeType.TABLE, "datajob": NodeType.DAG, "job": NodeType.DAG, "dashboard": NodeType.DASHBOARD, "chart": NodeType.REPORT}.get(etype, NodeType.TABLE)
    graph.add_node(
        Node(
            id=ds_id,
            type=ntype,
            name=str(entity.get("name")),
            meta={"platform": entity.get("platform"), "env": entity.get("env"), "owner": owner, "source": "lineage-file"},
        )
    )
    return ds_id


def _column(graph: ImpactGraph, ds_id: str, col: str) -> str:
    col = str(col).lower()
    col_id = f"column:{ds_id.split(':', 1)[1]}.{col}"
    graph.add_node(Node(id=col_id, type=NodeType.COLUMN, name=col, meta={"parent": ds_id}))
    graph.add_edge(Edge(src=ds_id, dst=col_id, type=EdgeType.CONTAINS))
    return col_id


def _split_col(ref: str, default_ds):
    """'analytics.dim_customer.customer_key' -> ('table:analytics.dim_customer', 'customer_key');
    'customer_key' -> (default_ds, 'customer_key')."""
    if "." in ref:
        ds, _, col = ref.rpartition(".")
        return "table:" + ds.lower(), col
    return default_ds, ref
