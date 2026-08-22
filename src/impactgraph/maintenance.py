"""Keeping the graph fresh: input fingerprints (--update), --watch, git hooks."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional

_SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", "dist", "build"}


def fingerprint(
    paths: Iterable[Optional[str]],
    patterns=("*.py", "*.sql", "*.json", "*.yml", "*.yaml"),
    exclude: Iterable[Optional[str]] = (),
) -> str:
    """SHA256 over the content of every input file (directories are walked).

    ``exclude`` lists files to ignore — the graph output and its cache, which
    often live inside the scanned repo and must not change the fingerprint.
    """
    h = hashlib.sha256()
    excluded = {Path(e).resolve() for e in exclude if e}
    for p in sorted(str(x) for x in paths if x):
        path = Path(p)
        if path.is_dir():
            files = sorted(f for pat in patterns for f in path.rglob(pat) if not any(s in f.parts for s in _SKIP))
        elif path.exists():
            files = [path]
        else:
            continue
        files = [f for f in files if f.resolve() not in excluded]
        for f in files:
            h.update(str(f.relative_to(path) if path.is_dir() else f.name).encode())
            try:
                h.update(f.read_bytes())
            except OSError:
                continue
    return h.hexdigest()


def cache_path(graph_path: str) -> Path:
    return Path(str(graph_path) + ".cache.json")


def is_up_to_date(graph_path: str, inputs: Iterable[Optional[str]]) -> bool:
    cp = cache_path(graph_path)
    if not cp.exists() or not Path(graph_path).exists():
        return False
    try:
        cached = json.loads(cp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return cached.get("fingerprint") == fingerprint(inputs, exclude=_outputs(graph_path))


def write_cache(graph_path: str, inputs: Iterable[Optional[str]]) -> None:
    inputs = list(inputs)
    cache_path(graph_path).write_text(
        json.dumps(
            {"fingerprint": fingerprint(inputs, exclude=_outputs(graph_path)), "inputs": [i for i in inputs if i]},
            indent=2,
        ),
        encoding="utf-8",
    )


def _outputs(graph_path: str) -> List[str]:
    return [str(graph_path), str(cache_path(graph_path))]


def watch(
    build: Callable[[], None],
    inputs: List[Optional[str]],
    interval: float = 2.0,
    max_iterations: Optional[int] = None,
    log=print,
    exclude: Iterable[Optional[str]] = (),
) -> None:
    """Rebuild whenever any input changes. ``max_iterations`` is for tests."""
    last = None
    i = 0
    while max_iterations is None or i < max_iterations:
        current = fingerprint(inputs, exclude=exclude)
        if current != last:
            build()
            last = current
            log("graph rebuilt — watching for changes (Ctrl+C to stop)")
        i += 1
        if max_iterations is None or i < max_iterations:
            time.sleep(interval)


def install_hook(repo: str, command: str, hook: str = "post-commit") -> Path:
    """Write a git hook that runs ``command`` (e.g. 'impactgraph build ... --update')."""
    hooks_dir = Path(repo) / ".git" / "hooks"
    if not hooks_dir.exists():
        raise FileNotFoundError(f"{hooks_dir} not found — is {repo} a git repository?")
    path = hooks_dir / hook
    path.write_text("#!/bin/sh\n# installed by impactgraph\n" + command + "\n", encoding="utf-8")
    try:
        os.chmod(path, path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    return path
