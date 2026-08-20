"""Core node/edge data model for the unified impact graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class NodeType(str, Enum):
    FILE = "file"
    MODULE = "module"
    FUNCTION = "function"
    CLASS = "class"
    DBT_MODEL = "dbt_model"
    DBT_SOURCE = "dbt_source"
    DBT_SEED = "dbt_seed"
    DBT_SNAPSHOT = "dbt_snapshot"
    EXPOSURE = "exposure"
    TABLE = "table"
    VIEW = "view"
    COLUMN = "column"
    LAMBDA = "lambda"
    API = "api"
    DAG = "dag"
    REPORT = "report"
    DASHBOARD = "dashboard"


class EdgeType(str, Enum):
    """Semantic edge types.

    Direction convention (semantic, per type):
      CONTAINS    parent -> child        (file contains function)
      CALLS       caller -> callee
      IMPORTS     importer -> imported
      DEPENDS_ON  downstream -> upstream (dbt model depends on source)
      WRITES_TO   writer -> target       (model materializes table)
      EXPOSES     producer -> exposure   (model feeds a dashboard)
    """

    CONTAINS = "contains"
    CALLS = "calls"
    IMPORTS = "imports"
    DEPENDS_ON = "depends_on"
    WRITES_TO = "writes_to"
    EXPOSES = "exposes"


# For each edge type: does impact flow along the edge ("forward",
# src change affects dst) or against it ("reverse", dst change affects src)?
IMPACT_DIRECTION: Dict[EdgeType, str] = {
    EdgeType.CONTAINS: "forward",    # file changed -> its functions affected
    EdgeType.CALLS: "reverse",       # callee changed -> callers affected
    EdgeType.IMPORTS: "reverse",     # imported module changed -> importers affected
    EdgeType.DEPENDS_ON: "reverse",  # upstream changed -> dependents affected
    EdgeType.WRITES_TO: "forward",   # model changed -> its table affected
    EdgeType.EXPOSES: "forward",     # model changed -> dashboard affected
}


@dataclass
class Node:
    id: str
    type: NodeType
    name: str
    path: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "name": self.name,
            "path": self.path,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Node":
        return cls(
            id=d["id"],
            type=NodeType(d["type"]),
            name=d["name"],
            path=d.get("path"),
            meta=d.get("meta", {}),
        )


@dataclass
class Edge:
    src: str
    dst: str
    type: EdgeType
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "src": self.src,
            "dst": self.dst,
            "type": self.type.value,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Edge":
        return cls(
            src=d["src"],
            dst=d["dst"],
            type=EdgeType(d["type"]),
            meta=d.get("meta", {}),
        )
