"""Self-contained interactive HTML view of a blast radius (no external assets)."""

from __future__ import annotations

import html
import json
from typing import Dict, List

from .analysis import ImpactAnalysis
from .graph import IMPACT_DIRECTION, INFERRED, ImpactGraph

_COLORS = {
    "file": "#8E8E93",
    "function": "#5AC8FA",
    "class": "#34AADC",
    "dbt_model": "#FF9500",
    "dbt_source": "#FFCC00",
    "dbt_seed": "#FFCC00",
    "dbt_snapshot": "#FF9500",
    "table": "#4CD964",
    "view": "#A4E47A",
    "column": "#C7C7CC",
    "exposure": "#FF3B30",
    "dashboard": "#FF3B30",
    "report": "#FF2D55",
    "api": "#AF52DE",
    "lambda": "#5856D6",
    "dag": "#007AFF",
}


def render_html(graph: ImpactGraph, analysis: ImpactAnalysis, title: str = "impactgraph — change impact") -> str:
    node_ids: List[str] = list(analysis.changed) + [n for n in analysis.affected if n not in analysis.changed]
    depth: Dict[str, int] = {nid: 0 for nid in analysis.changed}
    depth.update(analysis.affected)

    # layered layout: x by depth, y spread within layer (deterministic)
    layers: Dict[int, List[str]] = {}
    for nid in node_ids:
        layers.setdefault(depth.get(nid, 0), []).append(nid)
    max_layer = max(layers) if layers else 0
    col_w, row_h, pad = 260, 48, 40
    positions: Dict[str, tuple] = {}
    height = max(len(v) for v in layers.values()) * row_h + 2 * pad if layers else 200
    for d, ids in layers.items():
        ids.sort(key=lambda i: (graph.get_node(i).type.value if graph.get_node(i) else "", i))
        total = len(ids) * row_h
        y0 = (height - total) / 2 + row_h / 2
        for k, nid in enumerate(ids):
            positions[nid] = (pad + 90 + d * col_w, y0 + k * row_h)
    width = pad * 2 + 180 + max_layer * col_w + 160

    nodes_js = []
    for nid in node_ids:
        node = graph.get_node(nid)
        x, y = positions[nid]
        nodes_js.append(
            {
                "id": nid,
                "name": node.name if node else nid,
                "type": node.type.value if node else "unknown",
                "depth": depth.get(nid, 0),
                "owner": (node.owner if node else None) or "",
                "x": round(x, 1),
                "y": round(y, 1),
                "changed": nid in analysis.changed,
            }
        )
    ids = set(node_ids)
    edges_js = []
    for edge in graph.edges():
        if edge.src in ids and edge.dst in ids:
            if IMPACT_DIRECTION[edge.type] == "forward":
                s, t = edge.src, edge.dst
            else:
                s, t = edge.dst, edge.src
            edges_js.append({"s": s, "t": t, "type": edge.type.value, "inferred": edge.meta.get("provenance") == INFERRED})

    data = json.dumps({"nodes": nodes_js, "edges": edges_js, "risk": analysis.risk, "tests": analysis.recommended_tests,
                       "owners": analysis.owners, "colors": _COLORS, "width": width, "height": height})
    changed_names = ", ".join(html.escape(graph.get_node(n).name if graph.get_node(n) else n) for n in analysis.changed)
    risk = analysis.risk["level"]
    risk_color = {"LOW": "#34C759", "MEDIUM": "#FFCC00", "HIGH": "#FF9500", "CRITICAL": "#FF3B30"}.get(risk, "#999")
    return _TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__CHANGED__", changed_names) \
        .replace("__RISK__", risk).replace("__RISKCOLOR__", risk_color) \
        .replace("__SCORE__", str(analysis.risk["score"])).replace("__DATA__", data) \
        .replace("__COUNT__", str(len(analysis.affected)))


_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
 body{margin:0;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;background:#0f1115;color:#e6e6e6}
 header{padding:14px 20px;border-bottom:1px solid #262a33;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
 header h1{font-size:16px;margin:0;font-weight:600}
 .pill{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700;color:#111}
 input{background:#1a1e26;border:1px solid #2c313c;color:#eee;padding:6px 10px;border-radius:6px;min-width:220px}
 label{font-size:12px;color:#aaa;display:flex;gap:6px;align-items:center}
 main{display:grid;grid-template-columns:1fr 320px;height:calc(100vh - 57px)}
 #canvas{overflow:auto}
 aside{border-left:1px solid #262a33;padding:14px;overflow:auto;font-size:13px}
 aside h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#8a909c;margin:14px 0 6px}
 .legend span{display:inline-flex;align-items:center;gap:5px;margin:2px 8px 2px 0;font-size:12px}
 .dot{width:10px;height:10px;border-radius:50%;display:inline-block}
 .node{cursor:pointer}
 .node text{font-size:11px;fill:#e6e6e6;pointer-events:none}
 .node.dim{opacity:.15}
 .edge{stroke:#596273;stroke-width:1.3;fill:none;marker-end:url(#arrow)}
 .edge.inferred{stroke-dasharray:4 3;stroke:#b08a2e}
 .edge.dim{opacity:.08}
 .edge.hl{stroke:#ff9f0a;stroke-width:2}
 ul{padding-left:18px;margin:4px 0}
 li{margin:3px 0}
 #tip{position:fixed;pointer-events:none;background:#1a1e26;border:1px solid #2c313c;padding:6px 9px;border-radius:6px;font-size:12px;display:none}
</style></head><body>
<header>
 <h1>__TITLE__</h1>
 <span>Changed: <b>__CHANGED__</b></span>
 <span class="pill" style="background:__RISKCOLOR__">Risk __RISK__ · score __SCORE__</span>
 <span>__COUNT__ affected</span>
 <input id="q" placeholder="search nodes… (click a node to highlight its downstream)">
 <label><input type="checkbox" id="inf" checked style="min-width:0"> show inferred edges</label>
</header>
<main>
 <div id="canvas"><svg id="svg"></svg></div>
 <aside>
  <h2>Legend</h2><div class="legend" id="legend"></div>
  <h2>Owners to notify</h2><div id="owners"></div>
  <h2>Recommended tests</h2><ul id="tests"></ul>
  <h2>Selected</h2><div id="sel">Click a node.</div>
 </aside>
</main>
<div id="tip"></div>
<script>
const D = __DATA__;
const svg = document.getElementById('svg');
svg.setAttribute('width', D.width); svg.setAttribute('height', D.height);
const NS = 'http://www.w3.org/2000/svg';
const el = (t,a)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);return e;};
const defs = el('defs',{}); const m = el('marker',{id:'arrow',viewBox:'0 0 10 10',refX:'10',refY:'5',markerWidth:'7',markerHeight:'7',orient:'auto-start-reverse'});
m.appendChild(el('path',{d:'M0 0L10 5L0 10z',fill:'#596273'})); defs.appendChild(m); svg.appendChild(defs);
const pos = {}; D.nodes.forEach(n=>pos[n.id]=n);
const out = {}; D.edges.forEach(e=>{(out[e.s]=out[e.s]||[]).push(e.t)});
const edgeEls = D.edges.map(e=>{
  const a=pos[e.s], b=pos[e.t]; if(!a||!b) return null;
  const dx=(b.x-a.x)/2;
  const p = el('path',{d:`M${a.x+70} ${a.y} C ${a.x+70+dx} ${a.y}, ${b.x-70-dx} ${b.y}, ${b.x-70} ${b.y}`,class:'edge'+(e.inferred?' inferred':'')});
  p.dataset.s=e.s; p.dataset.t=e.t; p.dataset.inferred=e.inferred; svg.appendChild(p); return p;}).filter(Boolean);
const nodeEls = {};
D.nodes.forEach(n=>{
  const g = el('g',{class:'node',transform:`translate(${n.x},${n.y})`});
  g.appendChild(el('rect',{x:-70,y:-16,width:140,height:32,rx:8,fill:n.changed?'#ffd60a':(D.colors[n.type]||'#888'),stroke:n.changed?'#fff':'#0f1115','stroke-width':n.changed?2:1,opacity:.92}));
  const t = el('text',{'text-anchor':'middle',y:4}); t.textContent = n.name.length>20? n.name.slice(0,19)+'…':n.name;
  t.setAttribute('fill', '#111'); g.appendChild(t);
  g.addEventListener('click',()=>select(n.id));
  g.addEventListener('mousemove',ev=>{tip.style.display='block';tip.style.left=(ev.clientX+12)+'px';tip.style.top=(ev.clientY+12)+'px';tip.textContent=`${n.id} · ${n.type} · depth ${n.depth}${n.owner?' · owner '+n.owner:''}`});
  g.addEventListener('mouseleave',()=>tip.style.display='none');
  svg.appendChild(g); nodeEls[n.id]=g;
});
const tip=document.getElementById('tip');
function select(id){
  const reach=new Set([id]); const st=[id];
  while(st.length){const c=st.pop(); (out[c]||[]).forEach(t=>{if(!reach.has(t)){reach.add(t);st.push(t)}})}
  D.nodes.forEach(n=>nodeEls[n.id].classList.toggle('dim',!reach.has(n.id)));
  edgeEls.forEach(p=>{const on=reach.has(p.dataset.s)&&reach.has(p.dataset.t);p.classList.toggle('dim',!on);p.classList.toggle('hl',on)});
  const n=pos[id]; document.getElementById('sel').innerHTML=`<b>${n.name}</b><br>${n.id}<br>type: ${n.type}<br>depth: ${n.depth}<br>${n.owner?'owner: '+n.owner+'<br>':''}downstream in view: ${reach.size-1}`;
}
document.getElementById('q').addEventListener('input',ev=>{const q=ev.target.value.toLowerCase();D.nodes.forEach(n=>nodeEls[n.id].classList.toggle('dim',q&&!(n.id.toLowerCase().includes(q)||n.name.toLowerCase().includes(q))))});
document.getElementById('inf').addEventListener('change',ev=>{edgeEls.forEach(p=>{if(p.dataset.inferred==='true')p.style.display=ev.target.checked?'':'none'})});
const lg=document.getElementById('legend'); Object.entries(D.colors).forEach(([k,v])=>{const s=document.createElement('span');s.innerHTML=`<i class="dot" style="background:${v}"></i>${k}`;lg.appendChild(s)});
const ow=document.getElementById('owners'); const oks=Object.keys(D.owners||{}); ow.innerHTML = oks.length? oks.map(k=>`<div><b>${k}</b>: ${D.owners[k].join(', ')}</div>`).join('') : '<i>no owners recorded</i>';
const ul=document.getElementById('tests'); D.tests.forEach(t=>{const li=document.createElement('li');li.textContent=t;ul.appendChild(li)});
</script></body></html>
"""
