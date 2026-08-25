"""Light data profiling — facts about the data, attached to graph nodes.

``profile_warehouse(connection, graph)`` computes, for tables/views the graph
knows about (by default those that came from a warehouse / SQLite extraction):

  table:  row_count, profiled_at, freshness (max of date/timestamp columns)
  column: null_pct, distinct, min, max, top_values (sampled)

Results are stored in ``node.meta["profile"]`` and flow into risk scoring,
``relationships`` / ``lineage`` output, the HTML views, the knowledge wiki and
the LLM fallback (distinct counts help spot join keys). It is a snapshot on
demand — deterministic SQL over a sample, never continuous monitoring.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Dict, Iterable, List, Optional

from .security import is_sensitive_column
from .graph import ImpactGraph, NodeType

_DATE_HINTS = ("date", "time", "timestamp", "_at", "_ts", "day")
_MAX_COLUMNS = 60


def profile_warehouse(
    connection,
    graph: ImpactGraph,
    tables: Optional[Iterable[str]] = None,
    sample: int = 100_000,
    top_values: bool = True,
    max_columns: int = _MAX_COLUMNS,
    log=None,
) -> Dict[str, Dict]:
    """Profile tables through a DB-API connection and attach results to the graph.

    ``tables`` — node ids (``table:...``) or bare names; default: every table/view
    node whose metadata says it came from a warehouse/sqlite extraction.
    Returns {table_id: profile}.
    """
    if isinstance(connection, str):
        from .extractors.warehouse_extractor import connect

        connection = connect(connection)
    from .extractors.warehouse_extractor import dbapi_connection

    is_sqlite = isinstance(dbapi_connection(connection), sqlite3.Connection)
    quote = _quote_char(connection)   # MySQL uses backticks; everyone else double quotes
    targets = _targets(graph, tables)
    results: Dict[str, Dict] = {}
    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    for tid in targets:
        node = graph.get_node(tid)
        if node is None:
            continue
        rel = _relation_sql(tid, node, is_sqlite, quote)
        cols = [c for c in graph.nodes(NodeType.COLUMN) if c.meta.get("parent") == tid][:max_columns]
        prof: Dict = {"profiled_at": now, "sample": sample}
        try:
            prof["row_count"] = _scalar(connection, f"SELECT COUNT(*) FROM {rel}")
        except Exception as e:
            prof["error"] = str(e)[:200]
            node.meta["profile"] = prof
            results[tid] = prof
            if log:
                log(f"profile {tid}: {prof['error']}")
            continue
        if cols:
            names = [c.name for c in cols]
            exprs = []
            for n in names:
                q = _q(n, quote)
                exprs += [f"COUNT({q})", f"COUNT(DISTINCT {q})", f"MIN({q})", f"MAX({q})"]
            sql = f"SELECT COUNT(*), {', '.join(exprs)} FROM (SELECT * FROM {rel} LIMIT {int(sample)}) t"
            try:
                row = _row(connection, sql)
            except Exception:
                row = None
            if row:
                n_rows = row[0] or 0
                for i, col in enumerate(cols):
                    cnt, dist, mn, mx = row[1 + i * 4: 5 + i * 4]
                    sensitive = is_sensitive_column(col.name)
                    cp = {
                        "null_pct": round(100.0 * (n_rows - (cnt or 0)) / n_rows, 2) if n_rows else None,
                        "distinct": dist,
                        "min": None if sensitive else _jsonable(mn),
                        "max": None if sensitive else _jsonable(mx),
                        "sampled_rows": n_rows,
                    }
                    if sensitive:
                        cp["masked"] = True  # counts only - no sample values for personal/secret-looking columns
                    col.meta["profile"] = cp
                # freshness: max over date-like columns
                fresh = [c.meta["profile"]["max"] for c in cols if c.meta.get("profile") and _looks_temporal(c) and c.meta["profile"].get("max") is not None]
                if fresh:
                    prof["freshness"] = max(str(f) for f in fresh)
            if top_values:
                for col in cols[:20]:
                    if is_sensitive_column(col.name):
                        continue
                    q = _q(col.name, quote)
                    try:
                        rows = _rows(connection, f"SELECT {q}, COUNT(*) AS c FROM (SELECT {q} FROM {rel} LIMIT {int(sample)}) t GROUP BY {q} ORDER BY c DESC LIMIT 5")
                        col.meta.setdefault("profile", {})["top_values"] = [[_jsonable(v), c] for v, c in rows]
                    except Exception:
                        pass
            prof["columns_profiled"] = len(cols)
        node.meta["profile"] = prof
        results[tid] = prof
        if log:
            log(f"profile {tid}: {prof.get('row_count')} rows, {len(cols)} columns")
    return results


# --------------------------------------------------------------- helpers


def _targets(graph: ImpactGraph, tables) -> List[str]:
    if tables:
        out = []
        for t in tables:
            node = graph.get_node(t) or graph.resolve(t)
            if node is None and not str(t).startswith("table:"):
                node = graph.get_node("table:" + str(t).lower())
            if node is not None:
                out.append(node.id)
        return out
    return [n.id for n in graph.nodes() if n.type in (NodeType.TABLE, NodeType.VIEW)
            and n.meta.get("source") in ("warehouse", "sqlite")]


def _quote_char(connection) -> str:
    """MySQL accepts double-quoted identifiers only when ANSI_QUOTES is set; backticks always work.

    Getting this wrong is silent, not loud: MySQL reads a double-quoted identifier as a string
    literal, so every statistic comes back constant instead of raising.
    """
    from .extractors.warehouse_extractor import engine_name

    return "`" if engine_name(connection) == "mysql" else chr(34)


def _relation_sql(tid: str, node, is_sqlite: bool, quote: str = '"') -> str:
    bare = tid.split(":", 1)[1]
    if is_sqlite:
        return _q(bare.split(".")[-1], quote)
    return ".".join(_q(p, quote) if not p.islower() else p for p in bare.split("."))  # lower-case needs no quoting


def _q(name: str, quote: str = '"') -> str:
    return quote + str(name).replace(quote, quote * 2) + quote


def _scalar(con, sql):
    return _row(con, sql)[0]


def _row(con, sql):
    cur = con.cursor()
    try:
        cur.execute(sql)
        return cur.fetchone()
    finally:
        close = getattr(cur, "close", None)
        if close:
            close()


def _rows(con, sql):
    cur = con.cursor()
    try:
        cur.execute(sql)
        return cur.fetchall()
    finally:
        close = getattr(cur, "close", None)
        if close:
            close()


def _looks_temporal(col) -> bool:
    dtype = str(col.meta.get("data_type") or "").lower()
    name = col.name.lower()
    return any(h in dtype for h in ("date", "time")) or any(name.endswith(h) or h in name for h in _DATE_HINTS)


def _jsonable(v):
    if isinstance(v, (int, float, str)) or v is None:
        return v
    return str(v)


def profile_summary(node) -> str:
    """One-line human summary of a node's profile (or '')."""
    p = (node.meta or {}).get("profile") or {}
    if not p:
        return ""
    if node.type == NodeType.COLUMN:
        bits = []
        if p.get("null_pct") is not None:
            bits.append(f"{p['null_pct']}% null")
        if p.get("distinct") is not None:
            bits.append(f"{p['distinct']} distinct")
        if p.get("min") is not None or p.get("max") is not None:
            bits.append(f"range {p.get('min')}..{p.get('max')}")
        return ", ".join(bits)
    bits = []
    if p.get("row_count") is not None:
        bits.append(f"{p['row_count']:,} rows")
    if p.get("freshness"):
        bits.append(f"fresh to {p['freshness']}")
    if p.get("error"):
        bits.append(f"profile error: {p['error']}")
    return ", ".join(bits)
