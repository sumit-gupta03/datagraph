"""JavaScript / TypeScript extractor (regex-based, deterministic, best-effort).

No tree-sitter dependency: a small tokenizer-free scanner finds
  * files (``*.js, *.jsx, *.ts, *.tsx, *.mjs, *.cjs``),
  * functions — ``function name(``, ``async function name(``,
    ``const name = (...) =>`` / ``= async (...) =>`` / ``= function(``,
    ``export function``, class methods ``name(...) {``,
  * imports — ``import ... from './x'`` and ``require('./x')`` resolved to project
    files (extracted), and
  * calls by name inside functions (inferred, like the Python extractor), and
  * SQL embedded in template/normal strings -> table edges (code↔data bridge).

Line spans are recorded so ``datagraph diff`` can map changed lines to functions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from ..graph import EXTRACTED, INFERRED, Edge, EdgeType, ImpactGraph, Node, NodeType
from .base import Extractor
from .sql_in_code import looks_like_sql, sql_tables

_SKIP = {".git", "node_modules", "dist", "build", ".next", "coverage", ".venv", "venv"}
_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_FUNC_RE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(",
    re.M,
)
_ARROW_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>|"
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\b",
    re.M,
)
_METHOD_RE = re.compile(r"^\s{2,}(?:async\s+)?(?:static\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", re.M)
_IMPORT_RE = re.compile(r"""(?:import\s+(?:[^'";]+?\s+from\s+)?|export\s+[^'";]*?\s+from\s+|require\s*\(\s*)['"]([^'"]+)['"]""")
_CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_STRING_RE = re.compile(r"`([^`]*)`|\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'", re.S)
_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "new", "typeof", "await", "async",
                "constructor", "super", "import", "require", "export", "console", "log", "then", "map", "filter",
                "forEach", "reduce", "push", "get", "set", "delete", "class"}


class JsExtractor(Extractor):
    name = "js"

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root).resolve()

    def extract(self) -> ImpactGraph:
        graph = ImpactGraph()
        files = sorted(p for p in self.root.rglob("*") if p.suffix in _EXTS and not any(s in p.parts for s in _SKIP))
        index: Dict[str, List[str]] = {}
        parsed: Dict[str, Tuple[str, List[Tuple[str, int, int]]]] = {}
        for path in files:
            rel = path.relative_to(self.root).as_posix()
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            file_id = f"file:{rel}"
            graph.add_node(Node(id=file_id, type=NodeType.FILE, name=rel, path=rel))
            funcs = _functions(text)
            parsed[rel] = (text, funcs)
            for name, start, end in funcs:
                fid = f"func:{rel}::{name}"
                graph.add_node(Node(id=fid, type=NodeType.FUNCTION, name=name, path=rel, meta={"lineno": start, "end_lineno": end}))
                graph.add_edge(Edge(src=file_id, dst=fid, type=EdgeType.CONTAINS))
                index.setdefault(name, []).append(fid)

        rels = set(parsed)
        for rel, (text, funcs) in parsed.items():
            file_id = f"file:{rel}"
            for spec in _IMPORT_RE.findall(text):
                target = _resolve_import(rel, spec, rels)
                if target and target != rel:
                    graph.add_edge(Edge(src=file_id, dst=f"file:{target}", type=EdgeType.IMPORTS))
            lines = text.splitlines()
            for name, start, end in funcs:
                body = "\n".join(lines[start - 1:end])
                fid = f"func:{rel}::{name}"
                for callee in set(_CALL_RE.findall(body)):
                    if callee == name or callee in _JS_KEYWORDS:
                        continue
                    for cid in index.get(callee, []):
                        if cid != fid:
                            graph.add_edge(Edge(src=fid, dst=cid, type=EdgeType.CALLS,
                                                meta={"provenance": INFERRED, "reason": "resolved by function name"}))
                for m in _STRING_RE.finditer(body):
                    s = next(g for g in m.groups() if g is not None)
                    if looks_like_sql(s):
                        reads, writes, certain = sql_tables(s.replace("${", "{"))
                        prov = EXTRACTED if certain and "${" not in s else INFERRED
                        for t in reads:
                            graph.add_edge(Edge(src=fid, dst=f"table:{t}", type=EdgeType.DEPENDS_ON, meta={"provenance": prov, "via": "sql-in-code"}))
                        for t in writes:
                            graph.add_edge(Edge(src=fid, dst=f"table:{t}", type=EdgeType.WRITES_TO, meta={"provenance": prov, "via": "sql-in-code"}))
        return graph


def _functions(text: str) -> List[Tuple[str, int, int]]:
    """(name, start_line, end_line) for top-level-ish functions; end = next start - 1 or EOF."""
    starts: List[Tuple[int, str]] = []
    for m in _FUNC_RE.finditer(text):
        starts.append((m.start(), m.group(1)))
    for m in _ARROW_RE.finditer(text):
        starts.append((m.start(), m.group(1) or m.group(2)))
    for m in _METHOD_RE.finditer(text):
        name = m.group(1)
        if name not in _JS_KEYWORDS:
            starts.append((m.start(), name))
    starts.sort()
    out: List[Tuple[str, int, int]] = []
    total_lines = text.count("\n") + 1
    for i, (pos, name) in enumerate(starts):
        start_line = text.count("\n", 0, pos) + 1
        if i + 1 < len(starts):
            end_line = max(start_line, text.count("\n", 0, starts[i + 1][0]))
        else:
            end_line = total_lines
        out.append((name, start_line, end_line))
    return out


def _resolve_import(from_rel: str, spec: str, rels: set) -> Optional[str]:
    if not spec.startswith("."):
        return None
    base = (Path(from_rel).parent / spec).as_posix()
    base = _normpath(base)
    candidates = [base] + [base + ext for ext in _EXTS] + [base + "/index" + ext for ext in _EXTS]
    for c in candidates:
        if c in rels:
            return c
    return None


def _normpath(p: str) -> str:
    parts: List[str] = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)
