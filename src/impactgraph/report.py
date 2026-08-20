"""Terminal rendering of an impact analysis using rich."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from .analysis import ImpactAnalysis
from .graph import ImpactGraph

_RISK_STYLES = {
    "LOW": "green",
    "MEDIUM": "yellow",
    "HIGH": "red",
    "CRITICAL": "bold white on red",
}

_TYPE_ICONS = {
    "file": "📄",
    "function": "ƒ ",
    "class": "◆ ",
    "dbt_model": "⬢ ",
    "dbt_source": "⬡ ",
    "dbt_seed": "⬡ ",
    "dbt_snapshot": "⬢ ",
    "table": "▦ ",
    "view": "▤ ",
    "column": "│ ",
    "exposure": "📊",
    "dashboard": "📊",
    "report": "📈",
    "api": "⇄ ",
    "lambda": "λ ",
    "dag": "⛓ ",
}


def render_analysis(
    graph: ImpactGraph, analysis: ImpactAnalysis, console: Console | None = None
) -> None:
    console = console or Console()
    risk_level = analysis.risk["level"]
    style = _RISK_STYLES.get(risk_level, "white")

    header = Text()
    header.append("⚠ Change Impact\n\n", style="bold yellow")
    header.append("Changed:\n", style="bold")
    for nid in analysis.changed:
        node = graph.get_node(nid)
        label = node.name if node else nid
        header.append(f"  {label}\n", style="cyan")
    header.append("\nRisk: ", style="bold")
    header.append(f"{risk_level}", style=style)
    header.append(f"  (score {analysis.risk['score']})")
    console.print(Panel(header, expand=False))

    for tree_dict in analysis.trees:
        rich_tree = _to_rich_tree(tree_dict)
        console.print(rich_tree)
        console.print()

    counts = analysis.summary_by_type()
    if counts:
        console.print("[bold]Affected:[/bold]")
        for type_name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            console.print(f"  {count} {type_name.replace('_', ' ')}(s)")
        console.print()

    console.print("[bold]Recommended tests:[/bold]")
    for rec in analysis.recommended_tests:
        console.print(f"  ✓ {rec}")


def _to_rich_tree(entry: dict) -> Tree:
    icon = _TYPE_ICONS.get(entry.get("type", ""), "· ")
    label = f"{icon}[cyan]{entry['name']}[/cyan] [dim]({entry.get('type', '?')})[/dim]"
    via = entry.get("via")
    if via:
        label += f" [dim italic]via {via}[/dim italic]"
    tree = Tree(label)
    for child in entry.get("children", []):
        tree.add(_to_rich_tree(child))
    return tree
