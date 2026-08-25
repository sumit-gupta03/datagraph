"""Governance metadata: business glossary, domains, deprecation, ownership.

A catalog platform keeps this in a database behind a UI. datagraph keeps it in a file you
commit next to your code, and applies it to the graph so it shows up in impact analysis,
search, context packs and the wiki:

    # datagraph.yml  (YAML or JSON; --metadata datagraph.yml)
    version: 1
    glossary:
      - term: Customer PII
        definition: Personal data about an identified or identifiable customer.
        owner: privacy-office
        applies_to: ["column:dim_customer.email", "column:dim_customer.name"]
      - term: Net Revenue
        definition: Gross revenue minus refunds and discounts.
        applies_to: ["dbt:fact_booking", "column:fact_booking.amount"]
    domains:
      - name: Finance
        owner: finance
        description: Revenue and billing assets.
        assets: ["dbt:fact_booking", "table:prod.analytics.*"]      # * and ? wildcards
      - name: Growth
        assets: ["exposure:customer_dashboard"]
    deprecations:
      - asset: dbt:legacy_customer
        reason: Replaced by dim_customer on 2026-03-01.
        replacement: dbt:dim_customer
    owners:                                   # overrides / fills gaps
      "table:prod.raw.events": ingestion-team

Everything is optional. The same three concepts are also read straight from dbt: a model's
``meta.domain`` / ``config.meta.domain`` / ``group``, ``meta.terms``, and
``meta.deprecated`` / a ``deprecated`` tag.

Applied values live on the node: ``meta["domain"]``, ``meta["terms"]``, ``meta["deprecated"]``
(``{reason, replacement}``), ``meta["owner"]``.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Dict, List, Union

from .graph import ImpactGraph
from .security import sanitize_text


def load_metadata(path: Union[str, Path]) -> Dict:
    """Read a governance metadata file (YAML needs PyYAML; JSON always works)."""
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() in (".yml", ".yaml"):
        try:
            import yaml  # type: ignore
        except ImportError as e:  # pragma: no cover - message is the point
            raise ImportError("YAML metadata files need PyYAML: pip install datagraph-core[yaml]") from e
        doc = yaml.safe_load(text)
    else:
        doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError(f"{p} must contain a mapping (version / glossary / domains / ...)")
    return doc


def _match(graph: ImpactGraph, pattern: str) -> List[str]:
    """Resolve an asset reference: an exact id, a wildcard over ids, or a resolvable name."""
    if any(ch in pattern for ch in "*?["):
        return [n.id for n in graph.nodes() if fnmatch.fnmatchcase(n.id, pattern)]
    if pattern in graph:
        return [pattern]
    node = graph.resolve(pattern)
    return [node.id] if node else []


def apply_metadata(graph: ImpactGraph, doc: Dict) -> Dict[str, int]:
    """Attach glossary terms, domains, deprecations and owners to the graph.

    Returns counts of what was applied, and records unmatched references under
    ``"unmatched"`` so a typo in the file is visible instead of silent.
    """
    applied = {"terms": 0, "domains": 0, "deprecations": 0, "owners": 0, "unmatched": 0}
    unmatched: List[str] = []

    glossary: Dict[str, Dict] = {}
    for entry in doc.get("glossary") or []:
        term = sanitize_text(entry.get("term") or entry.get("name") or "", 120).strip()
        if not term:
            continue
        glossary[term] = {
            "term": term,
            "definition": sanitize_text(entry.get("definition") or entry.get("description") or "", 1000),
            "owner": entry.get("owner"),
            "group": entry.get("group") or entry.get("term_group"),
            "assets": [],
        }
        for ref in entry.get("applies_to") or entry.get("assets") or []:
            ids = _match(graph, str(ref))
            if not ids:
                unmatched.append(f"glossary '{term}' -> {ref}")
                continue
            for nid in ids:
                node = graph.get_node(nid)
                terms = node.meta.setdefault("terms", [])
                if term not in terms:
                    terms.append(term)
                    applied["terms"] += 1
                glossary[term]["assets"].append(nid)

    for entry in doc.get("domains") or doc.get("data_products") or []:
        name = sanitize_text(entry.get("name") or "", 120).strip()
        if not name:
            continue
        for ref in entry.get("assets") or entry.get("applies_to") or []:
            ids = _match(graph, str(ref))
            if not ids:
                unmatched.append(f"domain '{name}' -> {ref}")
                continue
            for nid in ids:
                node = graph.get_node(nid)
                node.meta["domain"] = name
                if entry.get("owner") and not node.owner:
                    node.meta["owner"] = entry["owner"]
                applied["domains"] += 1

    for entry in doc.get("deprecations") or []:
        ref = entry.get("asset") or entry.get("name")
        ids = _match(graph, str(ref)) if ref else []
        if not ids:
            unmatched.append(f"deprecation -> {ref}")
            continue
        for nid in ids:
            graph.get_node(nid).meta["deprecated"] = {
                "reason": sanitize_text(entry.get("reason") or "", 500),
                "replacement": entry.get("replacement"),
                "since": entry.get("since"),
            }
            applied["deprecations"] += 1

    for ref, owner in (doc.get("owners") or {}).items():
        ids = _match(graph, str(ref))
        if not ids:
            unmatched.append(f"owner -> {ref}")
            continue
        for nid in ids:
            graph.get_node(nid).meta["owner"] = owner
            applied["owners"] += 1

    if glossary:
        store = graph.meta.setdefault("glossary", {})
        store.update(glossary)
    applied["unmatched"] = len(unmatched)
    applied["unmatched_refs"] = unmatched[:20]  # type: ignore[assignment]
    return applied


def glossary_index(graph: ImpactGraph) -> Dict[str, Dict]:
    """Every term in use: definition (when a metadata file supplied one) and the assets carrying it."""
    index: Dict[str, Dict] = {}
    declared = (getattr(graph, "meta", None) or {}).get("glossary") or {}
    for name, entry in declared.items():
        index[name] = {"term": name, "definition": entry.get("definition", ""),
                       "owner": entry.get("owner"), "group": entry.get("group"), "assets": []}
    for node in graph.nodes():
        for term in node.meta.get("terms") or []:
            index.setdefault(term, {"term": term, "definition": "", "owner": None, "group": None, "assets": []})
            index[term]["assets"].append(node.id)
    for entry in index.values():
        entry["assets"] = sorted(set(entry["assets"]))
    return dict(sorted(index.items()))


def domains(graph: ImpactGraph) -> Dict[str, List[str]]:
    """domain name -> asset ids (only assets that carry a domain)."""
    out: Dict[str, List[str]] = {}
    for node in graph.nodes():
        d = node.meta.get("domain")
        if d:
            out.setdefault(str(d), []).append(node.id)
    return {k: sorted(v) for k, v in sorted(out.items())}


def deprecated_assets(graph: ImpactGraph) -> List[Dict]:
    """Deprecated assets and whether anything still depends on them."""
    rows = []
    for node in graph.nodes():
        dep = node.meta.get("deprecated")
        if not dep:
            continue
        users = [nid for nid in graph.impact(node.id, max_depth=None)
                 if not nid.startswith("column:") and nid != node.id]
        rows.append({"id": node.id, "name": node.name, "type": node.type.value,
                     "reason": (dep or {}).get("reason", "") if isinstance(dep, dict) else str(dep),
                     "replacement": (dep or {}).get("replacement") if isinstance(dep, dict) else None,
                     "still_used_by": users})
    return sorted(rows, key=lambda r: -len(r["still_used_by"]))


def deprecation_warnings(graph: ImpactGraph, affected: Dict[str, int]) -> List[str]:
    """Warnings for an impact analysis: deprecated assets in the blast radius."""
    out = []
    for nid in affected:
        node = graph.get_node(nid)
        if node is None or not node.meta.get("deprecated"):
            continue
        dep = node.meta["deprecated"]
        repl = dep.get("replacement") if isinstance(dep, dict) else None
        reason = dep.get("reason") if isinstance(dep, dict) else str(dep)
        out.append(f"'{node.name}' is deprecated"
                   + (f" - use {repl} instead" if repl else "")
                   + (f" ({reason})" if reason else ""))
    return out


def dbt_governance(node: dict) -> Dict:
    """Domain / terms / deprecation declared inside a dbt node's meta, config.meta, group or tags."""
    meta = node.get("meta") or {}
    config_meta = (node.get("config") or {}).get("meta") or {}
    tags = [str(t).lower() for t in (node.get("tags") or [])]
    out: Dict = {}
    domain = meta.get("domain") or config_meta.get("domain") or node.get("group")
    if isinstance(domain, dict):
        domain = domain.get("name")
    if domain:
        out["domain"] = str(domain)
    terms = meta.get("terms") or config_meta.get("terms") or meta.get("glossary_terms")
    if isinstance(terms, str):
        terms = [terms]
    if terms:
        out["terms"] = [str(t) for t in terms]
    deprecated = meta.get("deprecated", config_meta.get("deprecated"))
    if deprecated or "deprecated" in tags:
        if isinstance(deprecated, dict):
            out["deprecated"] = {"reason": deprecated.get("reason", ""), "replacement": deprecated.get("replacement")}
        else:
            out["deprecated"] = {"reason": "" if deprecated in (True, None) else str(deprecated), "replacement": None}
    return out
