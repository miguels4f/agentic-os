#!/usr/bin/env python3
"""Render an exact-text, hand-drawn-style annotated system map from JSON."""

from __future__ import annotations

import argparse
import html
import json
import math
import sys
import textwrap
from pathlib import Path


PALETTE = {
    "entry": ("#DDEEFF", "#2D5B88"),
    "process": ("#FFF4CF", "#745B13"),
    "storage": ("#DDF3DF", "#376C3B"),
    "external": ("#EADFFF", "#65458D"),
    "risk": ("#FFE0DE", "#963D38"),
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def lines(text: str, width: int) -> list[str]:
    return textwrap.wrap(str(text), width=max(8, width), break_long_words=False) or [""]


def text_block(x: float, y: float, text: str, size: int, chars: int, weight: int = 400,
               color: str = "#24313D", line_height: float = 1.25) -> str:
    chunks = lines(text, chars)
    spans = []
    for index, chunk in enumerate(chunks):
        dy = "0" if index == 0 else f"{size * line_height:.1f}"
        spans.append(f'<tspan x="{x:.1f}" dy="{dy}">{esc(chunk)}</tspan>')
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
        f'fill="{color}" font-family="Chalkboard, Comic Sans MS, Bradley Hand, sans-serif">'
        + "".join(spans)
        + "</text>"
    )


def anchor(a: dict, b: dict) -> tuple[float, float, float, float]:
    ax, ay = a["x"] + a["w"] / 2, a["y"] + a["h"] / 2
    bx, by = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
    dx, dy = bx - ax, by - ay
    if abs(dx) >= abs(dy):
        if dx >= 0:
            return a["x"] + a["w"], ay, b["x"], by
        return a["x"], ay, b["x"] + b["w"], by
    if dy >= 0:
        return ax, a["y"] + a["h"], bx, b["y"]
    return ax, a["y"], bx, b["y"] + b["h"]


def curve(x1: float, y1: float, x2: float, y2: float) -> str:
    dx, dy = x2 - x1, y2 - y1
    bend = min(55.0, max(18.0, math.hypot(dx, dy) * 0.12))
    if abs(dx) >= abs(dy):
        sign = 1 if dx >= 0 else -1
        return f"M {x1:.1f} {y1:.1f} C {x1 + bend * sign:.1f} {y1 - 5:.1f}, {x2 - bend * sign:.1f} {y2 + 5:.1f}, {x2:.1f} {y2:.1f}"
    sign = 1 if dy >= 0 else -1
    return f"M {x1:.1f} {y1:.1f} C {x1 + 5:.1f} {y1 + bend * sign:.1f}, {x2 - 5:.1f} {y2 - bend * sign:.1f}, {x2:.1f} {y2:.1f}"


def render(spec: dict) -> str:
    width, height = int(spec.get("width", 1400)), int(spec.get("height", 850))
    nodes = []
    by_id = {}
    for raw in spec.get("nodes", []):
        node = dict(raw)
        for key, default in (("w", 250), ("h", 145)):
            node[key] = float(node.get(key, default))
        node["x"], node["y"] = float(node["x"]), float(node["y"])
        if node["id"] in by_id:
            raise ValueError(f"Duplicate node id: {node['id']}")
        nodes.append(node)
        by_id[node["id"]] = node
    if not spec.get("title") or not nodes:
        raise ValueError("The specification requires a title and at least one node")

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="map-title map-desc">',
        f'<title id="map-title">{esc(spec["title"])}</title>',
        f'<desc id="map-desc">{esc(spec.get("subtitle", "Annotated system map"))}</desc>',
        '<defs><filter id="paper"><feTurbulence type="fractalNoise" baseFrequency="0.7" numOctaves="2" seed="7" result="noise"/><feColorMatrix in="noise" type="saturate" values="0" result="mono"/><feComponentTransfer in="mono"><feFuncA type="table" tableValues="0 0.035"/></feComponentTransfer><feBlend in="SourceGraphic" mode="multiply"/></filter><marker id="arrow" markerWidth="12" markerHeight="12" refX="10" refY="5" orient="auto" markerUnits="strokeWidth"><path d="M 0 0 L 10 5 L 0 10 z" fill="#425466"/></marker></defs>',
        f'<rect width="{width}" height="{height}" fill="#FBFAF6" filter="url(#paper)"/>',
        text_block(55, 58, spec["title"], 32, 68, 700, "#162431"),
    ]
    if spec.get("subtitle"):
        out.append(text_block(57, 92, spec["subtitle"], 17, 92, 400, "#556575"))

    for edge in spec.get("edges", []):
        if edge.get("from") not in by_id or edge.get("to") not in by_id:
            raise ValueError(f"Edge references an unknown node: {edge}")
        x1, y1, x2, y2 = anchor(by_id[edge["from"]], by_id[edge["to"]])
        dash = ' stroke-dasharray="10 8"' if edge.get("style") == "dashed" else ""
        path = curve(x1, y1, x2, y2)
        out.append(f'<path d="{path}" fill="none" stroke="#425466" stroke-width="3" stroke-linecap="round" marker-end="url(#arrow)"{dash}/>')
        if edge.get("label"):
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2 - 9
            out.append(f'<rect x="{mx - 74:.1f}" y="{my - 17:.1f}" width="148" height="24" rx="8" fill="#FBFAF6" opacity="0.94"/>')
            out.append(f'<text x="{mx:.1f}" y="{my:.1f}" text-anchor="middle" font-size="13" fill="#425466" font-family="Chalkboard, Comic Sans MS, sans-serif">{esc(edge["label"])}</text>')

    for index, node in enumerate(nodes):
        fill, stroke = PALETTE.get(node.get("kind", "process"), PALETTE["process"])
        x, y, w, h = node["x"], node["y"], node["w"], node["h"]
        rotate = (-0.35, 0.25, -0.15, 0.32)[index % 4]
        out.append(f'<g transform="rotate({rotate} {x + w / 2:.1f} {y + h / 2:.1f})">')
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="18" fill="{fill}" stroke="{stroke}" stroke-width="3"/>')
        out.append(f'<rect x="{x + 3}" y="{y - 2}" width="{w - 4}" height="{h + 3}" rx="18" fill="none" stroke="{stroke}" stroke-width="1.2" opacity="0.45"/>')
        if node.get("step"):
            out.append(f'<circle cx="{x + 27}" cy="{y + 27}" r="17" fill="{stroke}"/><text x="{x + 27}" y="{y + 33}" text-anchor="middle" font-size="17" font-weight="700" fill="white" font-family="sans-serif">{esc(node["step"])}</text>')
        title_x = x + (55 if node.get("step") else 20)
        title_chars = max(12, int((w - (title_x - x) - 16) / 10))
        title_lines = lines(node["title"], title_chars)
        out.append(text_block(title_x, y + 34, node["title"], 18, title_chars, 700, "#1C2935"))
        # A wrapped title pushes the body down instead of overlapping it.
        body_y = y + 70 + (len(title_lines) - 1) * 18 * 1.25
        body_chars = max(16, int((w - 40) / 8.2))
        body_value = node.get("body", "")
        body_lines = lines(body_value, body_chars)
        out.append(text_block(x + 20, body_y, body_value, 14, body_chars, 400, "#344553", 1.35))
        details = node.get("details", [])
        if not details:
            body_end = body_y + (len(body_lines) - 1) * 14 * 1.35
            limit = y + h - (34 if node.get("badge") else 14)
            if body_end > limit:
                needed = int(body_end - y + (34 if node.get("badge") else 14))
                raise ValueError(
                    f"Node {node['id']!r} title and body need about {needed}px height; declared {int(h)}px"
                )
        if details:
            if not isinstance(details, list):
                raise ValueError(f"Node {node['id']!r} details must be a list")
            cursor = body_y + len(body_lines) * 18.9 + 17
            out.append(f'<line x1="{x + 18:.1f}" y1="{cursor - 10:.1f}" x2="{x + w - 18:.1f}" y2="{cursor - 10:.1f}" stroke="{stroke}" stroke-width="1.3" opacity="0.42"/>')
            detail_chars = max(18, int((w - 40) / 7.4))
            for raw_detail in details:
                if not isinstance(raw_detail, dict) or not raw_detail.get("label"):
                    raise ValueError(f"Node {node['id']!r} has an invalid detail row")
                label = str(raw_detail["label"]).upper()
                value = str(raw_detail.get("value", ""))
                value_lines = lines(value, detail_chars)
                out.append(text_block(x + 20, cursor, label, 11, detail_chars, 700, stroke, 1.15))
                cursor += 18
                out.append(text_block(x + 20, cursor, value, 12, detail_chars, 400, "#344553", 1.24))
                cursor += len(value_lines) * 14.9 + 10
            if cursor > y + h - 34:
                needed = int(cursor - y + 34)
                raise ValueError(
                    f"Node {node['id']!r} details need about {needed}px height; declared {int(h)}px"
                )
        if node.get("badge"):
            badge = str(node["badge"]).upper()
            out.append(f'<rect x="{x + w - 92}" y="{y + h - 29}" width="78" height="20" rx="10" fill="white" opacity="0.72"/><text x="{x + w - 53}" y="{y + h - 15}" text-anchor="middle" font-size="10" font-weight="700" fill="{stroke}" font-family="sans-serif">{esc(badge)}</text>')
        out.append("</g>")

    for note in spec.get("notes", []):
        x, y = float(note["x"]), float(note["y"])
        w, h = float(note.get("w", 330)), float(note.get("h", 120))
        out.append(f'<g transform="rotate(-0.7 {x + w / 2:.1f} {y + h / 2:.1f})"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" fill="#FFF0A8" stroke="#9D8736" stroke-width="2"/>')
        if note.get("title"):
            out.append(text_block(x + 18, y + 29, note["title"], 16, max(12, int((w - 36) / 9)), 700))
        out.append(text_block(x + 18, y + (56 if note.get("title") else 31), note["body"], 14, max(16, int((w - 36) / 8.1))))
        out.append("</g>")

    if spec.get("legend", True):
        legend = [("entry", "User / entry"), ("process", "Internal step"), ("storage", "Stored / result"), ("external", "External service"), ("risk", "Risk / failure")]
        lx, ly = 58, height - 42
        for kind, label in legend:
            fill, stroke = PALETTE[kind]
            out.append(f'<rect x="{lx}" y="{ly - 14}" width="18" height="18" rx="5" fill="{fill}" stroke="{stroke}"/><text x="{lx + 25}" y="{ly}" font-size="12" fill="#4B5B69" font-family="sans-serif">{label}</text>')
            lx += 165
    out.append("</svg>")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="Input JSON map specification")
    parser.add_argument("output", type=Path, help="Output SVG path")
    args = parser.parse_args()
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        svg = render(spec)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(svg, encoding="utf-8")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"render_map: {exc}", file=sys.stderr)
        return 1
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
