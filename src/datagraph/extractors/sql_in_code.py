"""Detect SQL embedded in code (Python strings, Airflow operator args, JS templates)
and the tables it reads / writes — the automatic code↔data bridge.

``sql_tables(text)`` returns ``(reads, writes, certain)``: sets of lower-cased
table names and a flag that is False when the SQL had placeholders (f-strings,
``{}``/``%s``/``:param``) so the relationship is marked *inferred*.
"""

from __future__ import annotations

import re
from typing import Set, Tuple

try:
    import sqlglot
    from sqlglot import exp

    HAS_SQLGLOT = True
except ImportError:  # pragma: no cover
    HAS_SQLGLOT = False

_SQL_HINT = re.compile(r"\b(select|insert|update|delete|merge|create|with|copy|truncate)\b", re.I)
_SQL_SHAPE = re.compile(r"\b(from|into|join|update|table)\s+[\w\.\"`\[\]]+", re.I)
_READ_RE = re.compile(r"\b(?:from|join)\s+([\w\.\"`\[\]]+)", re.I)
_WRITE_RE = re.compile(r"\b(?:insert\s+into|merge\s+into|update|create\s+(?:or\s+replace\s+)?(?:table|view)(?:\s+if\s+not\s+exists)?|copy\s+into|truncate\s+table|delete\s+from)\s+([\w\.\"`\[\]]+)", re.I)
_PLACEHOLDER_RE = re.compile(r"\{[^}]*\}|%s|%\(\w+\)s|:\w+|\?|\$\d+")
_KEYWORDS = {"select", "where", "values", "set", "on", "as", "and", "or", "not", "null", "dual", "lateral", "unnest"}


def looks_like_sql(text: str) -> bool:
    if not text or len(text) < 12:
        return False
    return bool(_SQL_HINT.search(text)) and bool(_SQL_SHAPE.search(text))


def _clean(name: str) -> str:
    name = name.strip().strip(";,()").replace('"', "").replace("`", "").replace("[", "").replace("]", "")
    return name.lower()


def sql_tables(text: str, dialect=None) -> Tuple[Set[str], Set[str], bool]:
    """Tables read and written by a SQL string. Uses sqlglot when it can parse,
    falls back to regex. The third value is False when placeholders were found."""
    certain = not bool(_PLACEHOLDER_RE.search(text))
    reads: Set[str] = set()
    writes: Set[str] = set()
    parsed = False
    if HAS_SQLGLOT:
        try:
            cleaned = _PLACEHOLDER_RE.sub("placeholder_value", text)
            for stmt in sqlglot.parse(cleaned, read=dialect):
                if stmt is None:
                    continue
                parsed = True
                cte_names = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
                target = None
                if isinstance(stmt, (exp.Insert, exp.Create, exp.Update, exp.Delete, exp.Merge)):
                    t = stmt.find(exp.Table)
                    if t is not None and t.name:
                        target = _full(t)
                        writes.add(target)
                for t in stmt.find_all(exp.Table):
                    if not t.name:
                        continue
                    name = _full(t)
                    if name in cte_names or name == target:
                        continue
                    reads.add(name)
        except Exception:
            parsed = False
    if not parsed:
        for m in _WRITE_RE.finditer(text):
            n = _clean(m.group(1))
            if n and n not in _KEYWORDS:
                writes.add(n)
        for m in _READ_RE.finditer(text):
            n = _clean(m.group(1))
            if n and n not in _KEYWORDS and n not in writes and not n.startswith("("):
                reads.add(n)
    reads = {r for r in reads if r and not r.startswith("placeholder")}
    writes = {w for w in writes if w and not w.startswith("placeholder")}
    return reads, writes, certain


def _full(table) -> str:
    parts = [p.name for p in (table.args.get("catalog"), table.args.get("db")) if p]
    parts.append(table.name)
    return ".".join(parts).lower()
