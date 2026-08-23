"""LLM-assisted lineage — a clearly-labelled FALLBACK, never the primary source.

Deterministic extractors come first. When they cannot derive a relationship
(SQL that sqlglot cannot parse, stored procedures, dynamic SQL in code, tables
with no declared foreign keys but obvious naming conventions), ``suggest_lineage``
asks Claude for candidate relationships using **structured outputs**, and
``apply_suggestions`` adds them as edges tagged ``provenance: "llm"`` with a
confidence and a reason. Those edges are shown with an ``(llm)`` marker,
excluded by ``--no-inferred``, and can be reviewed in ``relationships --json``.

Providers: Anthropic (default), Amazon Bedrock (Nova / Claude on Bedrock / ...), or any
OpenAI-compatible endpoint - see ``datagraph.ai.providers``.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..security import UNTRUSTED_NOTICE, sanitize_text, wrap_untrusted
from .providers import extract_json, get_provider
from ..graph import LLM, Edge, EdgeType, ImpactGraph, Node, NodeType

SYSTEM_PROMPT = (
    "You are a careful data engineer. You are given a database schema (tables with their columns), "
    "the relationships that are ALREADY known from deterministic sources, and optionally SQL snippets "
    "that an automatic parser could not handle. Suggest ADDITIONAL likely relationships only: "
    "(a) column-to-column dependencies you can read from the SQL, and (b) foreign-key-like links "
    "implied by naming conventions (e.g. orders.customer_id -> customers.id). "
    "Use the exact node ids given. Never repeat a known relationship. Give a confidence between 0 and 1 "
    "and a one-sentence reason. If you are not reasonably confident, omit the relationship."
    + " " + UNTRUSTED_NOTICE
)

SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["table", "column"]},
                    "source": {"type": "string", "description": "node id that depends on / is derived from the target"},
                    "target": {"type": "string", "description": "node id that the source depends on"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["kind", "source", "target", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["relationships"],
    "additionalProperties": False,
}

_TABLE_TYPES = {NodeType.TABLE, NodeType.VIEW, NodeType.DBT_MODEL, NodeType.DBT_SOURCE, NodeType.DBT_SEED, NodeType.DBT_SNAPSHOT}


def schema_summary(graph: ImpactGraph, max_tables: Optional[int] = None) -> Dict:
    """Compact, deterministic description of the graph the model will reason about."""
    tables = []
    known = []
    by_id = {n.id: n for n in graph.nodes()}
    for n in sorted(graph.nodes(), key=lambda x: x.id):
        if n.type not in _TABLE_TYPES:
            continue
        cols = []
        for c in graph.nodes(NodeType.COLUMN):
            if c.meta.get("parent") != n.id:
                continue
            entry = {"id": c.id, "name": c.name, "type": c.meta.get("data_type")}
            prof = c.meta.get("profile") or {}
            if prof:  # distinct counts / ranges help the model spot join keys
                entry["profile"] = {k: prof.get(k) for k in ("distinct", "null_pct", "min", "max") if prof.get(k) is not None}
            cols.append(entry)
        cols.sort(key=lambda c: c["id"])
        t_entry = {"id": n.id, "type": n.type.value, "columns": cols}
        if (n.meta.get("profile") or {}).get("row_count") is not None:
            t_entry["row_count"] = n.meta["profile"]["row_count"]
        tables.append(t_entry)
    if max_tables is not None:
        tables = tables[:max_tables]
    for e in graph.edges():
        if e.type in (EdgeType.DEPENDS_ON, EdgeType.WRITES_TO):
            s, d = by_id.get(e.src), by_id.get(e.dst)
            if s and d and (s.type in _TABLE_TYPES or s.type == NodeType.COLUMN):
                known.append({"source": e.src, "target": e.dst, "via": e.meta.get("via", e.type.value)})
    return {"tables": tables, "known_relationships": sorted(known, key=lambda r: (r["source"], r["target"]))}


def suggest_lineage(
    graph: ImpactGraph,
    unparsed_sql: Optional[List[Dict]] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tables: Optional[int] = None,
    max_tokens: int = 16000,
    provider=None,
) -> List[Dict]:
    """Ask the configured LLM for candidate relationships. Returns a list of suggestion dicts
    (kind, source, target, confidence, reason) - nothing is added to the graph yet.
    ``provider``: LLMProvider instance or name ('anthropic' | 'bedrock' | 'openai'); default Anthropic."""
    llm = get_provider(provider, model=model, api_key=api_key)
    payload = schema_summary(graph, max_tables=max_tables)
    payload["unparsed_sql"] = [
        {"where": sanitize_text(u.get("where"), 300), "sql": sanitize_text(u.get("sql", ""), 4000)} for u in (unparsed_sql or [])
    ][:50]
    user = ("Schema, known relationships and unparsed SQL as JSON:\n\n"
            + wrap_untrusted(f"```json\n{json.dumps(payload, indent=2, sort_keys=True)}\n```")
            + "\n\nSuggest additional relationships.")
    text = llm.complete(SYSTEM_PROMPT, user, max_tokens=max_tokens, json_schema=SUGGESTION_SCHEMA)
    data = extract_json(text)
    if not isinstance(data, dict):
        return []
    out = []
    for r in data.get("relationships", []):
        try:
            out.append({"kind": r["kind"], "source": r["source"], "target": r["target"],
                        "confidence": float(r["confidence"]), "reason": str(r.get("reason", ""))})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def apply_suggestions(graph: ImpactGraph, suggestions: List[Dict], min_confidence: float = 0.6) -> int:
    """Add accepted suggestions as ``llm``-provenance DEPENDS_ON edges. Returns how many were added.

    Only nodes that already exist in the graph (or columns of existing tables) are
    linked — the model cannot invent tables."""
    added = 0
    for s in suggestions:
        if s.get("confidence", 0) < min_confidence:
            continue
        src, dst = s["source"], s["target"]
        if src == dst:
            continue
        for nid in (src, dst):
            if nid not in graph:
                if nid.startswith("column:") and "." in nid:
                    parent_name = nid[len("column:"):].rpartition(".")[0]
                    parent = next((n.id for n in graph.nodes() if n.id.split(":", 1)[-1] == parent_name), None)
                    if parent is None:
                        break
                    graph.add_node(Node(id=nid, type=NodeType.COLUMN, name=nid.rsplit(".", 1)[-1], meta={"parent": parent}))
                    graph.add_edge(Edge(src=parent, dst=nid, type=EdgeType.CONTAINS))
                else:
                    break
        else:
            before = len(graph.edges())
            graph.add_edge(Edge(src=src, dst=dst, type=EdgeType.DEPENDS_ON,
                                meta={"provenance": LLM, "via": "llm", "confidence": s.get("confidence"), "reason": s.get("reason", "")}))
            if len(graph.edges()) > before:
                added += 1
            if s.get("kind") == "column":
                # also link the owning tables so table-level views show the relationship
                sp, dp = graph.get_node(src), graph.get_node(dst)
                if sp and dp and sp.meta.get("parent") and dp.meta.get("parent") and sp.meta["parent"] != dp.meta["parent"]:
                    graph.add_edge(Edge(src=sp.meta["parent"], dst=dp.meta["parent"], type=EdgeType.DEPENDS_ON,
                                        meta={"provenance": LLM, "via": "llm", "confidence": s.get("confidence")}))
    return added
