#!/usr/bin/env python3
"""Rasterize one demo-mode frame to docs/demo.png.

Uses the real tile builder and the fictional fleet. No tailscale, SSH, or GPU
tools are invoked. Pillow is a documentation extra, not a runtime dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import overwatch  # noqa: E402
from fleet_overwatch.demo import build_demo_nodes  # noqa: E402

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover - docs helper
    raise SystemExit("pip install pillow  # needed only to regenerate docs/demo.png") from exc

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Regular.ttf",
    "/usr/share/fonts/truetype/macos/JetBrainsMono-Regular.ttf",
]

COLORS = {
    "text": (198, 230, 198),
    "label": (150, 255, 170),
    "dim": (118, 150, 122),
    "ok": (110, 255, 150),
    "warn": (214, 164, 255),
    "crit": (255, 96, 196),
    "frame": (78, 168, 104),
    "g0": (20, 110, 55),
    "g1": (30, 150, 60),
    "g2": (50, 190, 70),
    "g3": (90, 220, 90),
    "g4": (150, 240, 120),
    "g5": (190, 255, 160),
    "c0": (190, 110, 255),
    "c1": (255, 80, 210),
}

BG = (8, 16, 12)
PANEL = (10, 22, 16)
CHROME = (16, 28, 22)
INK = (186, 214, 190)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise SystemExit("no monospace font found for the demo screenshot")


def _tile_lines(node: dict) -> list[list[tuple[str, str]]]:
    top, content, bottom = overwatch.safe_tile(node)
    lines = [list(top)]
    for row in content:
        lines.append([("│", overwatch.FRAME), *row, ("│", overwatch.FRAME)])
    lines.append([(bottom, overwatch.FRAME)])
    return lines


def _pad_tile(lines: list[list[tuple[str, str]]], height: int) -> list[list[tuple[str, str]]]:
    gap = height - len(lines)
    if gap <= 0:
        return lines
    blank = [("│", overwatch.FRAME), (" " * overwatch.CW, overwatch.TEXT), ("│", overwatch.FRAME)]
    return lines[:-1] + [blank for _ in range(gap)] + [lines[-1]]


def _hstack(tiles: list[list[list[tuple[str, str]]]]) -> list[list[tuple[str, str]]]:
    height = max(len(tile) for tile in tiles)
    padded = [_pad_tile(tile, height) for tile in tiles]
    rows: list[list[tuple[str, str]]] = []
    for index in range(height):
        row: list[tuple[str, str]] = []
        for nth, tile in enumerate(padded):
            if nth:
                row.append(("  ", overwatch.TEXT))
            row.extend(tile[index])
        rows.append(row)
    return rows


def _width(runs: list[tuple[str, str]]) -> int:
    return sum(len(text) for text, _key in runs)


def _frame(bars: bool = True) -> list[list[tuple[str, str]]]:
    overwatch.DUAL_GAUGE = not bars
    overwatch.set_theme("smooth")
    overwatch.set_scheme("phosphor")
    overwatch.ORDER = ["node-a", "node-b", "node-c", "node-down", "node-off"]
    snapshot = build_demo_nodes(0)
    fleet_ok, fleet_total, pods, burn = overwatch.summary(snapshot)
    groups = overwatch.grouped(snapshot)
    columns = 3
    sections: list[tuple[str, list]] = []
    for title, members in groups:
        rows = [members[i:i + columns] for i in range(0, len(members), columns)]
        sections.append((title, rows))

    body: list[list[tuple[str, str]]] = []
    header = [
        ("FLEET OVERWATCH", overwatch.LABEL),
        ("  demo", overwatch.OK),
        (f"   {fleet_ok}/{fleet_total} fleet · {pods} pods · ${burn:.2f}/hr", overwatch.TEXT),
        ("    smooth · phosphor · " + ("bars" if bars else "gauge"), overwatch.DIM),
    ]
    body.append(header)
    body.append([])
    content_width = 0
    for title, rows in sections:
        built = [_hstack([_tile_lines(node) for node in row]) for row in rows]
        content_width = max(content_width, max(_width(line) for block in built for line in block))
        rule = f"── {title} " + "─" * max(0, content_width - len(title) - 4)
        body.append([(rule[:content_width], overwatch.DIM)])
        for block in built:
            body.extend(block)
        body.append([])
    totals = [("── TOTALS " + "─" * max(0, content_width - 10), overwatch.DIM)]
    body.append(totals)
    for label, gpu, cpu in overwatch.gauge_groups(snapshot):
        gpu_text = f"{gpu:.1f}%" if gpu is not None else "··"
        cpu_text = f"{cpu:.1f}%" if cpu is not None else "··"
        body.append([(f"  {label:<6} GPU {gpu_text:>7}   CPU {cpu_text:>7}", overwatch.TEXT)])
    return body


def render(bars: bool, destination: Path) -> None:
    font = _font(16)
    advance = font.getlength("M")
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + 4
    runs = _frame(bars)
    cols = max((_width(line) for line in runs), default=1)
    pad_x, pad_y = 28, 22
    chrome = 36
    width = int(pad_x * 2 + cols * advance) + 2
    height = int(chrome + pad_y * 2 + len(runs) * line_h) + 8
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((10, 8, width - 11, height - 9), radius=12, fill=PANEL, outline=(32, 64, 42))
    for index, color in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        draw.ellipse((26 + index * 18, 20, 36 + index * 18, 30), fill=color)
    title = "fleet-overwatch --demo"
    draw.text((88, 16), title, font=font, fill=INK)
    origin_y = chrome + pad_y
    for row_index, line in enumerate(runs):
        x = pad_x
        y = origin_y + row_index * line_h
        for text, key in line:
            draw.text((x, y), text, font=font, fill=COLORS.get(key, COLORS["text"]))
            x += advance * len(text)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, "PNG")
    print(f"wrote {destination} ({width}x{height})")


def main() -> None:
    render(True, ROOT / "docs" / "demo.png")


if __name__ == "__main__":
    main()
