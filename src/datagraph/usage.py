"""Usage statistics from the warehouse's own query log.

A catalog shows "popularity" by ingesting query history. datagraph reads the same system views
directly and attaches the result to the graph, which answers two questions a brownfield project
always has:

    * which tables does anyone actually query?
    * which tables can we delete?  (nothing downstream **and** nobody queries them)

Supported engines and the view each one uses:

    snowflake   SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY   (needs the ACCOUNT_USAGE grant)
    bigquery    <region>.INFORMATION_SCHEMA.JOBS         (referenced_tables)
    postgres    pg_stat_user_tables                      (seq_scan + idx_scan since stats reset)
    mysql       performance_schema.table_io_waits_summary_by_table
    others      no query log -> {} and a note; nothing fails

Results live on the node as ``meta["usage"] = {queries, last_query, window_days, source}``.
Read-only: every statement is a SELECT against a system view.
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional

from .graph import ImpactGraph, NodeType
from .security import redact_dsn  # noqa: F401  (imported for callers that log a DSN)

#: engines whose query log we can read, and whether the count is windowed by time
_SUPPORTED = ("snowflake", "bigquery", "postgres", "postgresql", "mysql")


def detect_dialect(connection) -> str:
    """Engine name behind a connection, seeing through SQLAlchemy's wrappers."""
    from .extractors.warehouse_extractor import engine_name

    return engine_name(connection)


def _sql(dialect: str, days: int, bigquery_region: str = "region-us") -> Optional[str]:
    d = (dialect or "").lower()
    if d == "snowflake":
        return (
            "SELECT LOWER(value:objectName::string) AS object_name, COUNT(*) AS queries, "
            "MAX(query_start_time) AS last_query "
            "FROM snowflake.account_usage.access_history, "
            "LATERAL FLATTEN(input => direct_objects_accessed) "
            f"WHERE query_start_time >= DATEADD(day, -{int(days)}, CURRENT_TIMESTAMP()) "
            "GROUP BY 1"
        )
    if d == "bigquery":
        return (
            "SELECT LOWER(CONCAT(t.project_id, '.', t.dataset_id, '.', t.table_id)) AS object_name, "
            "COUNT(*) AS queries, MAX(creation_time) AS last_query "
            f"FROM `{bigquery_region}`.INFORMATION_SCHEMA.JOBS, UNNEST(referenced_tables) AS t "
            f"WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY) "
            "GROUP BY 1"
        )
    if d in ("postgres", "postgresql"):
        # counters accumulate since the last stats reset - not a time window
        return (
            "SELECT LOWER(schemaname || '.' || relname) AS object_name, "
            "COALESCE(seq_scan, 0) + COALESCE(idx_scan, 0) AS queries, NULL AS last_query "
            "FROM pg_stat_user_tables"
        )
    if d == "mysql":
        return (
            "SELECT LOWER(CONCAT(object_schema, '.', object_name)) AS object_name, "
            "COUNT_READ AS queries, NULL AS last_query "
            "FROM performance_schema.table_io_waits_summary_by_table "
            "WHERE object_schema NOT IN ('mysql', 'performance_schema', 'sys', 'information_schema')"
        )
    return None


def _index(graph: ImpactGraph) -> Dict[str, List[str]]:
    """Map every suffix of a table id's bare name to that node, so 'schema.table' matches
    'db.schema.table' the way the warehouse reports it."""
    index: Dict[str, List[str]] = {}
    for node in graph.nodes(NodeType.TABLE) + graph.nodes(NodeType.VIEW):
        bare = node.id.split(":", 1)[1].lower()
        parts = bare.split(".")
        for i in range(len(parts)):
            index.setdefault(".".join(parts[i:]), []).append(node.id)
    return index


def _lookup(index: Dict[str, List[str]], object_name: str) -> List[str]:
    """Match a name from the query log to graph nodes, in either direction of qualification.

    The graph may hold ``fact_sales`` while the engine reports ``analytics.fact_sales`` (or the
    reverse). Try the full name first, then progressively shorter suffixes, and only accept a
    shortened match when exactly one node claims it - never guess between two candidates.
    """
    hit = index.get(object_name)
    if hit:
        return hit
    parts = object_name.split(".")
    for i in range(1, len(parts)):
        candidates = index.get(".".join(parts[i:]))
        if candidates and len(candidates) == 1:
            return candidates
    return []


def usage_stats(
    connection,
    graph: ImpactGraph,
    dialect: Optional[str] = None,
    days: int = 30,
    bigquery_region: str = "region-us",
    log=None,
) -> Dict[str, Dict]:
    """Read the engine's query log and attach ``meta["usage"]`` to matching tables.

    Returns {node_id: usage}. Unsupported engines return ``{}`` (with a note through ``log``),
    and a permission error is reported rather than raised - usage is a bonus, never a blocker.
    """
    dialect = (dialect or detect_dialect(connection)).lower()
    sql = _sql(dialect, days, bigquery_region)
    if sql is None:
        if log:
            log(f"usage: '{dialect}' has no query log datagraph can read (supported: {', '.join(_SUPPORTED)})")
        return {}

    cursor = connection.cursor()
    try:
        cursor.execute(sql)
        rows = list(cursor.fetchall())
    except Exception as exc:  # noqa: BLE001 - a missing grant must not fail the build
        if log:
            log(f"usage: could not read the query log ({str(exc)[:160]})")
        return {}
    finally:
        close = getattr(cursor, "close", None)
        if close:
            close()

    index = _index(graph)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    results: Dict[str, Dict] = {}
    for row in rows:
        object_name = str(row[0] or "").lower().strip('"')
        queries = int(row[1] or 0)
        last_query = row[2]
        for node_id in _lookup(index, object_name):
            entry = results.setdefault(node_id, {"queries": 0, "last_query": None,
                                                 "window_days": days if dialect in ("snowflake", "bigquery") else None,
                                                 "source": dialect, "collected_at": now})
            entry["queries"] += queries
            if last_query is not None:
                text = str(last_query)
                if entry["last_query"] is None or text > str(entry["last_query"]):
                    entry["last_query"] = text

    # every table we know about gets a usage record, so "0 queries" is distinguishable from "not measured"
    for node in graph.nodes(NodeType.TABLE) + graph.nodes(NodeType.VIEW):
        if node.meta.get("source") not in ("warehouse", "sqlite"):
            continue
        entry = results.get(node.id) or {"queries": 0, "last_query": None,
                                         "window_days": days if dialect in ("snowflake", "bigquery") else None,
                                         "source": dialect, "collected_at": now}
        results[node.id] = entry
        node.meta["usage"] = entry

    for node_id, entry in results.items():
        node = graph.get_node(node_id)
        if node is not None:
            node.meta["usage"] = entry
    if log:
        queried = sum(1 for e in results.values() if e["queries"])
        log(f"usage: {queried}/{len(results)} table(s) queried in the last {days} day(s) ({dialect})")
    return results


def unused_tables(graph: ImpactGraph, include_inferred: bool = True) -> List[Dict]:
    """Tables that were measured, nobody queried, and nothing downstream depends on.

    The safest deletion candidates in a brownfield warehouse - both signals must agree.
    """
    rows = []
    for node in graph.nodes(NodeType.TABLE) + graph.nodes(NodeType.VIEW):
        usage = node.meta.get("usage")
        if not usage or usage.get("queries"):
            continue
        downstream = [k for k in graph.impact(node.id, include_inferred=include_inferred)
                      if not k.startswith("column:") and k != node.id]
        rows.append({
            "id": node.id, "name": node.name, "type": node.type.value, "owner": node.owner,
            "queries": usage.get("queries", 0), "window_days": usage.get("window_days"),
            "downstream": downstream, "safe_to_drop": not downstream,
            "row_count": (node.meta.get("profile") or {}).get("row_count"),
        })
    return sorted(rows, key=lambda r: (not r["safe_to_drop"], r["id"]))


def usage_summary(node) -> str:
    """One-line human summary of a node's usage (or '')."""
    usage = (getattr(node, "meta", None) or {}).get("usage")
    if not usage:
        return ""
    window = f" in {usage['window_days']}d" if usage.get("window_days") else ""
    last = f", last {str(usage['last_query'])[:19]}" if usage.get("last_query") else ""
    return f"{usage.get('queries', 0)} quer{'y' if usage.get('queries') == 1 else 'ies'}{window}{last}"
