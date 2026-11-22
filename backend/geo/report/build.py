"""Build the customer report from the marts and the latest insights batch.

Every number in the report is read from mart.* or mart.insights at build time; nothing is
typed by hand. The prose is assembled from those numbers by small, explicit functions so a
reviewer can trace each sentence to a query.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from jinja2 import Environment, FileSystemLoader, select_autoescape

from geo.config import settings
from geo.db import connection
from geo.report import charts

TEMPLATES = Path(__file__).parent
CURRENT_DAYS = 7


# ------------------------------------------------------------------------------ formatting
def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def pts(x: float) -> str:
    return f"{abs(x) * 100:.0f} points"


def day(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


# ------------------------------------------------------------------------------ data access
@dataclass
class Ctx:
    conn: psycopg.Connection
    focus: str
    names: dict[str, str]
    platforms: dict[str, str]
    owners: dict[str, str]
    focus_categories: list[str]
    data_start: date
    data_end: date
    bad_days: dict[str, set[date]] = field(default_factory=dict)


def _ctx(conn: psycopg.Connection, focus: str) -> Ctx:
    names = {r["brand_id"]: r["name"] for r in conn.execute("select brand_id, name from config.brands")}
    if focus not in names:
        raise ValueError(f"unknown brand {focus!r}")
    platforms = {r["platform_id"]: r["name"] for r in conn.execute(
        """select platform_id, name from config.platforms p
           where exists (select 1 from mart.daily_visibility v where v.platform_id = p.platform_id
                         and v.real_answers > 0)
           order by platform_id""")}  # assistants with real answers in the data, active or not
    owners = {d: r["brand_id"] for r in conn.execute("select brand_id, domains from config.brands")
              for d in r["domains"]}
    cats = [r["category"] for r in conn.execute(
        "select category from config.brand_categories where brand_id = %s order by category", (focus,))]
    span = conn.execute("select min(day) s, max(day) e from mart.daily_category_visibility").fetchone()
    return Ctx(conn, focus, names, platforms, owners, cats, span["s"], span["e"])


def _standing(ctx: Ctx) -> dict[str, Any]:
    """Last-7-day pooled visibility per platform x category, excluding data-issue days."""
    start = max(ctx.data_start, ctx.data_end - timedelta(days=CURRENT_DAYS - 1))
    rows = ctx.conn.execute(
        """select platform_id, category, brand_id, sum(mentioning_answers) k, sum(real_answers) n,
                  sum(avg_position * mentioning_answers) / nullif(sum(mentioning_answers), 0) pos
           from mart.daily_category_visibility
           where day between %s and %s and category = any(%s)
           group by 1, 2, 3""", (start, ctx.data_end, ctx.focus_categories)).fetchall()
    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if r["n"]:
            cells[(r["platform_id"], r["category"])].append(
                {"brand": r["brand_id"], "vis": r["k"] / r["n"], "n": r["n"], "pos": r["pos"]})
    table = []
    for p in ctx.platforms:
        row = {"platform": ctx.platforms[p], "platform_id": p, "cells": []}
        for c in ctx.focus_categories:
            ranked = sorted(cells.get((p, c), []), key=lambda x: -x["vis"])
            me = next((x for x in ranked if x["brand"] == ctx.focus), None)
            rank = ranked.index(me) + 1 if me else None
            leader = ranked[0] if ranked else None
            row["cells"].append({
                "vis": me["vis"] if me else None, "rank": rank, "n": me["n"] if me else 0,
                "pos": float(me["pos"]) if me and me["pos"] else None,
                "leader": ctx.names[leader["brand"]] if leader and leader["brand"] != ctx.focus else None,
                "leader_vis": leader["vis"] if leader else None,
                "gap": (me["vis"] - ranked[1]["vis"]) if me and rank == 1 and len(ranked) > 1 else None,
                "runner_up": ctx.names[ranked[1]["brand"]] if me and rank == 1 and len(ranked) > 1 else None})
        table.append(row)
    overall = ctx.conn.execute(
        """select platform_id, sum(mentioning_answers) k, sum(real_answers) n,
                  sum(avg_position * mentioning_answers) / nullif(sum(mentioning_answers), 0) pos
           from mart.daily_visibility where brand_id = %s and day between %s and %s group by 1""",
        (ctx.focus, start, ctx.data_end)).fetchall()
    by_platform = {r["platform_id"]: (r["k"] / r["n"] if r["n"] else None, r["pos"]) for r in overall}
    for row in table:
        vis, pos = by_platform.get(row["platform_id"], (None, None))
        row["overall"], row["overall_pos"] = vis, float(pos) if pos else None
    tot = ctx.conn.execute("""select sum(mentioning_answers) k, sum(real_answers) n from mart.daily_visibility
                              where brand_id = %s and day between %s and %s""",
                           (ctx.focus, start, ctx.data_end)).fetchone()
    return {"start": start, "end": ctx.data_end, "rows": table,
            "overall": tot["k"] / tot["n"] if tot["n"] else None, "overall_n": tot["n"]}


def _volume(ctx: Ctx) -> dict[str, Any]:
    r = ctx.conn.execute("""select sum(total) total, sum(ok) ok, count(distinct platform_id) platforms
                            from mart.daily_platform_health""").fetchone()
    # Only the sources the metrics are built from (never a blend of synthetic and real).
    src = ctx.conn.execute("""select source, count(*) n from parsed.metric_answers group by 1 order by 2 desc""").fetchall()
    prompts = ctx.conn.execute("select count(*) n from config.prompts where active").fetchone()["n"]
    return {"answers": r["total"], "real": r["ok"], "real_rate": r["ok"] / r["total"] if r["total"] else 0,
            "platforms": r["platforms"], "prompts": prompts, "sources": {x["source"]: x["n"] for x in src}}


def _series(ctx: Ctx, platform: str, category: str, brands: list[str]) -> dict[str, list[tuple[date, float | None]]]:
    rows = ctx.conn.execute(
        """select brand_id, day, visibility from mart.daily_category_visibility
           where platform_id = %s and category = %s and brand_id = any(%s) order by day""",
        (platform, category, brands)).fetchall()
    out: dict[str, list[tuple[date, float | None]]] = defaultdict(list)
    for r in rows:
        v = None if r["day"] in ctx.bad_days.get(platform, set()) or r["visibility"] is None else float(r["visibility"])
        out[r["brand_id"]].append((r["day"], v))
    return out


def _prompt_moves(ctx: Ctx, platform: str, category: str, brand: str, change_day: date) -> list[dict[str, Any]]:
    version = ctx.conn.execute("select config.active_parser_version() v").fetchone()["v"]
    rows = ctx.conn.execute(
        """select p.text, a.day >= %(cd)s as after, count(*) n, count(m.brand_id) k
           from parsed.metric_answers a join config.prompts p using (prompt_id)
           left join parsed.mentions m on m.raw_answer_id = a.raw_answer_id
                and m.parser_version = a.parser_version and m.brand_id = %(b)s
           where a.parser_version = %(v)s and a.platform_id = %(p)s and a.category = %(c)s and a.status = 'ok'
             and not (a.day = any(%(x)s))
           group by 1, 2""",
        {"cd": change_day, "b": brand, "v": version, "p": platform, "c": category,
         "x": list(ctx.bad_days.get(platform, set()))}).fetchall()
    by: dict[str, dict[bool, float]] = defaultdict(dict)
    for r in rows:
        by[r["text"]][r["after"]] = r["k"] / r["n"]
    moves = [{"prompt": t, "before": v[False], "after": v[True], "delta": v[True] - v[False]}
             for t, v in by.items() if True in v and False in v]
    return sorted(moves, key=lambda m: m["delta"])


def _moving_url(ctx: Ctx, platform: str, category: str, domain: str, change_day: date, up: bool) -> str | None:
    """The page on `domain` whose citation rate moved most in the driver's direction."""
    r = ctx.conn.execute(
        """with a as (
             select raw_answer_id, parser_version, day >= %(cd)s as after from parsed.metric_answers
             where parser_version = config.active_parser_version() and platform_id = %(p)s
               and category = %(c)s and status = 'ok' and not (day = any(%(x)s))),
           n as (select after, count(*) n from a group by after)
           select url from (
             select c.url,
                    coalesce(count(*) filter (where a.after)::numeric / nullif((select n from n where after), 0), 0)
                  - coalesce(count(*) filter (where not a.after)::numeric / nullif((select n from n where not after), 0), 0)
                    as delta
             from parsed.citations c join a on a.raw_answer_id = c.raw_answer_id and a.parser_version = c.parser_version
             where c.domain = %(d)s
             group by c.url) u
           order by delta * %(sign)s desc limit 1""",
        {"cd": change_day, "p": platform, "c": category, "d": domain, "sign": 1 if up else -1,
         "x": list(ctx.bad_days.get(platform, set()))}).fetchone()
    return r["url"] if r else None


def _position(ctx: Ctx, platform: str, category: str, brand: str, change_day: date) -> tuple[Any, Any, Any, Any]:
    r = ctx.conn.execute(
        """select sum(avg_position * mentioning_answers) filter (where day < %(cd)s)
                    / nullif(sum(mentioning_answers) filter (where day < %(cd)s), 0) as pos_before,
                  sum(avg_position * mentioning_answers) filter (where day >= %(cd)s)
                    / nullif(sum(mentioning_answers) filter (where day >= %(cd)s), 0) as pos_after,
                  sum(mentioning_answers) filter (where day < %(cd)s)::numeric
                    / nullif(sum(real_answers) filter (where day < %(cd)s), 0) as vis_before,
                  sum(mentioning_answers) filter (where day >= %(cd)s)::numeric
                    / nullif(sum(real_answers) filter (where day >= %(cd)s), 0) as vis_after
           from mart.daily_category_visibility
           where platform_id = %(p)s and category = %(c)s and brand_id = %(b)s and not (day = any(%(x)s))""",
        {"cd": change_day, "p": platform, "c": category, "b": brand,
         "x": list(ctx.bad_days.get(platform, set()))}).fetchone()
    return r["pos_before"], r["pos_after"], r["vis_before"], r["vis_after"]


# How to get a brand represented on a third-party page depends on what kind of site it is.
SITE_PLAYBOOK = {
    "review": ({"g2.com", "capterra.com", "trustradius.com", "getapp.com", "trustpilot.com"},
               "Update {F}'s profile and comparison data on the site and ask current customers in this "
               "category for reviews; comparison pages are built from that data."),
    "community": ({"reddit.com", "stackoverflow.com", "news.ycombinator.com", "quora.com", "dev.to"},
                  "Have a named {F} employee answer in the thread with a disclosed, factual reply that "
                  "addresses the question asked; do not astroturf."),
}
DEFAULT_PLAY = "Contact the publisher with current facts about {F} and offer a quote or data for the next update."


# What a model can quote when comparing providers, per question category.
QUOTABLE = {
    "payments": "pricing, payment methods and countries covered, time to integrate",
    "billing": "pricing, tax handling, usage-based and invoicing features",
    "payouts": "payout countries and speed, onboarding and KYC, fees",
    "fraud": "how scoring works, chargeback handling, pricing",
}


def _play(domain: str, focus_name: str) -> str:
    for sites, text in SITE_PLAYBOOK.values():
        if domain in sites:
            return text.format(F=focus_name)
    return DEFAULT_PLAY.format(F=focus_name)


# ------------------------------------------------------------------------------ stories
@dataclass
class Story:
    platform_id: str
    category: str
    change_day: date
    findings: list[dict[str, Any]]
    confidence: str
    headline: str = ""
    body: list[str] = field(default_factory=list)
    drivers: list[dict[str, Any]] = field(default_factory=list)
    chart: str = ""
    focus_delta: float = 0.0
    prompt_moves: list[dict[str, Any]] = field(default_factory=list)
    prompt_move: dict[str, Any] | None = None
    focus_visibility: tuple[str, float, float] | None = None   # (category, before, after)
    focus_position: tuple[str, float, float] | None = None     # (category, before, after)
    why: str = ""


def _group(findings: list[dict[str, Any]], focus: str) -> list[Story]:
    """One story per platform + change date: brands that moved together are one event."""
    stories: list[Story] = []
    for f in findings:
        if f["confidence"] == "low":
            continue
        cd = date.fromisoformat(str(f["change_day"]))
        home = next((s for s in stories if s.platform_id == f["platform_id"]
                     and abs((s.change_day - cd).days) <= 2), None)
        if home:
            home.findings.append(f)
            if f["confidence"] == "high":
                home.confidence = "high"
        else:
            stories.append(Story(f["platform_id"], f["category"], cd, [f], f["confidence"]))
    for s in stories:
        s.focus_delta = sum(f["after"] - f["before"] for f in s.findings if f["brand_id"] == focus)
    return stories


def _write_story(ctx: Ctx, s: Story) -> None:
    P = ctx.platforms[s.platform_id]
    focus_f = [f for f in s.findings if f["brand_id"] == ctx.focus]
    other_f = [f for f in s.findings if f["brand_id"] != ctx.focus]
    cats = sorted({f["category"] for f in s.findings})
    cat_txt = " and ".join(cats)
    F = ctx.names[ctx.focus]

    def verb(f: dict[str, Any]) -> str:
        return ("lost" if f["after"] < f["before"] else "gained") + f" {pts(f['after'] - f['before'])}"

    if focus_f:
        f = focus_f[0]
        winner = next((o for o in other_f if o["after"] > o["before"]), None)
        s.headline = (f"{F} {verb(f)} of {f['category']} visibility on {P}"
                      + (f"; {ctx.names[winner['brand_id']]} {verb(winner)}" if winner else ""))
    else:
        brands = sorted({ctx.names[f["brand_id"]] for f in other_f})
        deltas = sorted(abs(f["after"] - f["before"]) * 100 for f in other_f)
        rng = f"{deltas[0]:.0f}" if len(deltas) == 1 or round(deltas[0]) == round(deltas[-1]) \
            else f"{deltas[0]:.0f}–{deltas[-1]:.0f}"
        direction = "gained" if other_f[0]["after"] > other_f[0]["before"] else "lost"
        s.headline = f"{' and '.join(brands)} {direction} {rng} points on {P} in {cat_txt} answers"

    positions: dict[str, tuple[str, float, float]] = {}
    for cat in cats:  # one paragraph per category: every brand that moved, then the focus brand's position
        fs = sorted([f for f in s.findings if f["category"] == cat], key=lambda x: x["brand_id"] != ctx.focus)
        moves = ", ".join(f"{ctx.names[f['brand_id']]} {pct(f['before'])} → {pct(f['after'])}" for f in fs)
        text = (f"In {cat} answers on {P}, from {day(s.change_day)}: {moves}. Based on {fs[0]['before_n']:,} real "
                f"answers before and {fs[0]['after_n']:,} after.")
        for f in fs:
            if f.get("position_before") and f.get("position_after"):
                text += (f" {ctx.names[f['brand_id']]}'s average position moved from "
                         f"{f['position_before']:.1f} to {f['position_after']:.1f}.")
        if not any(f["brand_id"] == ctx.focus for f in fs):
            pb, pa, vb, va = _position(ctx, s.platform_id, cat, ctx.focus, s.change_day)
            if pb and pa:
                positions[cat] = (cat, float(pb), float(pa))
            if vb is not None and va is not None:
                text += f" {F} was named in {pct(vb)} of answers before and {pct(va)} after"
                if pb and pa and float(pa) - float(pb) >= 0.3:
                    text += (f", but it is now recommended later in the answer: average position "
                             f"{float(pb):.1f} before, {float(pa):.1f} after.")
                    s.focus_delta = s.focus_delta or -0.0001  # a position loss still matters to the focus brand
                else:
                    text += "."
        s.body.append(text)

    lead_cat = (focus_f or sorted(other_f, key=lambda x: -abs(x["after"] - x["before"])))[0]["category"]
    s.focus_position = positions.get(lead_cat) or next(iter(positions.values()), None)
    own = next((f for f in s.findings if f["brand_id"] == ctx.focus), None)
    if own:
        s.focus_visibility = (own["category"], own["before"], own["after"])

    seen = set()
    for f in s.findings:
        for d in f.get("drivers", []):
            if d["domain"] in seen:
                continue
            seen.add(d["domain"])
            d = dict(d)
            d["url"] = _moving_url(ctx, s.platform_id, f["category"], d["domain"], s.change_day,
                                   d["after"] > d["before"])
            d["owner_name"] = ctx.names.get(d.get("owner") or "", None)
            d["brand_name"] = ctx.names.get(d.get("brand") or "", None)
            s.drivers.append(d)
    lead = next((f for f in s.findings if f["brand_id"] == ctx.focus), s.findings[0])
    s.prompt_moves = _prompt_moves(ctx, s.platform_id, lead["category"], lead["brand_id"], s.change_day)
    if s.prompt_moves:
        m = s.prompt_moves[0] if lead["after"] < lead["before"] else s.prompt_moves[-1]
        s.prompt_move = {**m, "brand": ctx.names[lead["brand_id"]]}
    s.why = (f"the gap is far beyond chance (p = {lead['p_value']:.1e}; the bar after correcting for "
             f"{lead.get('series_tested') or 'all'} series is {lead.get('alpha') or 0:.1e}), "
             f"{lead['persistence']:.0%} of days sit on their side of the change, and "
             f"{lead['breadth']} of {lead['prompts']} {lead['category']} questions moved the same way")
    if any("data-quality" in r for r in lead.get("reasons", [])):
        s.why += "; but the change date is within two days of a data problem on this assistant"
    series = _series(ctx, s.platform_id, lead["category"],
                     list(dict.fromkeys([ctx.focus] + [f["brand_id"] for f in s.findings
                                                       if f["category"] == lead["category"]])))
    s.chart = charts.line_chart(series, ctx.names, s.change_day, focus=ctx.focus)


def _watch(ctx: Ctx, s: Story, citation: dict[str, Any] | None = None) -> str:
    """A number the client can track to know the recommendation worked."""
    F, P = ctx.names[ctx.focus], ctx.platforms[s.platform_id]
    if citation:
        target = math.floor((citation["after"] + 0.75 * (citation["before"] - citation["after"])) * 20) / 20
        return (f"pages on {citation['domain']} cited in at least {pct(target)} of {P} answers in this category "
                f"(now {pct(citation['after'])}; {pct(citation['before'])} before {day(s.change_day)}).")
    if s.focus_visibility:
        cat, before, after = s.focus_visibility
        target = math.floor((after + 0.75 * (before - after)) * 20) / 20
        return (f"{F} named in at least {pct(target)} of {P} {cat} answers "
                f"(now {pct(after)}; {pct(before)} before {day(s.change_day)}).")
    if s.focus_position:
        cat, before, after = s.focus_position
        return (f"{F}'s average position in {P} {cat} answers back to {before + 0.25 * (after - before):.1f} "
                f"or better (now {after:.1f}; {before:.1f} before {day(s.change_day)}).")
    return f"{F}'s visibility on {P} in the weekly table above."


def _recommendations(ctx: Ctx, stories: list[Story], issues: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Ranked by how many of the focus brand's answers are at stake, then confidence. Only actions the
    client can take: collection problems are ours and appear under data quality, not here."""
    F = ctx.names[ctx.focus]
    recs: list[tuple[float, dict[str, str]]] = []
    for s in stories:
        P = ctx.platforms[s.platform_id]
        weight = 2.0 if s.confidence == "high" else 1.0
        lead = next((f for f in s.findings if f["brand_id"] == ctx.focus), s.findings[0])
        lost = s.focus_delta < 0
        owned_down = [d for d in s.drivers if d.get("owner") == ctx.focus and d["after"] < d["before"]]
        third_up = [d for d in s.drivers if not d.get("owner") and d["after"] > d["before"]]
        rival_up = [d for d in s.drivers if d.get("owner") and d["owner"] != ctx.focus and d["after"] > d["before"]]
        worst = s.prompt_moves[0] if s.prompt_moves and lead["brand_id"] == ctx.focus and lost else None
        if owned_down:
            d = owned_down[0]
            recs.append((weight * abs(s.focus_delta) + 0.02, {
                "title": f"Find out why {P} cites {d['url'] or d['domain']} less",
                "why": f"Pages on {d['domain']} appeared in {pct(d['before'])} of {P} {lead['category']} answers "
                       f"before {day(s.change_day)} and {pct(d['after'])} since, and this page fell the most. In the "
                       f"same period {F}'s {lead['category']} visibility on {P} fell {pts(s.focus_delta)}.",
                "do": "Check the page is crawlable by the assistant's search crawler, that its content "
                      "is current, and that it answers the questions buyers ask in its first screen.",
                "watch": _watch(ctx, s, d),
                "owner": "Developer relations / docs"}))
        for d in rival_up[:1]:
            q = f" The biggest single drop is on “{worst['prompt']}” ({pct(worst['before'])} to " \
                f"{pct(worst['after'])})." if worst else ""
            recs.append((weight * abs(s.focus_delta) + 0.01, {
                "title": f"Publish {F}'s answer to the question {ctx.names[d['owner']]} now owns on {P}",
                "why": f"{d['domain']} went from {pct(d['before'])} to {pct(d['after'])} of {P} "
                       f"{lead['category']} answers, led by {d['url'] or 'its pages'}.{q}",
                "do": f"Write a page that answers that question directly and fairly compares {F} with "
                      f"{ctx.names[d['owner']]}, with specifics a model can quote "
                      f"({QUOTABLE.get(lead['category'], 'pricing, coverage, setup')}).",
                "watch": _watch(ctx, s),
                "owner": "Product marketing"}))
        for d in third_up[:1]:
            rival = next((f for f in s.findings if f["brand_id"] != ctx.focus and f["after"] > f["before"]), None)
            gain = (f" {ctx.names[rival['brand_id']]} rose {pts(rival['after'] - rival['before'])} over the "
                    "same days.") if rival else ""
            recs.append((weight * (abs(s.focus_delta) + (abs(rival["after"] - rival["before"]) if rival else 0)) / 2, {
                "title": f"Get {F} represented on {d['url'] or d['domain']}",
                "why": f"{P} now cites {d['domain']} in {pct(d['after'])} of {lead['category']} answers, up from "
                       f"{pct(d['before'])}, and this is the page that rose most.{gain}",
                "do": _play(d["domain"], F),
                "watch": _watch(ctx, s),
                "owner": "Marketing / community"}))
    recs.sort(key=lambda x: -x[0])
    return [r for _, r in recs[:5]]


def _weak_spot_recommendations(ctx: Ctx, standing: dict[str, Any], limit: int = 3) -> list[dict[str, str]]:
    """Without enough history to detect changes, act on today's weakest assistant-category cells:
    the competitor closest behind, and the sites that assistant cites for those questions."""
    F = ctx.names[ctx.focus]
    cells = []
    for row in standing["rows"]:
        for cat, c in zip(ctx.focus_categories, row["cells"], strict=True):
            if c["vis"] is not None and c["n"]:
                cells.append((c["vis"], row["platform_id"], cat, c))
    recs = []
    ordinal = ["weakest", "second-weakest", "third-weakest", "fourth-weakest", "fifth-weakest"]
    for i, (vis, platform, cat, c) in enumerate(sorted(cells, key=lambda x: x[0])[:limit]):
        P = ctx.platforms[platform]
        sites = ctx.conn.execute(
            """select domain, sum(citing_answers)::numeric / nullif(sum(real_answers), 0) rate
               from mart.daily_citations where platform_id = %s and category = %s and day between %s and %s
               group by domain order by rate desc nulls last limit 3""",
            (platform, cat, standing["start"], standing["end"])).fetchall()
        site_txt = ", ".join(f"{s['domain']} ({pct(float(s['rate']))})" for s in sites if s["rate"])
        rival = c["leader"] or c.get("runner_up")
        rival_vis = c["leader_vis"] if c["leader"] else (vis - c["gap"] if c["gap"] is not None else None)
        own = [s for s in sites if ctx.owners.get(s["domain"]) == ctx.focus]
        third = [s for s in sites if not ctx.owners.get(s["domain"])]
        if third:
            do = _play(third[0]["domain"], F)
        elif own:
            do = (f"{P} already leans on {own[0]['domain']} here; make that page answer these questions "
                  f"in its first screen with specifics a model can quote ({QUOTABLE.get(cat, 'pricing, coverage, setup')}).")
        else:
            do = f"Publish a page that answers these {cat} questions directly, with quotable specifics."
        target = min(0.95, math.floor(round((vis + 0.10) * 20, 6)) / 20)
        recs.append({
            "title": f"Strengthen {F} in {cat} answers on {P}",
            "why": (f"This is {F}'s {ordinal[i]} spot: named in {pct(vis)} of {c['n']} real {cat} answers"
                    + (f", with {rival} at {pct(rival_vis)}" if rival and rival_vis is not None else "") + "."
                    + (f" The sites {P} cites most for these questions: {site_txt}." if site_txt else "")),
            "do": do,
            "watch": f"{F} named in at least {pct(target)} of {P} {cat} answers (now {pct(vis)}).",
            "owner": "Marketing / developer relations"})
    return recs


def _summary(ctx: Ctx, standing: dict[str, Any], stories: list[Story], issues: list[dict[str, Any]]) -> list[str]:
    F = ctx.names[ctx.focus]
    lines = []
    leads = sum(1 for r in standing["rows"] for c in r["cells"] if c["rank"] == 1)
    total = sum(1 for r in standing["rows"] for c in r["cells"] if c["rank"])
    lines.append(f"Across {len(ctx.platforms)} assistants, {F} was named in {pct(standing['overall'])} of "
                 f"{standing['overall_n']:,} relevant answers ({_span({'start': standing['start'], 'end': standing['end']})}). It is the most-named "
                 f"brand in {leads} of {total} assistant-and-category pairs.")
    losses = [s for s in stories if any(f["brand_id"] == ctx.focus and f["after"] < f["before"] for f in s.findings)]
    if losses:
        s = min(losses, key=lambda x: x.focus_delta)
        f = next(f for f in s.findings if f["brand_id"] == ctx.focus)
        lines.append(f"The one real loss is {f['category']} on {ctx.platforms[s.platform_id]}: {pct(f['before'])} "
                     f"to {pct(f['after'])} from {day(s.change_day)}.")
    rivals = [s for s in stories if not any(f["brand_id"] == ctx.focus for f in s.findings)]
    for s in rivals[:1]:
        f = max(s.findings, key=lambda x: x["after"] - x["before"])
        held = (f"{F} is still named as often there but now appears later in the answer"
                if s.focus_delta < 0 else f"{F}'s own share there held")
        lines.append(f"{ctx.names[f['brand_id']]} gained on {ctx.platforms[s.platform_id]} "
                     f"({pct(f['before'])} to {pct(f['after'])} in {f['category']}); {held}.")
    if issues:
        spans = "; ".join(f"{ctx.platforms[i['platform_id']]} {_span(i['evidence'])}" for i in issues)
        lines.append(f"Two apparent drops were collection problems, not market moves ({spans}). "
                     "They are excluded from the analysis." if len(issues) == 2 else
                     f"{len(issues)} apparent drops were collection problems ({spans}) and are excluded.")
    return lines


def _span(ev: dict[str, Any]) -> str:
    s, e = date.fromisoformat(str(ev["start"])), date.fromisoformat(str(ev["end"]))
    if s == e:
        return day(s)
    return f"{s.day}–{day(e)}" if s.month == e.month else f"{day(s)}–{day(e)}"


# ------------------------------------------------------------------------------ build
def build(conn: psycopg.Connection, focus: str) -> tuple[str, uuid.UUID, date]:
    ctx = _ctx(conn, focus)
    # The latest insights run, even when it found nothing (an empty batch writes no insight rows).
    batch = conn.execute("""select detail->>'batch' as batch_id from ops.pipeline_runs
                            where step = 'insights' and status = 'succeeded'
                            order by started_at desc limit 1""").fetchone()
    if not batch or not batch["batch_id"]:
        raise RuntimeError("no insights yet: run `geo insights` first")
    batch_id = batch["batch_id"]
    rows = conn.execute("select * from mart.insights where batch_id = %s order by insight_id", (batch_id,)).fetchall()
    findings = [{**r["evidence"], "confidence": r["confidence"]} for r in rows if r["kind"] == "visibility_change"]
    issues = [r for r in rows if r["kind"] == "data_issue"]
    for i in issues:
        ev = i["evidence"]
        s, e = date.fromisoformat(str(ev["start"])), date.fromisoformat(str(ev["end"]))
        ctx.bad_days.setdefault(i["platform_id"], set()).update(s + timedelta(days=k) for k in range((e - s).days + 1))

    relevant = [f for f in findings if f["category"] in ctx.focus_categories]
    stories = _group(relevant, focus)
    stories.sort(key=lambda s: (s.confidence != "high", -abs(s.focus_delta), s.change_day))
    for s in stories:
        _write_story(ctx, s)
    standing = _standing(ctx)
    volume = _volume(ctx)
    watch = [f for f in relevant if f["confidence"] == "low"]

    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    env.filters.update(pct=pct, pts=pts, day=day, span=_span)
    html = env.get_template("report.html").render(
        brand=ctx.names[focus], ctx=ctx, standing=standing, stories=stories, issues=issues, volume=volume,
        summary=_summary(ctx, standing, stories, issues),
        recommendations=_recommendations(ctx, stories, issues) or _weak_spot_recommendations(ctx, standing),
        watch=watch, history_days=(ctx.data_end - ctx.data_start).days + 1,
        heat=charts.heat, categories=ctx.focus_categories, synthetic=volume["sources"].get("synthetic", 0) > 0,
        generated=datetime.now(UTC).date(), batch_id=batch_id)
    return html, batch_id, ctx.data_end


def build_and_store(focus: str | None) -> tuple[uuid.UUID, str]:
    focus = focus or settings().focus_brand
    with connection() as conn:
        html, batch_id, data_end = build(conn, focus)
        report_id = uuid.uuid4()
        conn.execute("""insert into mart.reports (report_id, focus_brand, data_through, insights_batch, html)
                        values (%s, %s, %s, %s, %s)""", (report_id, focus, data_end, batch_id, html))
    return report_id, html
