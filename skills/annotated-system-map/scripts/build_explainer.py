#!/usr/bin/env python3
"""Build a navigable, single-file HTML architecture explainer from a content module.

Usage:  python3 build_explainer.py <content.py> <output.html>

The content module must define a dict named DOC. The contract is documented in
references/explainer-content.md; a short worked example lives in
references/example-explainer-content.py. Standard library only.
"""
from __future__ import annotations

import html
import importlib.util
import re
import sys
from pathlib import Path

KINDS = {"entry", "process", "storage", "external", "risk"}
BADGES = {"O": ("Observed", "obs", "verified in code or configuration"),
          "I": ("Inferred", "inf", "strongly implied, not proven"),
          "U": ("Unknown", "unk", "cannot be established from the project")}
DEFAULT_LABELS = {
    "simple": "In plain words", "say": "Say it like this", "tip": "Tip", "why_matters": "Why it matters",
    "choice": "Choice", "alt": "Alternatives considered", "why": "Why", "tradeoff": "Trade-off (the cost)",
    "nav_short": "Short version", "h_short": "The short version",
    "nav_journey": "The journey", "h_journey": "The journey (click any box)",
    "legend_colors": ["Blue = entry / human", "Yellow = processing", "Green = stored result",
                      "Purple = external / optional", "Red = control / risk", "Dashed = side path"],
    "nav_blocks": "Blocks", "h_blocks": "Each block: who does what, what goes in and out",
    "blocks_intro": "Every block lists its <em>contract</em>: the executor (who or what does the work), the input it accepts, "
                    "the output it must produce, the restrictions it runs under, and how it hands off. Badges say how sure we are.",
    "nav_decisions": "Decisions", "h_decisions": "Architecture decisions and why",
    "decisions_intro": "For each choice: what was chosen, what else was possible, why, and what it costs.",
    "nav_controls": "Safety controls", "h_controls": "Safety and reliability controls",
    "controls_cols": ["Control", "What it does", "Evidence"],
    "nav_stack": "Tech stack", "h_stack": "Tech stack", "stack_cols": ["Layer", "Choice"],
    "nav_numbers": "Numbers", "h_numbers": "Numbers you can quote", "numbers_cols": ["Item", "Value", "Source"],
    "nav_limits": "Limitations", "h_limits": "Honest limitations",
    "nav_qa": "Likely questions", "h_qa": "Likely questions",
    "nav_glossary": "Glossary", "h_glossary": "Glossary",
    "nav_about": "About this document", "h_about": "About this document",
    "expand": "Expand all", "collapse": "Collapse all", "theme": "Light / dark",
    "badge_O": "Observed", "badge_I": "Inferred", "badge_U": "Unknown",
    "badge_legend": "Badges show confidence: verified in code, strongly implied, or unknown.",
}
LEGACY_LABEL_KEYS = {"simple": "simple_label", "say": "say_label", "tip": "tip_label"}


def lab(doc: dict, key: str):
    """Label lookup: DOC['labels'] overrides, then legacy *_label keys, then English defaults."""
    custom = doc.get("labels", {})
    if key in custom:
        return custom[key]
    if key in LEGACY_LABEL_KEYS and doc.get(LEGACY_LABEL_KEYS[key]):
        return doc[LEGACY_LABEL_KEYS[key]]
    return DEFAULT_LABELS[key]


REQUIRED_TOP = ["title", "subtitle", "pitch", "journey_intro", "flow_rows", "blocks", "decisions"]
REQUIRED_BLOCK = ["id", "kind", "short", "one", "title", "details"]
REQUIRED_DECISION = ["title", "choice", "alt", "why", "tradeoff"]


def E(s) -> str:
    return html.escape(str(s), quote=True)


def _inline(s: str) -> str:
    """Apply `code`, **bold** and *italics*; code spans are protected from the other two."""
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    s = re.sub(r"`(.+?)`", stash, s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])", r"<em>\1</em>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>", s)


def rich(s) -> str:
    """Text fields: escape, then apply `code`, **bold** and *italics*."""
    return _inline(E(s))


def md(s) -> str:
    """Trusted HTML fields (subtitle, context, journey_intro, about): keep tags, same inline markup."""
    return _inline(str(s))


def validate(doc: dict) -> list[str]:
    problems = [f"DOC is missing required key '{k}'" for k in REQUIRED_TOP if not doc.get(k)]
    ids = set()
    for i, b in enumerate(doc.get("blocks", []), 1):
        for k in REQUIRED_BLOCK:
            if not b.get(k):
                problems.append(f"block #{i} ({b.get('id', '?')}) is missing '{k}'")
        if b.get("kind") and b["kind"] not in KINDS:
            problems.append(f"block {b.get('id')} has kind '{b['kind']}'; use one of {sorted(KINDS)}")
        if b.get("badge", "O") not in BADGES:
            problems.append(f"block {b.get('id')} badge must be O, I or U")
        for row in b.get("details", []):
            if not (isinstance(row, (list, tuple)) and len(row) == 2):
                problems.append(f"block {b.get('id')} details rows must be (label, value) pairs")
        if b.get("id") in ids:
            problems.append(f"duplicate block id '{b['id']}'")
        ids.add(b.get("id"))
    for row in doc.get("flow_rows", []):
        for sid in row:
            if sid not in ids:
                problems.append(f"flow_rows references unknown block '{sid}'")
    for s in doc.get("side", []):
        if s.get("id") not in ids:
            problems.append(f"side path '{s.get('id')}' needs a matching block (for its detail card)")
    for i, d in enumerate(doc.get("decisions", []), 1):
        for k in REQUIRED_DECISION:
            if not d.get(k):
                problems.append(f"decision D{i} ({d.get('title', '?')}) is missing '{k}'")
        if d.get("badge", "O") not in BADGES:
            problems.append(f"decision D{i} badge must be O, I or U")
    for key in doc.get("labels", {}):
        if key not in DEFAULT_LABELS:
            problems.append(f"unknown label key '{key}' (see DEFAULT_LABELS in build_explainer.py)")
    return problems


def _words(text) -> int:
    return len(re.findall(r"[\w'’]+", str(text)))


def warnings(doc: dict) -> list[str]:
    """Length and shape limits from the contract: reported, but they do not block the build."""
    out = []
    for item in list(doc.get("blocks", [])) + list(doc.get("side", [])):
        if _words(item.get("short", "")) > 4:
            out.append(f"'{item.get('id')}' short label has {_words(item['short'])} words (aim for 3)")
        if _words(item.get("one", "")) > 12:
            out.append(f"'{item.get('id')}' one-liner has {_words(item['one'])} words (aim for 10)")
    for i, row in enumerate(doc.get("flow_rows", []), 1):
        if len(row) > 5:
            out.append(f"flow row {i} has {len(row)} boxes (max 5)")
    pitch = _words(doc.get("pitch", ""))
    if pitch and not 40 <= pitch <= 110:
        out.append(f"pitch has {pitch} words (aim for 60-100)")
    n = len(doc.get("decisions", []))
    if n and not 5 <= n <= 15:
        out.append(f"{n} decisions (aim for 8-15; fewer only for a genuinely small system)")
    return out


def badge(code: str, doc: dict | None = None) -> str:
    label, cls, meaning = BADGES.get(code, BADGES["O"])
    if doc is not None:
        label = lab(doc, f"badge_{code if code in BADGES else 'O'}")
    return f'<span class="badge {cls}" title="{label}: {meaning}">{label}</span>'


def flow(doc: dict) -> str:
    by_id = {b["id"]: b for b in doc["blocks"]}
    rows = []
    for row in doc["flow_rows"]:
        cells = []
        for i, sid in enumerate(row):
            b = by_id[sid]
            if i:
                cells.append('<div class="arrow" aria-hidden="true">→</div>')
            cells.append(
                f'<a class="node k-{b["kind"]}" href="#b-{sid}">'
                f'<span class="num">{E(b.get("step", ""))}</span>'
                f'<span class="ntitle">{E(b["short"])}</span>'
                f'<span class="nbody">{E(b["one"])}</span></a>')
        rows.append('<div class="flowrow">' + "".join(cells) + "</div>")
    side = ""
    if doc.get("side"):
        side = '<div class="siderow">' + "".join(
            f'<a class="node k-side" href="#b-{s["id"]}"><span class="num">{E(by_id[s["id"]].get("step", ""))}</span>'
            f'<span class="ntitle">{E(s["short"])}</span>'
            f'<span class="nbody">{E(s["one"])}</span></a>' for s in doc["side"]) + "</div>"
    return '<div class="flow">' + '<div class="rowsep" aria-hidden="true">↓</div>'.join(rows) + side + "</div>"


def simple_line(doc: dict, text) -> str:
    if not text:
        return ""
    return f'<p class="simple"><span class="tag">{E(lab(doc, "simple"))}</span> {rich(text)}</p>'


def block_card(doc: dict, b: dict) -> str:
    rows = "".join(f"<tr><th>{E(k)}</th><td>{rich(v)}</td></tr>" for k, v in b["details"])
    why = f'<p class="why"><span class="tag t2">{E(lab(doc, "why_matters"))}</span> {rich(b["why"])}</p>' if b.get("why") else ""
    return f'''
<details class="card" id="b-{E(b["id"])}" open>
  <summary><span class="num k-{b["kind"]}">{E(b.get("step", "•"))}</span>
    <span class="ctitle">{E(b["title"])}</span>{badge(b.get("badge", "O"), doc)}</summary>
  {simple_line(doc, b.get("simple"))}
  <table class="contract">{rows}</table>
  {why}
</details>'''


def decision_card(doc: dict, i: int, d: dict) -> str:
    say = ""
    if d.get("say"):
        say = f'<blockquote class="say"><span class="tag t3">{E(lab(doc, "say"))}</span> “{rich(d["say"])}”</blockquote>'
    return f'''
<details class="card dec" id="d-{i}">
  <summary><span class="num k-dec">D{i}</span><span class="ctitle">{E(d["title"])}</span>{badge(d.get("badge", "O"), doc)}</summary>
  {simple_line(doc, d.get("simple"))}
  <dl>
    <dt>{E(lab(doc, "choice"))}</dt><dd>{rich(d["choice"])}</dd>
    <dt>{E(lab(doc, "alt"))}</dt><dd>{rich(d["alt"])}</dd>
    <dt>{E(lab(doc, "why"))}</dt><dd>{rich(d["why"])}</dd>
    <dt>{E(lab(doc, "tradeoff"))}</dt><dd>{rich(d["tradeoff"])}</dd>
  </dl>
  {say}
</details>'''


def qa_card(doc: dict, i: int, q: dict) -> str:
    tip = ""
    if q.get("tip"):
        tip = f'<p class="simple"><span class="tag">{E(lab(doc, "tip"))}</span> {rich(q["tip"])}</p>'
    return f'''
<details class="card qa">
  <summary><span class="num k-qa">Q{i}</span><span class="ctitle">{E(q["q"])}</span></summary>
  <blockquote class="say">{rich(q["a"])}</blockquote>
  {tip}
</details>'''


def table(headers: list[str], rows, bold_first=True, bold_col=None) -> str:
    head = "".join(f"<th>{E(h)}</th>" for h in headers)
    body = ""
    for row in rows:
        cells = []
        for j, cell in enumerate(row):
            content = rich(cell)
            if (bold_first and j == 0) or (bold_col is not None and j == bold_col):
                content = f"<strong>{content}</strong>"
            cells.append(f"<td>{content}</td>")
        body += "<tr>" + "".join(cells) + "</tr>"
    return f'<div class="tablewrap"><table class="grid"><tr>{head}</tr>{body}</table></div>'


CSS = r"""
:root{--bg:#f7f5f0;--panel:#fff;--ink:#1f2933;--muted:#5b6670;--line:#e3ded3;--accent:#2d5b88;
--entry:#dbeafe;--entryb:#2d5b88;--proc:#fff4cf;--procb:#8a6d16;--store:#dcf3df;--storeb:#2f6b36;
--ext:#eadfff;--extb:#65458d;--risk:#ffe0de;--riskb:#963d38;--side:#eef0f3;--sideb:#6b7480;
--obs:#dcf3df;--obsi:#2f6b36;--inf:#fff1c2;--infi:#7a5a00;--unk:#ffe0de;--unki:#963d38;--code:#f0ede6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#15181c;--panel:#1d2227;--ink:#e6e9ec;--muted:#9aa5b0;--line:#2e353d;--accent:#8ab8e6;
--entry:#1c3350;--entryb:#8ab8e6;--proc:#3a3218;--procb:#e2c46a;--store:#1d3a22;--storeb:#8fd19a;--ext:#2f2546;--extb:#c4a8f0;--risk:#47201e;--riskb:#f0a29c;--side:#262c33;--sideb:#a8b2bc;
--obs:#1d3a22;--obsi:#8fd19a;--inf:#3a3218;--infi:#e2c46a;--unk:#47201e;--unki:#f0a29c;--code:#262c33}}
:root[data-theme="dark"]{--bg:#15181c;--panel:#1d2227;--ink:#e6e9ec;--muted:#9aa5b0;--line:#2e353d;--accent:#8ab8e6;
--entry:#1c3350;--entryb:#8ab8e6;--proc:#3a3218;--procb:#e2c46a;--store:#1d3a22;--storeb:#8fd19a;--ext:#2f2546;--extb:#c4a8f0;--risk:#47201e;--riskb:#f0a29c;--side:#262c33;--sideb:#a8b2bc;
--obs:#1d3a22;--obsi:#8fd19a;--inf:#3a3218;--infi:#e2c46a;--unk:#47201e;--unki:#f0a29c;--code:#262c33}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
.layout{display:grid;grid-template-columns:250px minmax(0,1fr);max-width:1320px;margin:0 auto}
nav{position:sticky;top:0;height:100vh;overflow:auto;padding:24px 18px;border-right:1px solid var(--line)}
nav h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 10px}
nav a{display:block;color:var(--ink);text-decoration:none;padding:5px 8px;border-radius:6px;font-size:14px}
nav a:hover{background:var(--panel)}nav .sub a{padding-left:20px;font-size:13px;color:var(--muted)}
nav .tools{margin-top:18px;display:flex;flex-wrap:wrap;gap:6px}
button{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;padding:4px 8px;cursor:pointer}
main{padding:28px 36px 80px;min-width:0}
header h1{font-size:30px;margin:0 0 6px}header p.lead{font-size:18px;color:var(--muted);margin:0 0 14px;max-width:820px}
section{margin-top:44px;scroll-margin-top:16px}section>h2{font-size:22px;border-bottom:2px solid var(--line);padding-bottom:6px}
.pitch{background:var(--panel);border-left:4px solid var(--accent);padding:14px 18px;border-radius:8px;max-width:900px}
.legend{display:flex;flex-wrap:wrap;gap:10px;font-size:13px;color:var(--muted);margin:10px 0}
.badge{font-size:11px;font-weight:600;padding:2px 7px;border-radius:999px;margin-left:auto;white-space:nowrap}
.legend .badge{margin-left:0}
.obs{background:var(--obs);color:var(--obsi)}.inf{background:var(--inf);color:var(--infi)}.unk{background:var(--unk);color:var(--unki)}
.flow{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;overflow-x:auto}
.flowrow,.siderow{display:flex;align-items:stretch;gap:8px;min-width:min-content}
.siderow{margin-top:18px;padding-top:14px;border-top:1px dashed var(--line);gap:14px}
.rowsep{text-align:center;color:var(--muted);font-size:20px;margin:6px 0}
.arrow{align-self:center;color:var(--muted);font-size:22px}
.node{flex:1 1 0;min-width:130px;display:flex;flex-direction:column;gap:4px;text-decoration:none;color:var(--ink);
border:2px solid;border-radius:10px;padding:10px 12px;transition:transform .1s}
.node:hover{transform:translateY(-2px)}
.node .num{font-weight:700;font-size:12px;opacity:.8}.ntitle{font-weight:650;font-size:14px;line-height:1.25}.nbody{font-size:12.5px;color:var(--muted);line-height:1.35}
.k-entry{background:var(--entry);border-color:var(--entryb)}.k-process{background:var(--proc);border-color:var(--procb)}
.k-storage{background:var(--store);border-color:var(--storeb)}.k-external{background:var(--ext);border-color:var(--extb)}
.k-risk{background:var(--risk);border-color:var(--riskb)}.k-side{background:var(--side);border-color:var(--sideb);border-style:dashed}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:0 16px;margin:12px 0;scroll-margin-top:16px}
.card[open]{padding-bottom:14px}
.card summary{display:flex;align-items:center;gap:10px;cursor:pointer;padding:12px 0;list-style:none}
.card summary::-webkit-details-marker{display:none}
.card .num{flex:none;min-width:30px;height:30px;padding:0 6px;border-radius:15px;display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;border:2px solid}
.k-dec{background:var(--ext);border-color:var(--extb)}.k-qa{background:var(--entry);border-color:var(--entryb)}
.ctitle{font-weight:650;font-size:16px}
.tag{display:inline-block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;background:var(--code);padding:1px 7px;border-radius:4px;margin-right:6px;color:var(--muted)}
.t2{background:var(--risk);color:var(--riskb)}.t3{background:var(--store);color:var(--storeb)}
.simple{margin:4px 0 10px}
table.contract{width:100%;border-collapse:collapse;font-size:14px}
.contract th{text-align:left;vertical-align:top;width:150px;color:var(--muted);font-weight:600;padding:6px 10px 6px 0;border-top:1px solid var(--line)}
.contract td{padding:6px 0;border-top:1px solid var(--line)}
dl{display:grid;grid-template-columns:190px 1fr;gap:6px 14px;font-size:14.5px;margin:6px 0}
dt{color:var(--muted);font-weight:600}dd{margin:0}
blockquote.say{margin:10px 0 0;padding:10px 14px;border-left:4px solid var(--storeb);background:var(--code);border-radius:6px;font-style:italic}
code{font:13px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere}
ul.plain{padding-left:20px}ul.plain li{margin:4px 0}
table.grid{width:100%;border-collapse:collapse;font-size:14px;background:var(--panel);border-radius:8px;overflow:hidden}
.grid th,.grid td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
.grid th{background:var(--code)}
.tablewrap{overflow-x:auto}
.warn{background:var(--risk);border-left:4px solid var(--riskb);padding:12px 16px;border-radius:8px}
nav a{overflow-wrap:anywhere}
@media (max-width:1100px){.layout{grid-template-columns:minmax(0,1fr)}nav .sub{display:none}
nav{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}main{padding:24px 20px 60px}}
@media (max-width:900px){main{padding:20px 16px 60px}.flowrow,.siderow{flex-direction:column}.arrow{transform:rotate(90deg)}
dl{grid-template-columns:1fr}.contract th{width:110px}}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}.node{transition:none}}
"""

JS = r"""
document.querySelectorAll('[data-act]').forEach(b=>b.addEventListener('click',()=>{
 const open=b.dataset.act==='open';document.querySelectorAll('details.card').forEach(d=>d.open=open)}));
document.getElementById('theme').addEventListener('click',()=>{const r=document.documentElement;
 const cur=r.dataset.theme||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');r.dataset.theme=cur==='dark'?'light':'dark'});
document.querySelectorAll('a[href^="#b-"],a[href^="#d-"]').forEach(a=>a.addEventListener('click',()=>{
 const t=document.querySelector(a.getAttribute('href'));if(t&&t.tagName==='DETAILS')t.open=true}));
"""


def build(doc: dict) -> str:
    nav, sections = [], []

    def add(sid: str, nav_label: str, body: str, sub: str = "") -> None:
        nav.append(f'<a href="#{sid}">{E(nav_label)}</a>' + (f'<div class="sub">{sub}</div>' if sub else ""))
        sections.append(body)

    add("pitch", lab(doc, "nav_short"), f'''<section id="pitch"><h2>{E(lab(doc, "h_short"))}</h2>
<div class="pitch"><p>{rich(doc["pitch"])}</p></div>{simple_line(doc, doc.get("pitch_simple"))}
{"".join(f'<p>{md(p)}</p>' for p in doc.get("context", []))}</section>''')

    add("journey", lab(doc, "nav_journey"), f'''<section id="journey"><h2>{E(lab(doc, "h_journey"))}</h2>
<p>{md(doc["journey_intro"])}</p>
<div class="legend">{"".join(f"<span>{E(x)}</span>" for x in lab(doc, "legend_colors"))}</div>
{flow(doc)}</section>''')

    add("blocks", lab(doc, "nav_blocks"),
        f'<section id="blocks"><h2>{E(lab(doc, "h_blocks"))}</h2><p>{md(lab(doc, "blocks_intro"))}</p>'
        + "".join(block_card(doc, b) for b in doc["blocks"]) + "</section>",
        "".join(f'<a href="#b-{E(b["id"])}">{E(b.get("step", ""))}. {E(b["short"])}</a>' for b in doc["blocks"]))

    add("decisions", lab(doc, "nav_decisions"),
        f'<section id="decisions"><h2>{E(lab(doc, "h_decisions"))}</h2><p>{md(lab(doc, "decisions_intro"))}</p>'
        + "".join(decision_card(doc, i, d) for i, d in enumerate(doc["decisions"], 1)) + "</section>",
        "".join(f'<a href="#d-{i}">D{i}. {E(d["title"])}</a>' for i, d in enumerate(doc["decisions"], 1)))

    if doc.get("controls"):
        add("controls", lab(doc, "nav_controls"), f'<section id="controls"><h2>{E(lab(doc, "h_controls"))}</h2>'
            + table(lab(doc, "controls_cols"), doc["controls"]) + "</section>")
    if doc.get("stack"):
        add("stack", lab(doc, "nav_stack"), f'<section id="stack"><h2>{E(lab(doc, "h_stack"))}</h2>' + table(lab(doc, "stack_cols"), doc["stack"]) + "</section>")
    if doc.get("numbers"):
        add("numbers", lab(doc, "nav_numbers"), f'<section id="numbers"><h2>{E(lab(doc, "h_numbers"))}</h2>'
            + table(lab(doc, "numbers_cols"), doc["numbers"], bold_first=False, bold_col=1) + "</section>")
    if doc.get("limits"):
        add("limits", lab(doc, "nav_limits"), f'<section id="limits"><h2>{E(lab(doc, "h_limits"))}</h2><div class="warn"><ul class="plain">'
            + "".join(f"<li>{rich(x)}</li>" for x in doc["limits"]) + "</ul></div></section>")
    if doc.get("fit"):
        title = doc.get("fit_title", "How this maps to the goal")
        add("fit", title, f'<section id="fit"><h2>{E(title)}</h2><ul class="plain">'
            + "".join(f"<li>{rich(x)}</li>" for x in doc["fit"]) + "</ul></section>")
    if doc.get("qa"):
        add("qa", lab(doc, "nav_qa"), f'<section id="qa"><h2>{E(lab(doc, "h_qa"))}</h2>'
            + "".join(qa_card(doc, i, q) for i, q in enumerate(doc["qa"], 1)) + "</section>")
    if doc.get("glossary"):
        add("glossary", lab(doc, "nav_glossary"), f'<section id="glossary"><h2>{E(lab(doc, "h_glossary"))}</h2><dl>'
            + "".join(f"<dt>{E(a)}</dt><dd>{rich(b)}</dd>" for a, b in doc["glossary"]) + "</dl></section>")
    if doc.get("about"):
        add("about", lab(doc, "nav_about"), f'<section id="about"><h2>{E(lab(doc, "h_about"))}</h2><p>{md(doc["about"])}</p></section>')

    legend = "".join(badge(c, doc) for c in BADGES)
    return f'''<!doctype html><html lang="{E(doc.get("lang", "en"))}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(doc["title"])}</title>
<style>{CSS}</style></head><body><div class="layout">
<nav><h2>{E(doc["title"])}</h2>{"".join(nav)}
<div class="tools"><button data-act="open">{E(lab(doc, "expand"))}</button><button data-act="close">{E(lab(doc, "collapse"))}</button><button id="theme">{E(lab(doc, "theme"))}</button></div></nav>
<main><header><h1>{E(doc["title"])}</h1><p class="lead">{md(doc["subtitle"])}</p>
<div class="legend">{legend}<span>{E(lab(doc, "badge_legend"))}</span></div></header>
{"".join(sections)}</main></div><script>{JS}</script></body></html>'''


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    src, out = Path(argv[1]), Path(argv[2])
    spec = importlib.util.spec_from_file_location("explainer_content", src)
    if spec is None or spec.loader is None:
        print(f"build_explainer: cannot load {src}", file=sys.stderr)
        return 2
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    doc = getattr(module, "DOC", None)
    if not isinstance(doc, dict):
        print("build_explainer: the content module must define a dict named DOC", file=sys.stderr)
        return 2
    problems = validate(doc)
    for w in [] if problems else warnings(doc):
        print(f"build_explainer: warning: {w}", file=sys.stderr)
    if problems:
        print("build_explainer: content does not meet the contract:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(doc), encoding="utf-8")
    print(f"{out} ({out.stat().st_size} bytes, {len(doc['blocks'])} blocks, {len(doc['decisions'])} decisions)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
