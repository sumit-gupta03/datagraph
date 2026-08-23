"""Live DataHub importer over GraphQL.

Pulls datasets (name, platform, schema fields, owners) and their upstream
lineage — including fine-grained (column) lineage — straight from a running
DataHub instance, so datagraph inherits the enterprise lineage DataHub
already collected and adds the code side on top.

    DataHubExtractor("https://datahub.company.com", token=os.environ["DATAHUB_TOKEN"]).extract()

The HTTP transport is injectable (``transport=callable(query, variables) -> dict``)
so the extractor is testable offline; by default it POSTs to ``<server>/api/graphql``
with ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Callable, Dict, List, Optional

from ..graph import Edge, EdgeType, ImpactGraph, Node, NodeType
from .base import Extractor

_SEARCH_QUERY = """
query search($query: String!, $start: Int!, $count: Int!) {
  search(input: {type: DATASET, query: $query, start: $start, count: $count}) {
    total
    searchResults {
      entity {
        urn
        ... on Dataset {
          name
          platform { name }
          properties { name qualifiedName }
          schemaMetadata { fields { fieldPath type nativeDataType } }
          ownership { owners { owner { ... on CorpUser { username } ... on CorpGroup { name } } } }
          upstream: lineage(input: {direction: UPSTREAM, start: 0, count: 200}) {
            relationships { type entity { urn ... on Dataset { name } ... on DataJob { urn } } }
          }
          fineGrainedLineages { upstreams { urn path } downstreams { urn path } }
        }
      }
    }
  }
}
"""


class DataHubExtractor(Extractor):
    name = "datahub"

    def __init__(
        self,
        server: str,
        token: Optional[str] = None,
        query: str = "*",
        max_entities: int = 2000,
        page_size: int = 100,
        transport: Optional[Callable[[str, Dict], Dict]] = None,
    ) -> None:
        self.server = server.rstrip("/")
        self.token = token
        self.query = query
        self.max_entities = max_entities
        self.page_size = page_size
        self.transport = transport or self._http

    def _http(self, query: str, variables: Dict) -> Dict:
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        req = urllib.request.Request(self.server + "/api/graphql", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — user-supplied server
            return json.loads(resp.read().decode("utf-8"))

    def extract(self) -> ImpactGraph:
        graph = ImpactGraph()
        start = 0
        while start < self.max_entities:
            data = self.transport(_SEARCH_QUERY, {"query": self.query, "start": start, "count": self.page_size})
            if "errors" in data and not data.get("data"):
                raise RuntimeError(f"DataHub GraphQL error: {data['errors']}")
            search = ((data.get("data") or {}).get("search")) or {}
            results = search.get("searchResults") or []
            for r in results:
                self._dataset(graph, r.get("entity") or {})
            start += len(results)
            if not results or start >= int(search.get("total") or 0):
                break
        return graph

    def _dataset(self, graph: ImpactGraph, ds: Dict) -> Optional[str]:
        urn = ds.get("urn")
        if not urn:
            return None
        tid = _urn_to_id(urn, ds.get("name"))
        owners = [
            (o.get("owner") or {}).get("username") or (o.get("owner") or {}).get("name")
            for o in ((ds.get("ownership") or {}).get("owners") or [])
        ]
        graph.add_node(Node(
            id=tid, type=NodeType.TABLE, name=tid.split(":", 1)[1],
            meta={"urn": urn, "platform": ((ds.get("platform") or {}).get("name")), "owner": next((o for o in owners if o), None),
                  "source": "datahub"},
        ))
        for f in ((ds.get("schemaMetadata") or {}).get("fields") or []):
            fp = f.get("fieldPath")
            if fp:
                _column(graph, tid, fp, f.get("nativeDataType") or f.get("type"))
        for rel in ((ds.get("upstream") or {}).get("relationships") or []):
            ent = rel.get("entity") or {}
            up_urn = ent.get("urn")
            if not up_urn:
                continue
            if up_urn.startswith("urn:li:dataset:"):
                up_id = _urn_to_id(up_urn, ent.get("name"))
                graph.add_node(Node(id=up_id, type=NodeType.TABLE, name=up_id.split(":", 1)[1], meta={"urn": up_urn, "source": "datahub"}))
                graph.add_edge(Edge(src=tid, dst=up_id, type=EdgeType.DEPENDS_ON, meta={"via": "datahub"}))
            elif up_urn.startswith("urn:li:dataJob:"):
                job_id = "job:" + _job_name(up_urn)
                graph.add_node(Node(id=job_id, type=NodeType.DAG, name=_job_name(up_urn), meta={"urn": up_urn, "source": "datahub"}))
                graph.add_edge(Edge(src=job_id, dst=tid, type=EdgeType.WRITES_TO, meta={"via": "datahub"}))
        for fgl in ds.get("fineGrainedLineages") or []:
            downs = [(d.get("urn"), d.get("path")) for d in (fgl.get("downstreams") or [])]
            ups = [(u.get("urn"), u.get("path")) for u in (fgl.get("upstreams") or [])]
            for d_urn, d_path in downs:
                d_tid = _urn_to_id(d_urn) if d_urn and d_urn != urn else tid
                if not d_path:
                    continue
                d_col = _column(graph, d_tid, d_path, None)
                for u_urn, u_path in ups:
                    if not (u_urn and u_path):
                        continue
                    u_tid = _urn_to_id(u_urn)
                    graph.add_node(Node(id=u_tid, type=NodeType.TABLE, name=u_tid.split(":", 1)[1], meta={"urn": u_urn, "source": "datahub"}))
                    u_col = _column(graph, u_tid, u_path, None)
                    graph.add_edge(Edge(src=d_col, dst=u_col, type=EdgeType.DEPENDS_ON, meta={"via": "datahub"}))
        return tid


_URN_RE = re.compile(r"^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,([^,]+),[^)]+\)$")


def _urn_to_id(urn: str, name: Optional[str] = None) -> str:
    m = _URN_RE.match(urn or "")
    dataset_name = (m.group(1) if m else (name or urn)).strip()
    return "table:" + dataset_name.lower()


def _job_name(urn: str) -> str:
    m = re.search(r"urn:li:dataJob:\(urn:li:dataFlow:\([^,]+,([^,]+),[^)]+\),([^)]+)\)", urn)
    return f"{m.group(1)}/{m.group(2)}" if m else urn


def _column(graph: ImpactGraph, tid: str, field_path: str, dtype) -> str:
    col = field_path.split(".")[-1].lower() if "." in field_path and not field_path.startswith("[") else field_path.lower()
    col_id = f"column:{tid.split(':', 1)[1]}.{col}"
    graph.add_node(Node(id=col_id, type=NodeType.COLUMN, name=col, meta={"parent": tid, "data_type": dtype}))
    graph.add_edge(Edge(src=tid, dst=col_id, type=EdgeType.CONTAINS))
    return col_id
