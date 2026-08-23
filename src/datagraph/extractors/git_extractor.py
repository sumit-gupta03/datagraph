"""Git diff extractor: maps changed files (and changed lines) to graph nodes."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

from ..graph import ImpactGraph, NodeType

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class ChangeSet:
    """Files (and line ranges) changed between two git refs."""

    files: List[str] = field(default_factory=list)
    # file -> list of (start_line, end_line) ranges in the new version
    line_ranges: Dict[str, List[Tuple[int, int]]] = field(default_factory=dict)


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
    )
    return result.stdout


def collect_changes(
    repo: Union[str, Path],
    base: str = "HEAD",
    head: Optional[str] = None,
) -> ChangeSet:
    """Collect changed files and line ranges.

    With only ``base``, compares the working tree (including staged changes)
    against that ref. With ``head`` as well, compares ``base...head``.
    """
    repo = Path(repo).resolve()
    diff_target = [f"{base}...{head}"] if head else [base]

    names = _run_git(repo, "diff", "--name-only", *diff_target)
    files = [line.strip().replace("\\", "/") for line in names.splitlines() if line.strip()]

    changes = ChangeSet(files=files)

    diff = _run_git(repo, "diff", "--unified=0", *diff_target)
    current_file: Optional[str] = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):].strip()
        elif line.startswith("+++ "):
            current_file = None  # deleted file
        elif current_file and line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                end = start + max(count - 1, 0)
                changes.line_ranges.setdefault(current_file, []).append((start, end))
    return changes


def changed_node_ids(graph: ImpactGraph, changes: ChangeSet) -> List[str]:
    """Map a ChangeSet onto graph node ids.

    Returns the file nodes for every changed file, plus any function/class
    nodes whose line span intersects the changed line ranges, plus any nodes
    (dbt models, tables) contained in a changed file.
    """
    ids: List[str] = []
    seen: Set[str] = set()

    # Index nodes by path for suffix matching (graph paths may be repo-relative
    # to a different root than the diff paths).
    by_path: Dict[str, List] = {}
    for node in graph.nodes():
        if node.path:
            by_path.setdefault(node.path, []).append(node)

    for changed_file in changes.files:
        matches = _match_paths(changed_file, by_path)
        for node in matches:
            if node.type in (NodeType.FUNCTION, NodeType.CLASS):
                if not _spans_intersect(node, changes.line_ranges.get(changed_file)):
                    continue
            if node.id not in seen:
                seen.add(node.id)
                ids.append(node.id)
    return ids


def _match_paths(changed_file: str, by_path: Dict[str, List]) -> List:
    if changed_file in by_path:
        candidates = list(by_path[changed_file])
    else:
        candidates = []
        for path, nodes in by_path.items():
            if changed_file.endswith("/" + path) or path.endswith("/" + changed_file):
                candidates.extend(nodes)
    return candidates


def _spans_intersect(node, ranges: Optional[List[Tuple[int, int]]]) -> bool:
    if not ranges:
        # No line info (e.g. rename/binary): treat whole file as changed.
        return True
    start = node.meta.get("lineno")
    end = node.meta.get("end_lineno", start)
    if start is None:
        return True
    return any(not (r_end < start or r_start > end) for r_start, r_end in ranges)
