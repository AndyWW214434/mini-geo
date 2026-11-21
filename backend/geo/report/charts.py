"""Small inline-SVG charts for the report: no JavaScript, prints cleanly to PDF."""

from __future__ import annotations

from datetime import date
from html import escape

PALETTE = ["#5b4fe0", "#d9480f", "#2b8a3e", "#868e96", "#1c7ed6"]


def heat(v: float | None) -> str:
    """Background tint for a visibility cell (0..1)."""
    if v is None:
        return "transparent"
    alpha = 0.08 + 0.5 * max(0.0, min(1.0, v))
    return f"rgba(91, 79, 224, {alpha:.2f})"


def line_chart(series: dict[str, list[tuple[date, float | None]]], names: dict[str, str], change_day: date,
               focus: str, width: int = 640, height: int = 190) -> str:
    pad_l, pad_r, pad_t, pad_b = 36, 120, 12, 26
    days = sorted({d for pts in series.values() for d, _ in pts})
    if not days:
        return ""
    x0, span = days[0], max(1, (days[-1] - days[0]).days)

    def x(d: date) -> float:
        return pad_l + (width - pad_l - pad_r) * (d - x0).days / span

    def y(v: float) -> float:
        return pad_t + (height - pad_t - pad_b) * (1 - v)

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" class="chart" xmlns="http://www.w3.org/2000/svg">']
    for g in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y(g):.1f}" y2="{y(g):.1f}" class="grid"/>'
                     f'<text x="{pad_l - 6}" y="{y(g) + 3:.1f}" class="axis" text-anchor="end">{g:.0%}</text>')
    cx = x(change_day)
    parts.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{pad_t}" y2="{height - pad_b}" class="change"/>'
                 f'<text x="{cx + 4:.1f}" y="{pad_t + 10}" class="axis">{change_day.day} {change_day:%b}</text>')
    for d in (days[0], days[-1]):
        parts.append(f'<text x="{x(d):.1f}" y="{height - 8}" class="axis" text-anchor="middle">'
                     f'{d.day} {d:%b}</text>')
    order = sorted(series, key=lambda b: b != focus)
    for i, brand in enumerate(order):
        color = PALETTE[i % len(PALETTE)]
        segs, cur = [], []
        for d, v in series[brand]:
            if v is None:  # gap for excluded (data-issue) days
                if cur:
                    segs.append(cur)
                cur = []
            else:
                cur.append(f"{x(d):.1f},{y(v):.1f}")
        if cur:
            segs.append(cur)
        w = 2.4 if brand == focus else 1.6
        for seg in segs:
            parts.append(f'<polyline points="{" ".join(seg)}" fill="none" stroke="{color}" stroke-width="{w}"/>')
        last = next((v for _, v in reversed(series[brand]) if v is not None), None)
        if last is not None:
            parts.append(f'<text x="{width - pad_r + 6}" y="{y(last) + 4:.1f}" class="label" fill="{color}">'
                         f'{escape(names[brand])}</text>')
    parts.append("</svg>")
    return "".join(parts)
