"""Render the architecture guide as portable HTML with offline SVG diagrams."""
from pathlib import Path
import html
import re
import textwrap

ROOT = Path(__file__).resolve().parents[1]
# Each edge refers to a numbered box. Mermaid sources in the guide hold the full detail.
DIAGRAMS = [
    (["Browser phone simulator", "Gemini Live audio", "FastAPI + ephemeral tokens", "Reviewed policy summaries", "LangGraph workflow", "SQLite / libSQL + receipts", "Simulated SMS and inventory", "Simulated payments and handoff"], [(0,1),(0,2),(2,3),(2,4),(4,5),(4,6),(5,7)]),
    (["Gemini requests tools", "Finished caller turn", "Schema + workflow guard", "Fresh approval / resend", "Identity: request / resend / verify", "Reads: case / options / policy", "propose_action / confirm_action", "save_notes / transfer_to_representative", "Atomic receipt + notes", "Final result to Gemini"], [(0,2),(1,3),(3,4),(2,4),(2,5),(2,6),(2,7),(4,8),(5,8),(6,8),(7,8),(8,9)]),
    (["Live connection failure", "Token provider failure", "Bounded reconnect + jitter", "30-second circuit cooldown", "Resume saved state", "Single recovery probe", "Recovered conversation", "Local spoken fallback / stop"], [(0,2),(1,3),(2,4),(3,5),(4,6),(5,6),(2,7),(5,7)]),
    (["Tool request + stable ID", "Guard + receipt lookup", "Saved result replay", "Bounded transaction", "Committed booking + receipt", "Timeout / lost response", "Query tool-status", "Same-ID retry: max 3 attempts", "Confirmed result", "Unknown: reconcile / handoff"], [(0,1),(1,2),(1,3),(3,4),(3,5),(5,6),(6,7),(6,8),(7,9)]),
    (["Storm arrival spike", "Carrier IVR + callback", "Global admission / recovery reserve", "Independent service cells", "Reserved voice capacity + alternate", "API / workflow / cached reads", "Durable queue + outbox", "Airline / SMS / payment adapters", "Reconciliation + human exceptions", "Durable state + audit"], [(0,1),(1,2),(2,3),(3,4),(3,5),(5,6),(6,7),(7,8),(7,9)]),
]


def diagram(labels, edges, index):
    height = ((len(labels) + 1) // 2) * 145 + 30
    def pos(i):
        return 35 + i % 2 * 515, 25 + i // 2 * 145
    out = [f'<svg role="img" aria-label="Architecture diagram {index + 1}" viewBox="0 0 1000 {height}" xmlns="http://www.w3.org/2000/svg"><defs><marker id="a{index}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#708899"/></marker></defs>']
    for a, b in edges:
        x, y = pos(a)
        u, v = pos(b)
        sx, sy, tx, ty = (x + 410, y + 40, u, v + 40) if a // 2 == b // 2 else (x + 205, y + 80, u + 205, v)
        out.append(f'<path d="M{sx} {sy} L{tx} {ty}" fill="none" stroke="#708899" stroke-width="1.5" marker-end="url(#a{index})"/>')
    for i, label in enumerate(labels):
        x, y = pos(i)
        out.append(f'<rect x="{x}" y="{y}" width="410" height="80" rx="8" fill="#edf4f8" stroke="#8aa7b9"/>')
        for j, line in enumerate(textwrap.wrap(label, 39)):
            out.append(f'<text x="{x + 205}" y="{y + 35 + j * 21}" text-anchor="middle" fill="#173b54" font-size="16">{html.escape(line)}</text>')
    return ''.join(out) + '</svg>'


def inline(text):
    text = html.escape(text)
    text = re.sub(r'\[([^]]+)\]\((https://[^)]+)\)', r'<a href="\2">\1</a>', text)
    return re.sub(r'`([^`]+)`', r'<code>\1</code>', text)


lines = (ROOT / 'docs/enterprise.md').read_text(encoding='utf-8').splitlines()
body, i, count = [], 0, 0
while i < len(lines):
    line = lines[i]
    if line.startswith('```mermaid'):
        i += 1
        while i < len(lines) and not lines[i].startswith('```'):
            i += 1
        body.append(diagram(*DIAGRAMS[count], count))
        count += 1
    elif line.startswith('# '):
        body.append('<h1>' + inline(line[2:]) + '</h1>')
    elif line.startswith('## '):
        body.append('<h2>' + inline(line[3:]) + '</h2>')
    elif line.startswith('|'):
        rows = []
        while i < len(lines) and lines[i].startswith('|'):
            if not re.match(r'^\|[\s|:-]+$', lines[i]):
                rows.append([inline(c.strip()) for c in lines[i].strip('|').split('|')])
            i += 1
        body.append('<div class="table"><table>' + ''.join('<tr>' + ''.join(f'<{"th" if k == 0 else "td"}>{c}</{"th" if k == 0 else "td"}>' for c in row) + '</tr>' for k, row in enumerate(rows)) + '</table></div>')
        i -= 1
    elif line.strip():
        body.append('<p>' + inline(line) + '</p>')
    i += 1
assert count == len(DIAGRAMS), 'Diagram summaries must match the guide'
style = 'body{font:17px/1.65 system-ui,sans-serif;color:#173046;background:#f5f7f9;margin:0}main{max-width:1100px;margin:auto;padding:54px 30px 100px}h1{font-size:42px;line-height:1.15}h2{margin-top:60px;padding-top:24px;border-top:2px solid #b3c5d0;font-size:26px}svg{width:100%;background:white;border:1px solid #c7d4dd;border-radius:12px;margin:20px 0}a{color:#075cad}table{border-collapse:collapse;width:100%;font-size:15px;background:white}th,td{padding:14px;border:1px solid #d6e0e7;text-align:left;vertical-align:top}th{background:#e4eef4}.table{overflow:auto}code{background:#e4eef4;padding:2px 5px}@media print{body{background:white}main{padding:0}svg{break-inside:avoid}h2{break-after:avoid}}'
(ROOT / 'docs/enterprise.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Voice agent architecture</title><style>' + style + '</style><main>' + ''.join(body) + '</main></html>', encoding='utf-8')
