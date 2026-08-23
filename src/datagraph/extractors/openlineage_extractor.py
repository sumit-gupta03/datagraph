"""OpenLineage event importer.

Reads OpenLineage run events (a JSON array, or NDJSON with one event per
line — the format Marquez / DataHub / Airflow emit) and turns them into graph
nodes and edges:

  * every input/output dataset  -> ``table:<name>`` node (namespace kept in meta)
  * every job                   -> ``job:<namespace>/<name>`` node (type DAG)
  * job DEPENDS_ON its inputs, job WRITES_TO its outputs, and each output
    DEPENDS_ON each input (dataset-level lineage)
  * ``schema`` facet fields     -> COLUMN nodes
  * ``columnLineage`` facet     -> column -> column DEPENDS_ON edges
  * ``ownership`` facet         -> ``owner`` on the dataset

This is how datagraph inherits lineage that DataHub / Marquez / Airflow
already collected, and adds the code side on top.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, Union

from ..graph import Edge, EdgeType, ImpactGraph, Node, NodeType
from .base import Extractor


class OpenLineageExtractor(Extractor):
    name = "openlineage"

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)

    def extract(self) -> ImpactGraph:
        graph = ImpactGraph()
        for event in _read_events(self.path):
            self._add_event(graph, event)
        return graph

    def _add_event(self, graph: ImpactGraph, event: dict) -> None:
        job = event.get("job") or {}
        job_id = None
        if job.get("name"):
            job_id = f"job:{job.get('namespace', 'default')}/{job['name']}"
            graph.add_node(
                Node(id=job_id, type=NodeType.DAG, name=job["name"], meta={"namespace": job.get("namespace"), "source": "openlineage"})
            )
        inputs = [self._dataset(graph, d) for d in event.get("inputs") or []]
        outputs = [self._dataset(graph, d) for d in event.get("outputs") or []]
        for i in inputs:
            if job_id:
                graph.add_edge(Edge(src=job_id, dst=i, type=EdgeType.DEPENDS_ON))
        for o in outputs:
            if job_id:
                graph.add_edge(Edge(src=job_id, dst=o, type=EdgeType.WRITES_TO))
            for i in inputs:
                if i != o:
                    graph.add_edge(Edge(src=o, dst=i, type=EdgeType.DEPENDS_ON, meta={"via": "openlineage"}))
        # column lineage facet lives on output datasets
        for d in event.get("outputs") or []:
            out_id = _dataset_id(d)
            facet = ((d.get("facets") or {}).get("columnLineage") or {}).get("fields") or {}
            for out_col, spec in facet.items():
                out_col_id = f"column:{_bare(out_id)}.{out_col.lower()}"
                graph.add_node(Node(id=out_col_id, type=NodeType.COLUMN, name=out_col.lower(), meta={"parent": out_id}))
                graph.add_edge(Edge(src=out_id, dst=out_col_id, type=EdgeType.CONTAINS))
                for inp in spec.get("inputFields") or []:
                    in_ds = "table:" + str(inp.get("name", "")).lower()
                    in_col = inp.get("field")
                    if not inp.get("name") or not in_col:
                        continue
                    in_col_id = f"column:{_bare(in_ds)}.{in_col.lower()}"
                    graph.add_node(Node(id=in_ds, type=NodeType.TABLE, name=inp["name"], meta={"namespace": inp.get("namespace")}))
                    graph.add_node(Node(id=in_col_id, type=NodeType.COLUMN, name=in_col.lower(), meta={"parent": in_ds}))
                    graph.add_edge(Edge(src=in_ds, dst=in_col_id, type=EdgeType.CONTAINS))
                    graph.add_edge(Edge(src=out_col_id, dst=in_col_id, type=EdgeType.DEPENDS_ON, meta={"via": "openlineage"}))

    def _dataset(self, graph: ImpactGraph, d: dict) -> str:
        ds_id = _dataset_id(d)
        facets = d.get("facets") or {}
        owners = ((facets.get("ownership") or {}).get("owners") or [])
        owner = owners[0].get("name") if owners else None
        graph.add_node(
            Node(
                id=ds_id,
                type=NodeType.TABLE,
                name=d.get("name", ds_id),
                meta={"namespace": d.get("namespace"), "owner": owner, "source": "openlineage"},
            )
        )
        for field in ((facets.get("schema") or {}).get("fields") or []):
            col = field.get("name")
            if not col:
                continue
            col_id = f"column:{_bare(ds_id)}.{col.lower()}"
            graph.add_node(
                Node(id=col_id, type=NodeType.COLUMN, name=col.lower(), meta={"parent": ds_id, "data_type": field.get("type")})
            )
            graph.add_edge(Edge(src=ds_id, dst=col_id, type=EdgeType.CONTAINS))
        return ds_id


def _dataset_id(d: dict) -> str:
    return "table:" + str(d.get("name", "")).lower()


def _bare(node_id: str) -> str:
    return node_id.split(":", 1)[1]


def _read_events(path: Path) -> Iterable[dict]:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return [e for e in data if isinstance(e, dict)]
    if text.startswith("{"):
        try:
            single = json.loads(text)
            return [single] if isinstance(single, dict) else []
        except json.JSONDecodeError:
            pass
    events: List[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events
