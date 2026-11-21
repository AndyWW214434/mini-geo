"""Chart specs and the metric registry.

A chart is a declarative query over the daily metric tables, never raw answers and never
free SQL. Ratios are pooled (sum of numerators / sum of denominators) so a 30-day line of
"Stripe vs Adyen on ChatGPT" matches the tables exactly, whatever the grouping.

Adding a metric = one entry in METRICS. Adding a chart type = one entry in CHART_TYPES
(and its renderer in the web app).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field, field_validator, model_validator

CHART_TYPES = {
    "line": "metric over time, one line per group",
    "bar": "metric over the whole period, one bar per group",
    "table": "metric per group and day as rows",
    "number": "a single pooled value for the whole selection",
}
Dimension = Literal["brand", "platform", "category", "domain"]


@dataclass(frozen=True)
class Metric:
    name: str
    description: str
    family: Literal["visibility", "citation"]
    numerator: str          # SQL over the source table's columns
    denominator: str | None
    unit: Literal["ratio", "rank", "count"]


METRICS: dict[str, Metric] = {m.name: m for m in [
    Metric("visibility", "Share of real answers (to prompts in the brand's categories) that name the brand",
           "visibility", "sum(mentioning_answers)", "sum(real_answers)", "ratio"),
    Metric("avg_position", "Average rank at which the brand is recommended when named (1 = first)",
           "visibility", "sum(avg_position * mentioning_answers)", "sum(mentioning_answers)", "rank"),
    Metric("mentions", "Number of real answers naming the brand", "visibility",
           "sum(mentioning_answers)", None, "count"),
    Metric("real_answers", "Number of real (complete, on-topic) answers collected", "visibility",
           "sum(real_answers)", None, "count"),
    Metric("citation_share", "Share of all citations that point to the domain", "citation",
           "sum(citations)", "sum(total_citations)", "ratio"),
    Metric("citation_rate", "Share of real answers that cite the domain at least once", "citation",
           "sum(citing_answers)", "sum(real_answers)", "ratio"),
]}

DIM_COLUMN = {"brand": "brand_id", "platform": "platform_id", "category": "category", "domain": "domain"}


class ChartSpec(BaseModel):
    type: Literal["line", "bar", "table", "number"] = "line"
    metric: str = "visibility"
    group_by: Dimension | None = None  # default: brand for visibility metrics, domain for citation metrics
    brands: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    date_from: date | None = None
    date_to: date | None = None
    last_n_days: int | None = Field(default=None, ge=1, le=365)

    model_config = {"extra": "forbid"}

    @field_validator("metric")
    @classmethod
    def _metric(cls, v: str) -> str:
        if v not in METRICS:
            raise ValueError(f"unknown metric {v!r}; one of {sorted(METRICS)}")
        return v

    @model_validator(mode="after")
    def _dims(self) -> ChartSpec:
        fam = METRICS[self.metric].family
        if self.type == "number":
            self.group_by = None
        elif self.group_by is None:
            self.group_by = "brand" if fam == "visibility" else "domain"
        if fam == "visibility" and (self.group_by == "domain" or self.domains):
            raise ValueError("visibility metrics are per brand; use citation_share or citation_rate for domains")
        if fam == "citation" and (self.group_by == "brand" or self.brands):
            raise ValueError("citation metrics are per domain, not per brand")
        return self


@dataclass
class ChartData:
    spec: dict[str, Any]
    unit: str
    date_from: date | None
    date_to: date | None
    series: list[dict[str, Any]]  # [{key, label, points: [{x, y, n}]}] (bar/number: one point, x = None)

    @property
    def empty(self) -> bool:
        return not any(p["y"] is not None for s in self.series for p in s["points"])


def _window(conn: psycopg.Connection, spec: ChartSpec) -> tuple[date | None, date | None]:
    last = conn.execute("select max(day) d from mart.daily_visibility").fetchone()["d"]
    end = spec.date_to or last
    start = spec.date_from
    if spec.last_n_days and end:
        start = end - timedelta(days=spec.last_n_days - 1)
    return start, end


def chart_data(conn: psycopg.Connection, spec: ChartSpec) -> ChartData:
    metric = METRICS[spec.metric]
    start, end = _window(conn, spec)
    by_day = spec.type in ("line", "table")
    key = sql.Identifier(DIM_COLUMN[spec.group_by]) if spec.group_by else sql.SQL("'all'")
    x = sql.SQL("day") if by_day else sql.SQL("null::date")

    def filters(dims: tuple[str, ...]) -> tuple[sql.Composable, dict[str, Any]]:
        where: list[sql.Composable] = [sql.SQL("true")]
        params: dict[str, Any] = {}
        for dim, values in (("brand", spec.brands), ("platform", spec.platforms), ("category", spec.categories),
                            ("domain", spec.domains)):
            if values and dim in dims:
                where.append(sql.SQL("{} = any({})").format(sql.Identifier(DIM_COLUMN[dim]), sql.Placeholder(dim)))
                params[dim] = values
        if start:
            where.append(sql.SQL("day >= %(start)s"))
            params["start"] = start
        if end:
            where.append(sql.SQL("day <= %(end)s"))
            params["end"] = end
        return sql.SQL(" and ").join(where), params

    if metric.family == "visibility":
        table = "daily_category_visibility" if spec.categories or spec.group_by == "category" else "daily_visibility"
        where, params = filters(("brand", "platform", "category"))
        query = sql.SQL("select {key} as key, {x} as x, {num} as num, {den} as den from mart.{table} "
                        "where {where} group by 1, 2").format(
            key=key, x=x, num=sql.SQL(metric.numerator), den=sql.SQL(metric.denominator or "sum(real_answers)"),
            table=sql.Identifier(table), where=where)
        rows = [(r["key"], r["x"], r["num"], r["den"]) for r in conn.execute(query, params)]
    else:
        # Numerator per group from the domain rows. The denominator comes from the per-(day, platform,
        # category) totals, which repeat on every domain row: they are taken once (max) and never summed
        # across domains. A domain filter narrows the numerator only.
        where, params = filters(("platform", "category", "domain"))
        nums = conn.execute(sql.SQL("select {key} as key, {x} as x, {num} as num from mart.daily_citations "
                                    "where {where} group by 1, 2").format(
            key=key, x=x, num=sql.SQL(metric.numerator), where=where), params).fetchall()
        keyed = spec.group_by in ("platform", "category")
        twhere, tparams = filters(("platform", "category"))
        dens = conn.execute(sql.SQL(
            "select {key} as key, {x} as x, sum(t) as den from ("
            " select day, platform_id, category, max({col}) as t from mart.daily_citations where {where}"
            " group by 1, 2, 3) totals group by 1, 2").format(
            key=key if keyed else sql.SQL("'all'"), x=x,
            col=sql.Identifier("total_citations" if metric.name == "citation_share" else "real_answers"),
            where=twhere), tparams).fetchall()
        den_by = {(r["key"], r["x"]): r["den"] for r in dens}
        rows = [(r["key"], r["x"], r["num"], den_by.get((r["key"] if keyed else "all", r["x"]))) for r in nums]

    rows.sort(key=lambda r: (str(r[0]), r[1] or date.min))
    labels = _labels(conn, spec.group_by)
    series: dict[str, dict[str, Any]] = {}
    for k, xv, num, den in rows:
        if metric.denominator:
            y = float(num) / float(den) if num is not None and den else None
        else:
            y = float(num) if num is not None else None
        s = series.setdefault(k, {"key": k, "label": labels.get(k, k), "points": []})
        s["points"].append({"x": xv.isoformat() if xv else None, "y": y, "n": int(den) if den is not None else None})
    if spec.type == "bar":  # biggest first
        series = dict(sorted(series.items(), key=lambda kv: -(kv[1]["points"][0]["y"] or 0)))
        if metric.family == "citation":
            series = dict(list(series.items())[:15])
    return ChartData(spec.model_dump(mode="json"), metric.unit, start, end, list(series.values()))


def _labels(conn: psycopg.Connection, dim: str | None) -> dict[str, str]:
    if dim == "brand":
        return {r["brand_id"]: r["name"] for r in conn.execute("select brand_id, name from config.brands")}
    if dim == "platform":
        return {r["platform_id"]: r["name"] for r in conn.execute("select platform_id, name from config.platforms")}
    return {}


def catalog(conn: psycopg.Connection) -> dict[str, Any]:
    """What the data can answer: used by the chat and the UI."""
    span = conn.execute("select min(day) s, max(day) e from mart.daily_visibility").fetchone()
    return {
        "metrics": {m.name: {"description": m.description, "unit": m.unit,
                             "dimensions": ["brand", "platform", "category"] if m.family == "visibility"
                             else ["domain", "platform", "category"]} for m in METRICS.values()},
        "chart_types": CHART_TYPES,
        "brands": [{"id": r["brand_id"], "name": r["name"], "categories": r["categories"]} for r in conn.execute(
            """select b.brand_id, b.name, array_agg(bc.category order by bc.category) categories
               from config.brands b join config.brand_categories bc using (brand_id) group by 1, 2 order by 1""")],
        "platforms": [{"id": r["platform_id"], "name": r["name"]}
                      for r in conn.execute("select platform_id, name from config.platforms order by 1")],
        "categories": [r["category"] for r in conn.execute("select category from config.categories order by 1")],
        "date_range": {"from": span["s"].isoformat() if span["s"] else None,
                       "to": span["e"].isoformat() if span["e"] else None},
        "top_domains": [r["domain"] for r in conn.execute(
            "select domain from mart.daily_citations group by 1 order by sum(citations) desc limit 25")],
    }
