"""Find visibility changes worth a customer's attention, explain them, grade confidence.

Method (also in docs/METHOD.md):
1. Data quality first. For each platform-day, the share of answers that are real (`ok`).
   A day well below the platform's normal rate is a collection problem, not a trend: it
   becomes a `data_issue` and is excluded from change detection on that platform.
2. Change detection at the (platform, category, brand) grain, on clean days only. Daily
   counts k/n are treated as binomial. The best single change day maximises the two-segment
   likelihood (each side >= MIN_SEGMENT days). The before/after rates are then compared with a
   two-proportion z-test.
3. A change is reported only if it is large (>= MIN_DELTA), significant after a Bonferroni
   correction across every series tested, persistent (most days after the change sit on the
   new side), and broad (moves in the same direction on several prompts, not one).
4. Why: citation rates per domain before vs after on that platform and category, plus how
   much more often the brand is named when a moving domain is cited (lift).
"""

from __future__ import annotations

import math
import statistics
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

import psycopg

from geo.db import connection
from geo.etl.pipeline import tracked

MIN_SEGMENT = 4         # days on each side of a change
MIN_DELTA = 0.10        # 10 percentage points
MIN_PERSISTENCE = 0.70  # share of after-days on the new side of the midpoint
MIN_BREADTH = 2         # prompts moving in the same direction
OK_RATE_FLOOR = 0.75
OK_RATE_DROP = 0.12


@dataclass
class DataIssue:
    platform_id: str
    start: date
    end: date
    ok_rate: float
    baseline_ok_rate: float
    dominant_failure: str
    answers: int


@dataclass
class Finding:
    platform_id: str
    category: str
    brand_id: str
    change_day: date
    before: float
    after: float
    before_n: int
    after_n: int
    before_days: int
    after_days: int
    p_value: float
    persistence: float
    breadth: int
    prompts: int
    confidence: str
    reasons: list[str] = field(default_factory=list)
    drivers: list[dict[str, Any]] = field(default_factory=list)
    position_before: float | None = None
    position_after: float | None = None
    alpha: float | None = None
    series_tested: int | None = None

    @property
    def delta(self) -> float:
        return self.after - self.before


# ------------------------------------------------------------------------------- statistics
def _loglik(k: int, n: int) -> float:
    if n == 0 or k == 0 or k == n:
        return 0.0
    p = k / n
    return k * math.log(p) + (n - k) * math.log(1 - p)


def best_split(ks: list[int], ns: list[int], min_seg: int = MIN_SEGMENT) -> tuple[int, float] | None:
    """Index of the first 'after' day maximising the two-segment binomial likelihood."""
    if len(ks) < 2 * min_seg:
        return None
    base = _loglik(sum(ks), sum(ns))
    best: tuple[int, float] | None = None
    for t in range(min_seg, len(ks) - min_seg + 1):
        gain = _loglik(sum(ks[:t]), sum(ns[:t])) + _loglik(sum(ks[t:]), sum(ns[t:])) - base
        if best is None or gain > best[1]:
            best = (t, gain)
    return best


def two_proportion_p(k1: int, n1: int, k2: int, n2: int) -> float:
    if n1 == 0 or n2 == 0:
        return 1.0
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = abs(k1 / n1 - k2 / n2) / se
    return math.erfc(z / math.sqrt(2))


# ------------------------------------------------------------------------------- data quality
def find_data_issues(conn: psycopg.Connection) -> tuple[list[DataIssue], dict[str, set[date]]]:
    rows = conn.execute("""select day, platform_id, total, ok, error, empty, not_answer, truncated, wrong_prompt
                           from mart.daily_platform_health order by platform_id, day""").fetchall()
    by_platform: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_platform[r["platform_id"]].append(r)
    issues: list[DataIssue] = []
    bad: dict[str, set[date]] = defaultdict(set)
    for platform, days in by_platform.items():
        rates = [d["ok"] / d["total"] for d in days if d["total"]]
        baseline = statistics.median(rates) if rates else 1.0
        flagged = [d for d in days if d["total"] and (d["ok"] / d["total"] < OK_RATE_FLOOR
                                                     or d["ok"] / d["total"] < baseline - OK_RATE_DROP)]
        bad[platform] = {d["day"] for d in flagged}
        run: list[dict] = []
        for d in flagged + [None]:  # group consecutive days
            if d is not None and run and d["day"] == run[-1]["day"] + timedelta(days=1):
                run.append(d)
                continue
            if run:
                failures = {k: sum(x[k] for x in run) for k in ("error", "empty", "not_answer", "truncated",
                                                                 "wrong_prompt")}
                total, ok = sum(x["total"] for x in run), sum(x["ok"] for x in run)
                issues.append(DataIssue(platform, run[0]["day"], run[-1]["day"], ok / total, baseline,
                                        max(failures, key=failures.get), total))
            run = [d] if d is not None else []
    return issues, bad


# ------------------------------------------------------------------------------- evidence
def _prompt_breadth(conn, version: str, platform: str, category: str, brand: str, change_day: date,
                    start: date, end: date, excluded: set[date], direction: int) -> tuple[int, int]:
    rows = conn.execute(
        """select a.prompt_id, a.day >= %(cd)s as after,
                  count(*) as n, count(m.brand_id) as k
           from parsed.answers a
           left join parsed.mentions m on m.raw_answer_id = a.raw_answer_id
                and m.parser_version = a.parser_version and m.brand_id = %(brand)s
           where a.parser_version = %(v)s and a.platform_id = %(p)s and a.category = %(c)s and a.status = 'ok'
             and a.day between %(s)s and %(e)s and not (a.day = any(%(x)s))
           group by 1, 2""",
        {"cd": change_day, "brand": brand, "v": version, "p": platform, "c": category, "s": start, "e": end,
         "x": list(excluded)}).fetchall()
    rates: dict[str, dict[bool, float]] = defaultdict(dict)
    for r in rows:
        rates[r["prompt_id"]][r["after"]] = r["k"] / r["n"]
    moved = sum(1 for v in rates.values()
                if True in v and False in v and (v[True] - v[False]) * direction >= MIN_DELTA / 2)
    return moved, len(rates)


def _drivers(conn, version: str, platform: str, category: str, brand: str, change_day: date, start: date,
             end: date, excluded: set[date], owners: dict[str, str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """with real as (
             select day, max(real_answers) as real_answers from mart.daily_category_visibility
             where platform_id = %(p)s and category = %(c)s and day between %(s)s and %(e)s
               and not (day = any(%(x)s)) group by day)
           select d.domain,
                  sum(d.citing_answers) filter (where d.day <  %(cd)s) as before_k,
                  sum(d.citing_answers) filter (where d.day >= %(cd)s) as after_k,
                  (select sum(real_answers) from real where day <  %(cd)s) as before_n,
                  (select sum(real_answers) from real where day >= %(cd)s) as after_n
           from mart.daily_citations d
           where d.platform_id = %(p)s and d.category = %(c)s and d.day between %(s)s and %(e)s
             and not (d.day = any(%(x)s))
           group by d.domain""",
        {"p": platform, "c": category, "s": start, "e": end, "cd": change_day, "x": list(excluded)}).fetchall()
    out = []
    for r in rows:
        if not r["before_n"] or not r["after_n"]:
            continue
        b, a = (r["before_k"] or 0) / r["before_n"], (r["after_k"] or 0) / r["after_n"]
        if abs(a - b) < 0.10:
            continue
        kind = ("started" if b < 0.10 and a >= 0.25 else "stopped" if b >= 0.25 and a < 0.10
                else "up" if a > b else "down")
        out.append({"domain": r["domain"], "before": round(b, 4), "after": round(a, 4), "kind": kind,
                    "owner": owners.get(r["domain"]), "brand": brand})
    out.sort(key=lambda d: abs(d["after"] - d["before"]), reverse=True)
    for d in out[:4]:  # how much more often the brand is named when this domain is cited
        lift = conn.execute(
            """select count(*) filter (where cited) as n_cited,
                      count(*) filter (where cited and named) as k_cited,
                      count(*) filter (where not cited) as n_other,
                      count(*) filter (where not cited and named) as k_other
               from (select a.raw_answer_id,
                            exists (select 1 from parsed.citations c where c.raw_answer_id = a.raw_answer_id
                                    and c.parser_version = a.parser_version and c.domain = %(d)s) as cited,
                            exists (select 1 from parsed.mentions m where m.raw_answer_id = a.raw_answer_id
                                    and m.parser_version = a.parser_version and m.brand_id = %(b)s) as named
                     from parsed.answers a
                     where a.parser_version = %(v)s and a.platform_id = %(p)s and a.category = %(c)s
                       and a.status = 'ok' and a.day between %(cd)s and %(e)s
                       and not (a.day = any(%(x)s))) t""",
            {"d": d["domain"], "b": brand, "v": version, "p": platform, "c": category, "cd": change_day,
             "e": end, "x": list(excluded)}).fetchone()
        if lift["n_cited"] >= 10 and lift["n_other"] >= 10:
            d["named_when_cited"] = round(lift["k_cited"] / lift["n_cited"], 4)
            d["named_otherwise"] = round(lift["k_other"] / lift["n_other"], 4)
            d["lift_p"] = two_proportion_p(lift["k_cited"], lift["n_cited"], lift["k_other"], lift["n_other"])
    return out[:4]


# ------------------------------------------------------------------------------- detection
def detect(conn: psycopg.Connection) -> tuple[list[Finding], list[DataIssue], int]:
    version = conn.execute("select config.active_parser_version() v").fetchone()["v"]
    issues, bad_days = find_data_issues(conn)
    owners = {d: r["brand_id"] for r in conn.execute("select brand_id, domains from config.brands")
              for d in r["domains"]}
    rows = conn.execute("""select platform_id, category, brand_id, day, real_answers, mentioning_answers,
                                  avg_position
                           from mart.daily_category_visibility
                           where real_answers > 0 order by platform_id, category, brand_id, day""").fetchall()
    series: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if r["day"] not in bad_days[r["platform_id"]]:
            series[(r["platform_id"], r["category"], r["brand_id"])].append(r)

    alpha = 0.05 / max(1, len(series))  # Bonferroni across every series tested
    findings: list[Finding] = []
    for (platform, category, brand), pts in series.items():
        ks, ns = [p["mentioning_answers"] for p in pts], [p["real_answers"] for p in pts]
        split = best_split(ks, ns)
        if not split:
            continue
        t = split[0]
        k1, n1, k2, n2 = sum(ks[:t]), sum(ns[:t]), sum(ks[t:]), sum(ns[t:])
        before, after = k1 / n1, k2 / n2
        delta = after - before
        if abs(delta) < MIN_DELTA:
            continue
        p = two_proportion_p(k1, n1, k2, n2)
        if p >= alpha:
            continue
        mid = (before + after) / 2
        after_side = [(k / n - mid) * delta > 0 for k, n in zip(ks[t:], ns[t:], strict=True)]
        before_side = [(k / n - mid) * delta < 0 for k, n in zip(ks[:t], ns[:t], strict=True)]
        persistence = min(sum(after_side) / len(after_side), sum(before_side) / len(before_side))
        change_day = pts[t]["day"]
        start, end = pts[0]["day"], pts[-1]["day"]
        breadth, n_prompts = _prompt_breadth(conn, version, platform, category, brand, change_day, start, end,
                                             bad_days[platform], 1 if delta > 0 else -1)
        near_issue = any(i.platform_id == platform and i.start - timedelta(days=2) <= change_day
                         <= i.end + timedelta(days=2) for i in issues)
        reasons = [f"p={p:.1e} vs threshold {alpha:.1e}",
                   f"{persistence:.0%} of days on each side sit on their side of the midpoint",
                   f"{breadth} of {n_prompts} prompts moved the same way"]
        if near_issue:
            reasons.append("change day is within 2 days of a data-quality problem on this platform")
        if p < alpha / 100 and persistence >= 0.85 and breadth >= 3 and not near_issue:
            confidence = "high"
        elif persistence >= MIN_PERSISTENCE and breadth >= MIN_BREADTH:
            confidence = "medium"
        else:
            confidence = "low"
        pos = [(p_["avg_position"], p_["mentioning_answers"]) for p_ in pts]

        def wavg(xs: list[tuple[Any, int]]) -> float | None:
            xs = [(float(a), w) for a, w in xs if a is not None and w]
            return sum(a * w for a, w in xs) / sum(w for _, w in xs) if xs else None

        findings.append(Finding(
            platform, category, brand, change_day, before, after, n1, n2, t, len(pts) - t, p, persistence,
            breadth, n_prompts, confidence, reasons,
            _drivers(conn, version, platform, category, brand, change_day, start, end, bad_days[platform], owners),
            wavg(pos[:t]), wavg(pos[t:]), alpha, len(series)))
    findings.sort(key=lambda f: ({"high": 0, "medium": 1, "low": 2}[f.confidence], -abs(f.delta)))
    return findings, issues, len(series)


def run_insights() -> dict[str, Any]:
    batch = uuid.uuid4()
    with tracked("insights") as result, connection() as conn:
        findings, issues, tested = detect(conn)
        for f in findings:
            conn.execute(
                """insert into mart.insights (batch_id, kind, brand_id, platform_id, category, change_day,
                     before_value, after_value, delta, before_n, after_n, p_value, confidence, summary, evidence)
                   values (%s, 'visibility_change', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (batch, f.brand_id, f.platform_id, f.category, f.change_day, f.before, f.after, f.delta,
                 f.before_n, f.after_n, f.p_value, f.confidence,
                 f"{f.brand_id} {f.category} visibility on {f.platform_id}: {f.before:.0%} -> {f.after:.0%}",
                 psycopg.types.json.Jsonb({**asdict(f), "change_day": str(f.change_day)}, dumps=_dumps)))
        for i in issues:
            conn.execute(
                """insert into mart.insights (batch_id, kind, platform_id, change_day, before_value, after_value,
                     confidence, summary, evidence)
                   values (%s, 'data_issue', %s, %s, %s, %s, 'high', %s, %s)""",
                (batch, i.platform_id, i.start, i.baseline_ok_rate, i.ok_rate,
                 f"{i.platform_id}: {i.ok_rate:.0%} real answers {i.start}..{i.end} ({i.dominant_failure})",
                 psycopg.types.json.Jsonb(asdict(i), dumps=_dumps)))
        result.update(batch=str(batch), findings=len(findings), data_issues=len(issues), series_tested=tested,
                      rows_out=len(findings) + len(issues))
    return {"batch": str(batch), "findings": len(findings), "data_issues": len(issues), "series_tested": tested}


def _dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, default=str)
