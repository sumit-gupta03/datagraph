"""Extractor plugin registry.

Third-party packages add extractors without touching datagraph by declaring an
entry point in their ``pyproject.toml``::

    [project.entry-points."datagraph.extractors"]
    looker = "datagraph_looker:plugin"

where ``plugin`` is a :class:`ExtractorPlugin` (or a callable returning one).
After ``pip install datagraph-looker``, ``datagraph build --looker PATH`` just
works, and ``datagraph plugins`` lists what is installed. Plugins can also be
registered programmatically with :func:`register`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..graph import ImpactGraph


@dataclass
class ExtractorPlugin:
    name: str                          # CLI flag name: --<name>
    factory: Callable[..., object]     # factory(value, **options) -> object with .extract() -> ImpactGraph
    help: str = ""                     # shown in `datagraph build --help`
    value_name: str = "PATH"           # metavar for the CLI flag
    options: Dict[str, str] = field(default_factory=dict)  # extra flags: {"token": "help text"} -> --<name>-token
    source: str = "registered"         # "entry-point:<dist>" or "registered"

    def extract(self, value: str, **options) -> ImpactGraph:
        ext = self.factory(value, **{k: v for k, v in options.items() if v is not None})
        graph = ext.extract() if hasattr(ext, "extract") else ext
        if not isinstance(graph, ImpactGraph):
            raise TypeError(f"plugin '{self.name}' must produce an ImpactGraph, got {type(graph).__name__}")
        return graph


_REGISTRY: Dict[str, ExtractorPlugin] = {}
_LOADED = False


def register(plugin: ExtractorPlugin) -> ExtractorPlugin:
    """Register a plugin programmatically (tests, embedded use)."""
    _REGISTRY[plugin.name] = plugin
    return plugin


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def load_entry_points(group: str = "datagraph.extractors") -> List[ExtractorPlugin]:
    """Discover plugins declared by installed packages (idempotent)."""
    global _LOADED
    if _LOADED:
        return list(_REGISTRY.values())
    _LOADED = True
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return list(_REGISTRY.values())
    try:
        eps = entry_points(group=group)
    except TypeError:  # Python 3.9: entry_points() returns a dict
        eps = entry_points().get(group, [])
    for ep in eps:
        try:
            obj = ep.load()
            plugin = obj() if callable(obj) and not isinstance(obj, ExtractorPlugin) else obj
            if isinstance(plugin, ExtractorPlugin):
                plugin.source = f"entry-point:{getattr(ep, 'dist', None) and ep.dist.name or ep.value}"
                _REGISTRY.setdefault(plugin.name, plugin)
        except Exception as e:  # a broken plugin must not break datagraph
            _REGISTRY.setdefault(ep.name, ExtractorPlugin(name=ep.name, factory=_broken(ep.name, e), help=f"(failed to load: {e})", source="entry-point"))
    return list(_REGISTRY.values())


def plugins() -> List[ExtractorPlugin]:
    load_entry_points()
    return sorted(_REGISTRY.values(), key=lambda p: p.name)


def get(name: str) -> Optional[ExtractorPlugin]:
    load_entry_points()
    return _REGISTRY.get(name)


def _broken(name: str, error: Exception):
    def factory(value, **options):
        raise RuntimeError(f"extractor plugin '{name}' failed to load: {error}")

    return factory
