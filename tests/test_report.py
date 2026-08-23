"""The terminal report must render on UTF-8 and on legacy (cp1252) consoles alike."""

import io

from rich.console import Console

from datagraph.analysis import analyze_impact
from datagraph.report import render_analysis


def _render_with_encoding(graph, analysis, encoding: str) -> str:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding=encoding, errors="strict", newline="")
    console = Console(file=stream, force_terminal=False, width=100, color_system=None)
    render_analysis(graph, analysis, console=console)
    stream.flush()
    return raw.getvalue().decode(encoding)


def test_render_utf8_uses_glyphs(dbt_graph):
    analysis = analyze_impact(dbt_graph, ["dbt:customer"])
    out = _render_with_encoding(dbt_graph, analysis, "utf-8")
    assert "Change Impact" in out
    assert "revenue_report" in out
    assert "⚠" in out and "✓" in out


def test_render_cp1252_falls_back_to_ascii(dbt_graph):
    """Windows legacy consoles (cp1252) cannot encode the glyphs — must not crash."""
    analysis = analyze_impact(dbt_graph, ["dbt:customer"])
    out = _render_with_encoding(dbt_graph, analysis, "cp1252")
    assert "! Change Impact" in out
    assert "[dbt]" in out
    assert "+ dbt build" in out
    assert "revenue_report" in out
