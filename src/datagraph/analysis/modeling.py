"""Dimensional modelling from the graph - deterministic, explainable.

* ``classify_tables(graph)``     - fact / dimension / bridge / lookup per table-like node, with
                                   confidence and the reasons (column roles, keys, names, profile).
* ``star_schema(graph)``         - the star(s): facts with grain, measures and dimensions; dimensions
                                   with keys and attributes; conformed dimensions; snowflake chains;
                                   a list of issues worth fixing.
* ``propose_from_table(graph, t)`` - split one wide/flat table into a proposed fact + dimensions
                                   (uses profiles when present: low-cardinality columns become
                                   dimension attributes, numeric columns measures, dates the grain).
* ``to_mermaid(model)`` / ``to_markdown(model)`` - ER diagram and a readable report.

Foreign keys come from the warehouse (``extracted``); when a schema has none, links are inferred
from names (``orders.customer_id`` -> ``customers``), tagged ``inferred`` so you know to verify.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..graph import EdgeType, ImpactGraph, Node, NodeType

TABLE_TYPES = {NodeType.TABLE, NodeType.VIEW, NodeType.DBT_MODEL, NodeType.DBT_SOURCE, NodeType.DBT_SEED, NodeType.DBT_SNAPSHOT}

_MEASURE_WORDS = ("amount", "total", "qty", "quantity", "count", "price", "revenue", "cost", "value", "sum", "num_", "number_of",
                  "score", "rate", "balance", "fee", "tax", "discount", "weight", "duration", "sales", "profit", "margin",
                  "lifetime", "spend", "units", "volume", "hours", "minutes", "seconds", "points", "clicks", "views", "impressions")
_DATE_WORDS = ("date", "_at", "_ts", "timestamp", "_time", "_day", "_month", "_year", "period", "created", "updated", "modified")
_FLAG_PREFIX = ("is_", "has_", "was_", "can_", "should_", "flag")
_FACT_NAMES = ("fact", "fct_", "order", "transaction", "event", "payment", "sale", "booking", "shipment", "invoice", "click",
               "session", "log", "activity", "visit", "purchase", "trip", "ride", "claim", "line_item", "item")
_DIM_NAMES = ("dim", "customer", "product", "user", "account", "employee", "store", "location", "region", "country", "date",
              "calendar", "category", "supplier", "vendor", "channel", "campaign", "department", "plan", "status", "type", "currency")
_NUMERIC = ("int", "num", "dec", "float", "double", "real", "money", "serial")
_DATEY = ("date", "time")
_TEXTY = ("char", "text", "string", "clob", "enum")
_BOOLY = ("bool", "bit")


def _base(name: str) -> str:
    n = name.split(":", 1)[-1].split(".")[-1].lower()
    return n


def _singular(w: str) -> str:
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith("ses") or w.endswith("xes"):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _strip_prefix(w: str) -> str:
    for p in ("dim_", "fact_", "fct_", "stg_", "raw_", "src_", "int_", "mart_", "tbl_", "t_", "d_", "f_"):
        if w.startswith(p):
            return w[len(p):]
    return w


def _dtype(col: Node) -> str:
    return str(col.meta.get("data_type") or col.meta.get("type") or "").lower()


def _columns(graph: ImpactGraph, tid: str) -> List[Node]:
    return sorted([c for c in graph.nodes(NodeType.COLUMN) if c.meta.get("parent") == tid], key=lambda c: c.name)


def classify_column(col: Node, table_base: str, row_count: Optional[int] = None) -> str:
    """pk | fk | date | measure | flag | attribute"""
    n = col.name.lower()
    t = _dtype(col)
    prof = col.meta.get("profile") or {}
    distinct = prof.get("distinct")
    if col.meta.get("primary_key"):
        return "pk"
    if n in ("id", "pk", f"{table_base}_id", f"{_singular(table_base)}_id", f"{table_base}_key", f"{_singular(table_base)}_key",
             f"{_singular(table_base)}_sk", f"{table_base}_sk"):
        return "pk"
    if n.endswith(("_id", "_key", "_sk", "_fk", "_code", "_number", "_no")) or n in ("sk", "key"):
        if distinct is not None and row_count and distinct >= row_count and row_count > 1:
            return "pk"
        return "fk" if not n.endswith(("_code", "_number", "_no")) else "attribute"
    if any(t.startswith(b) for b in _BOOLY) or n.startswith(_FLAG_PREFIX):
        return "flag"
    if any(w in t for w in _DATEY) or any(n.endswith(w) or n.startswith(w) or n == w.strip("_") for w in _DATE_WORDS):
        return "date"
    if any(w in t for w in _NUMERIC) and not any(w in t for w in _BOOLY):
        return "measure" if any(w in n for w in _MEASURE_WORDS) or n.endswith(("_usd", "_eur", "_inr", "_pct", "_percent")) \
            else ("attribute" if distinct is not None and distinct <= 50 else "measure")
    if not t and any(w in n for w in _MEASURE_WORDS):
        return "measure"
    return "attribute"


def fk_links(graph: ImpactGraph, include_inferred: bool = True) -> List[Dict]:
    """Table-to-table key links: extracted foreign keys plus (optionally) name-inferred ones."""
    links: List[Dict] = []
    seen = set()
    tables = [n for n in graph.nodes() if n.type in TABLE_TYPES]
    for e in graph.edges():
        if e.type == EdgeType.DEPENDS_ON and e.meta.get("via") == "foreign_key":
            s, d = graph.get_node(e.src), graph.get_node(e.dst)
            if s and d and s.type == NodeType.COLUMN and d.type == NodeType.COLUMN:
                key = (s.meta.get("parent"), s.name, d.meta.get("parent"))
                if key in seen:
                    continue
                seen.add(key)
                links.append({"from_table": s.meta.get("parent"), "from_column": s.name, "to_table": d.meta.get("parent"),
                              "to_column": d.name, "provenance": "extracted"})
    if not include_inferred:
        return links
    # name inference: <x>_id in T  ->  table whose base name is x / xs / dim_x ... having x_id or id
    by_base: Dict[str, List[Node]] = {}
    for t in tables:
        b = _strip_prefix(_base(t.id))
        by_base.setdefault(b, []).append(t)
        by_base.setdefault(_singular(b), []).append(t)
    for t in tables:
        tb = _strip_prefix(_base(t.id))
        for c in _columns(graph, t.id):
            n = c.name.lower()
            m = re.match(r"^(.*?)_(id|key|sk|fk)$", n)
            if not m:
                continue
            ref = m.group(1)
            if ref in (tb, _singular(tb)):
                continue  # own key
            cands = [x for x in by_base.get(ref, []) + by_base.get(_singular(ref), []) if x.id != t.id]
            if not cands:
                continue
            # prefer a table that actually has the same column (or an 'id' column)
            best = None
            for x in cands:
                names = {cc.name for cc in _columns(graph, x.id)}
                if n in names or "id" in names or f"{ref}_id" in names:
                    best = x
                    break
            if best is None:
                best = cands[0]
            key = (t.id, n, best.id)
            if key in seen:
                continue
            seen.add(key)
            links.append({"from_table": t.id, "from_column": n, "to_table": best.id,
                          "to_column": n if n in {cc.name for cc in _columns(graph, best.id)} else "id", "provenance": "inferred"})
    return links


def classify_tables(graph: ImpactGraph, include_inferred: bool = True) -> Dict[str, Dict]:
    links = fk_links(graph, include_inferred)
    out_links: Dict[str, List[Dict]] = {}
    in_links: Dict[str, List[Dict]] = {}
    for l in links:
        out_links.setdefault(l["from_table"], []).append(l)
        in_links.setdefault(l["to_table"], []).append(l)
    result: Dict[str, Dict] = {}
    for t in graph.nodes():
        if t.type not in TABLE_TYPES:
            continue
        cols = _columns(graph, t.id)
        base = _strip_prefix(_base(t.id))
        rows = (t.meta.get("profile") or {}).get("row_count")
        if not cols:
            result[t.id] = {"id": t.id, "name": t.name, "type": t.type.value, "role": "unknown", "confidence": 0.0,
                            "reasons": ["no columns known (add a catalog / warehouse connection)"], "columns": {},
                            "counts": {}, "row_count": rows, "fk_out": out_links.get(t.id, []), "fk_in": in_links.get(t.id, [])}
            continue
        roles = {c.name: classify_column(c, base, rows) for c in cols}
        counts = {r: sum(1 for v in roles.values() if v == r) for r in ("pk", "fk", "date", "measure", "flag", "attribute")}
        n_out = len(out_links.get(t.id, []))
        n_in = len(in_links.get(t.id, []))
        reasons: List[str] = []
        fact = 0.0
        dim = 0.0
        if n_out:
            fact += 2.0 * n_out
            reasons.append(f"{n_out} foreign key(s) to other tables")
        if n_in:
            dim += 2.0 * n_in
            reasons.append(f"referenced by {n_in} table(s)")
        if counts["measure"]:
            fact += 1.5 * counts["measure"]
            reasons.append(f"{counts['measure']} measure-like column(s)")
        if counts["date"]:
            fact += 1.0
        if counts["attribute"]:
            dim += 0.75 * counts["attribute"]
        if counts["pk"]:
            dim += 1.0
        if counts["fk"] and not n_out:
            fact += 1.0 * counts["fk"]
            reasons.append(f"{counts['fk']} key-like column(s)")
        lname = _base(t.id)
        if any(w in lname for w in _FACT_NAMES):
            fact += 2.0
            reasons.append(f"name '{lname}' looks like a fact")
        if any(lname.startswith(w) or lname == w or lname.endswith("_" + w) for w in _DIM_NAMES) or lname.startswith("dim"):
            dim += 2.0
            reasons.append(f"name '{lname}' looks like a dimension")
        if rows is not None:
            reasons.append(f"{rows} rows")
        role = "unknown"
        bridge = (n_out >= 2 or counts["fk"] >= 2) and counts["measure"] == 0 and counts["attribute"] == 0 and counts["date"] == 0
        if t.type == NodeType.VIEW and not n_out and not n_in:
            # a view with no key links is a derived/aggregate object, not a base fact or dimension
            result[t.id] = {"id": t.id, "name": t.name, "type": t.type.value, "role": "derived", "confidence": 0.7,
                            "reasons": ["view without key links - derived / aggregate object (report layer)"] + reasons,
                            "columns": roles, "counts": counts, "row_count": rows, "fk_out": [], "fk_in": []}
            continue
        if bridge:
            role, conf = "bridge", 0.8
        elif fact == 0 and dim == 0:
            conf = 0.0
        elif fact > dim:
            role, conf = "fact", min(1.0, round((fact - dim) / max(fact, 1.0) * 0.6 + 0.4, 2))
        elif dim > fact:
            role, conf = "dimension", min(1.0, round((dim - fact) / max(dim, 1.0) * 0.6 + 0.4, 2))
        else:
            role, conf = ("lookup" if counts["attribute"] and not counts["measure"] else "unknown"), 0.3
        if role == "dimension" and counts["measure"] >= 2 and n_in == 0:
            role = "fact" if fact >= dim * 0.8 else role
        result[t.id] = {
            "id": t.id, "name": t.name, "type": t.type.value, "role": role, "confidence": conf,
            "reasons": reasons, "columns": roles, "counts": counts, "row_count": rows,
            "fk_out": out_links.get(t.id, []), "fk_in": in_links.get(t.id, []),
        }
    return result


def star_schema(graph: ImpactGraph, include_inferred: bool = True) -> Dict:
    cls = classify_tables(graph, include_inferred)
    facts: List[Dict] = []
    dims: Dict[str, Dict] = {}
    issues: List[str] = []
    for tid, c in cls.items():
        if c["role"] == "dimension" or c["role"] == "lookup":
            pk = [k for k, r in c["columns"].items() if r == "pk"]
            dims[tid] = {"id": tid, "name": c["name"], "key": pk[0] if pk else None,
                         "attributes": [k for k, r in c["columns"].items() if r in ("attribute", "flag", "date")],
                         "used_by": [], "confidence": c["confidence"], "snowflake_to": [l["to_table"] for l in c["fk_out"]]}
            if not pk:
                issues.append(f"dimension {tid} has no recognisable primary key column")
    for tid, c in cls.items():
        if c["role"] not in ("fact", "bridge"):
            continue
        dim_refs = []
        for l in c["fk_out"]:
            dim_refs.append({"table": l["to_table"], "via": l["from_column"], "provenance": l["provenance"]})
            if l["to_table"] in dims:
                dims[l["to_table"]]["used_by"].append(tid)
            elif cls.get(l["to_table"], {}).get("role") == "fact":
                issues.append(f"{tid}.{l['from_column']} points at another fact ({l['to_table']}) - fact-to-fact link, consider a shared dimension")
        orphan_keys = [k for k, r in c["columns"].items() if r == "fk" and k not in {d["via"] for d in dim_refs}]
        dates = [k for k, r in c["columns"].items() if r == "date"]
        measures = [k for k, r in c["columns"].items() if r == "measure"]
        degenerate = [k for k, r in c["columns"].items() if r == "attribute"]
        grain = (dates[:1] + [d["via"] for d in dim_refs])
        facts.append({"id": tid, "name": c["name"], "role": c["role"], "confidence": c["confidence"], "grain": grain,
                      "measures": measures, "dates": dates, "dimensions": dim_refs, "unresolved_keys": orphan_keys,
                      "degenerate_dimensions": degenerate, "row_count": c["row_count"]})
        date_dim_keys = [d["via"] for d in dim_refs if _base(d["table"]) in ("dim_date", "dim_calendar", "date_dim", "dates", "calendar", "dim_time", "dim_datetime")]
        if date_dim_keys:
            grain = date_dim_keys[:1] + [g for g in grain if g not in date_dim_keys[:1]]
            facts[-1]["grain"] = grain
        if not dates and not date_dim_keys:
            issues.append(f"fact {tid} has no date/time column - no time grain for trending")
        if not dim_refs and not orphan_keys:
            issues.append(f"fact {tid} links to no dimension")
        for k in orphan_keys:
            issues.append(f"{tid}.{k} looks like a foreign key but no matching dimension table was found")
        if not measures and c["role"] == "fact":
            issues.append(f"fact {tid} has no numeric measure - factless fact (fine for events/coverage) or misclassified")
        for l in c["fk_out"]:
            if l["provenance"] == "inferred":
                issues.append(f"{tid}.{l['from_column']} -> {l['to_table']} is inferred from names - verify or declare a foreign key")
        prof_cols = {k: (graph.get_node(f"column:{tid.split(':',1)[1]}.{k}") or Node(id="x", type=NodeType.COLUMN, name=k)).meta.get("profile") for k in c["columns"]}
        for k, p in prof_cols.items():
            if p and c["columns"][k] == "fk" and (p.get("null_pct") or 0) > 20:
                issues.append(f"{tid}.{k} is a key with {p['null_pct']}% nulls - late-arriving dimension or data quality issue")
    for tid, d in dims.items():
        if not d["used_by"]:
            issues.append(f"dimension {tid} is not referenced by any fact")
        if d["snowflake_to"]:
            issues.append(f"dimension {tid} links to {', '.join(d['snowflake_to'])} - snowflaked; consider flattening into one dimension")
        m = [k for k, r in cls[tid]["columns"].items() if r == "measure"]
        if m:
            issues.append(f"dimension {tid} carries measure-like columns ({', '.join(m[:4])}) - move to a fact or keep as attributes deliberately")
    conformed = [tid for tid, d in dims.items() if len(set(d["used_by"])) >= 2]
    # --- Kimball standard views: bus matrix, four-step design per fact, SCD + surrogate-key hints
    bus = {f["id"]: sorted({d["table"] for d in f["dimensions"]}) for f in facts}
    for f in facts:
        f["kimball"] = {
            "1_business_process": _base(f["id"]).replace("fact_", "").replace("fct_", ""),
            "2_grain": ("one row per " + " x ".join(f["grain"])) if f["grain"] else "undeclared - add a date and the dimension keys",
            "3_dimensions": [d["table"] for d in f["dimensions"]],
            "4_facts": f["measures"],
            "additivity": {m: ("semi-additive" if any(w in m for w in ("balance", "inventory", "stock", "level", "headcount", "snapshot")) else "additive") for m in f["measures"]},
        }
    scd = {}
    for tid, d in dims.items():
        roles = cls[tid]["columns"]
        names = set(roles)
        has_history = bool(names & {"valid_from", "valid_to", "effective_from", "effective_to", "start_date", "end_date", "is_current", "current_flag", "row_effective_date", "row_expiration_date"})
        has_updated = any(n in names for n in ("updated_at", "modified_at", "last_modified", "last_updated", "updated_date"))
        key = d["key"]
        is_date_dim = _base(tid) in ("dim_date", "dim_calendar", "date_dim", "dates", "calendar", "dim_time", "dim_datetime")
        key_col = next((c for c in _columns(graph, tid) if c.name == key), None) if key else None
        key_type = _dtype(key_col) if key_col else ""
        surrogate_ok = bool(key) and (not key_type or any(w in key_type for w in _NUMERIC)) and not (key or "").endswith(("_code", "_number"))
        scd[tid] = {
            "scd_type": "static" if is_date_dim else (2 if has_history else (1 if has_updated else "undecided")),
            "history_columns": sorted(names & {"valid_from", "valid_to", "effective_from", "effective_to", "start_date", "end_date", "is_current", "current_flag"}),
            "surrogate_key": key if surrogate_ok else None,
            "recommendation": ("static dimension (date/calendar) - no SCD needed" if is_date_dim else
                               "SCD type 2 in place (history columns present)" if has_history else
                               ("updated_at present but no history columns - decide SCD type 1 (overwrite) or add valid_from/valid_to/is_current for type 2" if has_updated else
                                "no change tracking - SCD type 1 assumed; add valid_from/valid_to/is_current if history matters")),
        }
        d["scd"] = scd[tid]
        if not surrogate_ok and key:
            issues.append(f"dimension {tid}: key '{key}' is a natural/text key - standard practice is an integer surrogate key plus the natural key as an attribute")
        if not is_date_dim and not any(n in names for n in ("updated_at", "modified_at", "valid_from", "effective_from", "is_current", "current_flag")) and d["attributes"]:
            issues.append(f"dimension {tid}: no change-tracking columns - decide SCD type (1 overwrite / 2 history)")
    has_date_dim = any(_base(tid) in ("dim_date", "dim_calendar", "date_dim", "dates", "calendar", "dim_time") for tid in dims)
    if facts and not has_date_dim:
        issues.append("no date dimension found - standard practice is a conformed dim_date joined by a date key from every fact")
    unknown = [tid for tid, c in cls.items() if c["role"] == "unknown"]
    if unknown:
        issues.append(f"{len(unknown)} table(s) could not be classified (no columns / keys known): " + ", ".join(unknown[:6]))
    return {"facts": facts, "dimensions": list(dims.values()), "conformed_dimensions": conformed, "bus_matrix": bus,
            "scd": scd, "standard": "Kimball dimensional modelling (business process -> grain -> dimensions -> facts; conformed dimensions via the bus matrix; SCD types)",
            "classification": cls, "issues": issues, "include_inferred": include_inferred}


def propose_from_table(graph: ImpactGraph, table: str, max_attr_distinct: int = 200) -> Dict:
    """Propose a star schema from one wide/denormalised table."""
    t = graph.resolve(table) or graph.get_node(table)
    if t is None:
        raise KeyError(table)
    cols = _columns(graph, t.id)
    rows = (t.meta.get("profile") or {}).get("row_count")
    base = _strip_prefix(_base(t.id))
    roles = {c.name: classify_column(c, base, rows) for c in cols}
    measures = [n for n, r in roles.items() if r == "measure"]
    dates = [n for n, r in roles.items() if r == "date"]
    keys = [n for n, r in roles.items() if r in ("pk", "fk")]
    attrs = [n for n, r in roles.items() if r in ("attribute", "flag")]
    # group attributes by prefix (customer_name, customer_email -> dim_customer); keys by their entity
    groups: Dict[str, List[str]] = {}
    for n in attrs + [k for k in keys if roles[k] == "fk"]:
        col = next(c for c in cols if c.name == n)
        prof = col.meta.get("profile") or {}
        if roles[n] == "attribute" and prof.get("distinct") and rows and prof["distinct"] > max_attr_distinct and prof["distinct"] > rows * 0.5:
            groups.setdefault("__degenerate__", []).append(n)  # near-unique text: degenerate dimension on the fact
            continue
        m = re.match(r"^([a-z]+?)_(id|key|sk|fk)$", n) or re.match(r"^([a-z]+?)_[a-z0-9_]+$", n)
        ent = m.group(1) if m else n
        groups.setdefault(ent, []).append(n)
    dims = []
    for ent, members in groups.items():
        if ent == "__degenerate__":
            continue
        dims.append({"name": f"dim_{ent}", "key": f"{ent}_key", "source_columns": members,
                     "natural_key": next((m for m in members if roles.get(m) == "fk"), None)})
    if dates:
        dims.append({"name": "dim_date", "key": "date_key", "source_columns": dates, "natural_key": dates[0]})
    fact_name = f"fact_{base}" if not base.startswith(("fact", "fct")) else base
    fact = {"name": fact_name, "grain": (dates[:1] + [d["key"] for d in dims if d["name"] != "dim_date"]),
            "measures": measures, "foreign_keys": [d["key"] for d in dims],
            "degenerate_dimensions": groups.get("__degenerate__", []), "source": t.id, "row_count": rows}
    notes = []
    if not measures:
        notes.append("no numeric measures detected - this may be an event/factless fact")
    if not dates:
        notes.append("no date column - add one to give the fact a time grain")
    if not dims:
        notes.append("no low-cardinality attributes found to form dimensions")
    return {"source": t.id, "fact": fact, "dimensions": dims, "column_roles": roles, "notes": notes}


# --------------------------------------------------------------- rendering


def _ent(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name.split(":", 1)[-1])


def to_mermaid(model: Dict) -> str:
    lines = ["erDiagram"]
    if "fact" in model and "dimensions" in model and "facts" not in model:  # proposal
        f = model["fact"]
        lines.append(f"  {_ent(f['name'])} {{")
        for k in f["foreign_keys"]:
            lines.append(f"    int {k} FK")
        for m in f["measures"]:
            lines.append(f"    numeric {m}")
        for d in f["degenerate_dimensions"]:
            lines.append(f"    string {d}")
        lines.append("  }")
        for d in model["dimensions"]:
            lines.append(f"  {_ent(d['name'])} {{")
            lines.append(f"    int {d['key']} PK")
            for c in d["source_columns"]:
                lines.append(f"    string {c}")
            lines.append("  }")
            lines.append(f"  {_ent(f['name'])} }}o--|| {_ent(d['name'])} : {d['key']}")
        return "\n".join(lines) + "\n"
    cls = model.get("classification", {})
    shown = set()
    for f in model["facts"]:
        shown.add(f["id"])
        lines.append(f"  {_ent(f['id'])} {{")
        for k, r in cls[f["id"]]["columns"].items():
            if r in ("pk", "fk"):
                lines.append(f"    key {k} {'PK' if r == 'pk' else 'FK'}")
            elif r == "measure":
                lines.append(f"    numeric {k}")
            elif r == "date":
                lines.append(f"    date {k}")
        lines.append("  }")
    for d in model["dimensions"]:
        shown.add(d["id"])
        lines.append(f"  {_ent(d['id'])} {{")
        if d["key"]:
            lines.append(f"    key {d['key']} PK")
        for a in d["attributes"][:12]:
            lines.append(f"    string {a}")
        lines.append("  }")
    for f in model["facts"]:
        for d in f["dimensions"]:
            if d["table"] in shown:
                lbl = d["via"] + (" (inferred)" if d["provenance"] == "inferred" else "")
                lines.append(f"  {_ent(f['id'])} }}o--|| {_ent(d['table'])} : \"{lbl}\"")
    for d in model["dimensions"]:
        for s in d["snowflake_to"]:
            if s in shown:
                lines.append(f"  {_ent(d['id'])} }}o--|| {_ent(s)} : snowflake")
    return "\n".join(lines) + "\n"


def to_markdown(model: Dict, title: str = "Dimensional model") -> str:
    out = [f"# {title}", ""]
    if "facts" not in model:  # proposal
        f = model["fact"]
        out.append(f"Proposed star schema from `{model['source']}`" + (f" ({f['row_count']} rows)" if f.get("row_count") is not None else ""))
        out.append("")
        out.append(f"## Fact: `{f['name']}`")
        out.append(f"- grain: {', '.join(f['grain']) or 'one row per source row'}")
        out.append(f"- measures: {', '.join(f['measures']) or '-'}")
        out.append(f"- foreign keys: {', '.join(f['foreign_keys']) or '-'}")
        if f["degenerate_dimensions"]:
            out.append(f"- degenerate dimensions (kept on the fact): {', '.join(f['degenerate_dimensions'])}")
        out.append("")
        out.append("## Dimensions")
        for d in model["dimensions"]:
            out.append(f"- `{d['name']}` (key `{d['key']}`" + (f", natural key `{d['natural_key']}`" if d.get("natural_key") else "") + f"): {', '.join(d['source_columns'])}")
        if model["notes"]:
            out.append("")
            out.append("## Notes")
            out += [f"- {n}" for n in model["notes"]]
        out += ["", "```mermaid", to_mermaid(model).rstrip(), "```", ""]
        return "\n".join(out)
    out.append(f"{len(model['facts'])} fact(s), {len(model['dimensions'])} dimension(s), {len(model['conformed_dimensions'])} conformed - "
               + model.get("standard", "Kimball dimensional modelling"))
    out.append("")
    dim_ids = [d["id"] for d in model["dimensions"]]
    if model["facts"] and dim_ids:
        out.append("## Bus matrix (facts x dimensions)")
        out.append("| fact | " + " | ".join(_base(d) for d in dim_ids) + " |")
        out.append("|---|" + "---|" * len(dim_ids))
        for f in model["facts"]:
            used = set(model.get("bus_matrix", {}).get(f["id"], []))
            out.append(f"| {_base(f['id'])} | " + " | ".join("X" if d in used else "" for d in dim_ids) + " |")
        out.append("")
    out.append("## Facts")
    for f in model["facts"]:
        out.append(f"### `{f['id']}` - {f['role']} (confidence {f['confidence']})")
        out.append(f"- grain: {', '.join(f['grain']) or 'unknown'}")
        out.append(f"- measures: {', '.join(f['measures']) or '-'}")
        out.append("- dimensions: " + (", ".join(f"{d['table']} via `{d['via']}`" + (" *(inferred)*" if d['provenance'] == 'inferred' else "") for d in f["dimensions"]) or "-"))
        if f["degenerate_dimensions"]:
            out.append(f"- degenerate dimensions: {', '.join(f['degenerate_dimensions'][:10])}")
        if f["unresolved_keys"]:
            out.append(f"- unresolved keys: {', '.join(f['unresolved_keys'])}")
        k = f.get("kimball")
        if k:
            out.append(f"- Kimball: process `{k['1_business_process']}` -> grain: {k['2_grain']} -> {len(k['3_dimensions'])} dimension(s) -> {len(k['4_facts'])} fact measure(s)"
                       + (" (" + ", ".join(f"{m} {a}" for m, a in k["additivity"].items() if a != "additive") + ")" if any(a != "additive" for a in k["additivity"].values()) else ""))
        out.append("")
    out.append("## Dimensions")
    for d in model["dimensions"]:
        conf = " **conformed**" if d["id"] in model["conformed_dimensions"] else ""
        scd = d.get("scd") or {}
        out.append(f"- `{d['id']}`{conf} - key `{d['key'] or '?'}`, {len(d['attributes'])} attribute(s), used by {', '.join(sorted(set(d['used_by']))) or 'nobody'}"
                   + (f"; SCD type {scd['scd_type']}" if scd else ""))
    derived = [c for c in model["classification"].values() if c["role"] == "derived"]
    if derived:
        out.append("")
        out.append("## Derived / report objects (views without key links)")
        out += [f"- `{c['id']}`" for c in derived]
    others = [c for c in model["classification"].values() if c["role"] not in ("fact", "dimension", "bridge", "lookup", "derived")]
    if others:
        out.append("")
        out.append("## Unclassified")
        out += [f"- `{c['id']}` ({c['type']})" for c in others]
    if model["issues"]:
        out.append("")
        out.append("## Issues / suggestions")
        out += [f"- {i}" for i in model["issues"]]
    out += ["", "```mermaid", to_mermaid(model).rstrip(), "```", ""]
    return "\n".join(out)
