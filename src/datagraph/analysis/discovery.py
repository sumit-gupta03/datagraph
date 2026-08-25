"""Discovery: search across everything, and a sensitive-data (PII) report.

``search`` is the local equivalent of a catalog's search box: it looks at ids, names, paths,
descriptions, column names, owners, tags, glossary terms and domains, ranks the hits, and can be
filtered the way a catalog facets (type / domain / tag / term / owner).

``pii_report`` answers the question a catalog answers with manual PII tagging: *where is personal
data, and what is exposed to it?* Columns are classified with the same heuristic that masks values
during profiling, and the graph then shows which dashboards, APIs and downstream tables inherit them.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..graph import ImpactGraph, NodeType
from ..security import is_sensitive_column

_EXPOSED_TYPES = {NodeType.DASHBOARD, NodeType.REPORT, NodeType.EXPOSURE, NodeType.API, NodeType.LAMBDA}


def _haystack(node) -> Dict[str, str]:
    meta = node.meta or {}
    return {
        "id": node.id.lower(),
        "name": (node.name or "").lower(),
        "path": (node.path or "").lower(),
        "description": str(meta.get("description") or "").lower(),
        "owner": str(node.owner or "").lower(),
        "domain": str(meta.get("domain") or "").lower(),
        "tags": " ".join(str(t).lower() for t in (meta.get("tags") or [])),
        "terms": " ".join(str(t).lower() for t in (meta.get("terms") or [])),
    }


def search(
    graph: ImpactGraph,
    query: str = "",
    node_type: Optional[str] = None,
    domain: Optional[str] = None,
    tag: Optional[str] = None,
    term: Optional[str] = None,
    owner: Optional[str] = None,
    include_columns: bool = False,
    limit: int = 50,
) -> List[Dict]:
    """Rank nodes against a free-text query plus catalog-style filters.

    Scoring (highest first): exact name, name prefix, name substring, id substring,
    a matching column name, description / tag / term / owner / domain / path match.
    An empty query with filters lists everything matching the filters.
    """
    q = (query or "").strip().lower()
    columns_by_parent: Dict[str, List[str]] = {}
    for col in graph.nodes(NodeType.COLUMN):
        parent = col.meta.get("parent")
        if parent:
            columns_by_parent.setdefault(parent, []).append(col.name.lower())

    rows: List[Dict] = []
    for node in graph.nodes():
        if node.type == NodeType.COLUMN and not include_columns:
            continue
        hay = _haystack(node)
        if node_type and node.type.value != node_type:
            continue
        if domain and hay["domain"] != domain.lower():
            continue
        if tag and tag.lower() not in hay["tags"].split():
            continue
        if term and term.lower() not in hay["terms"]:
            continue
        if owner and owner.lower() not in hay["owner"]:
            continue

        cols = columns_by_parent.get(node.id, [])
        score, why = 0, ""
        if not q:
            score, why = 1, "filter"
        elif hay["name"] == q:
            score, why = 100, "name"
        elif hay["name"].startswith(q):
            score, why = 80, "name"
        elif q in hay["name"]:
            score, why = 60, "name"
        elif q in hay["id"]:
            score, why = 50, "id"
        elif any(q in c for c in cols):
            score, why = 40, "column"
        elif q in hay["description"]:
            score, why = 30, "description"
        elif q in hay["terms"]:
            score, why = 28, "glossary term"
        elif q in hay["tags"]:
            score, why = 26, "tag"
        elif q in hay["domain"]:
            score, why = 24, "domain"
        elif q in hay["owner"]:
            score, why = 22, "owner"
        elif q in hay["path"]:
            score, why = 20, "path"
        if not score:
            continue

        rows.append({
            "id": node.id,
            "name": node.name,
            "type": node.type.value,
            "owner": node.owner,
            "domain": node.meta.get("domain"),
            "terms": node.meta.get("terms") or [],
            "tags": node.meta.get("tags") or [],
            "description": (node.meta.get("description") or "")[:160],
            "deprecated": bool(node.meta.get("deprecated")),
            "columns": len(cols),
            "matched_on": why,
            "score": score,
        })

    rows.sort(key=lambda r: (-r["score"], r["id"]))
    return rows[:limit]


def pii_report(graph: ImpactGraph, include_inferred: bool = True) -> Dict:
    """Where personal data lives, and everything downstream that is exposed to it.

    Returns tables holding sensitive columns, the exposures (dashboards / APIs / reports) that
    inherit them, and the columns whose values were masked during profiling.
    """
    tables: Dict[str, Dict] = {}
    for col in graph.nodes(NodeType.COLUMN):
        if not is_sensitive_column(col.name):
            continue
        parent = col.meta.get("parent")
        if not parent or parent not in graph:
            continue
        node = graph.get_node(parent)
        entry = tables.setdefault(parent, {
            "id": parent, "name": node.name, "type": node.type.value,
            "owner": node.owner, "domain": node.meta.get("domain"),
            "columns": [], "masked_in_profile": [], "exposed_to": [], "downstream": 0,
        })
        entry["columns"].append(col.name)
        if (col.meta.get("profile") or {}).get("masked"):
            entry["masked_in_profile"].append(col.name)

    for tid, entry in tables.items():
        downstream = graph.impact(tid, include_inferred=include_inferred)
        entry["downstream"] = len([k for k in downstream if not k.startswith("column:")])
        exposed = []
        for nid in downstream:
            node = graph.get_node(nid)
            if node is not None and node.type in _EXPOSED_TYPES:
                exposed.append({"id": nid, "name": node.name, "type": node.type.value, "owner": node.owner})
        entry["exposed_to"] = sorted(exposed, key=lambda e: e["id"])
        entry["columns"] = sorted(set(entry["columns"]))
        entry["masked_in_profile"] = sorted(set(entry["masked_in_profile"]))

    rows = sorted(tables.values(), key=lambda r: (-len(r["exposed_to"]), -r["downstream"], r["id"]))
    return {
        "tables": rows,
        "sensitive_columns": sum(len(r["columns"]) for r in rows),
        "exposures": sorted({e["id"] for r in rows for e in r["exposed_to"]}),
        "unowned": [r["id"] for r in rows if not r["owner"]],
        "note": ("Classification is a name-based heuristic (the same one that masks values while "
                 "profiling) - review it before treating it as a compliance record."),
    }
