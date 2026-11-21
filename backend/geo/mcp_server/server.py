"""MCP server: the only way the AI chat reaches data and dashboards.

Mounted at /mcp/ on the API. It is an OAuth resource server: every request carries the
signed-in user's WorkOS access token, verified with the same verifier as the REST API. Each
tool reads the organization from that token, so the chat can only see and change the
dashboards of the user it acts for. Free-form SQL runs as `geo_readonly`, a role that can
read the metric tables and nothing else.

Adding a tool = one decorated function below.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import psycopg
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from pydantic import ValidationError

from geo.api.auth import InvalidToken, Principal, principal_from_claims, verifier
from geo.config import settings
from geo.dashboards import repo
from geo.dashboards.charts import ChartSpec, catalog, chart_data
from geo.db import connection

QUERY_ROW_LIMIT = 200
QUERY_TIMEOUT = "5s"


class WorkOSTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            p = verifier().verify(token)
        except InvalidToken:
            return None
        return AccessToken(token=token, client_id="geo-chat", scopes=[], expires_at=p.expires_at,
                           subject=p.user_id, claims={"org_id": p.org_id, "role": p.role, "sid": p.session_id})


mcp = MCPServer(
    name="mini-geo",
    instructions=(
        "Tools over AI-assistant visibility metrics (daily, per brand / assistant / category / cited domain) "
        "and the signed-in user's organization dashboards. Call describe_data first."),
    token_verifier=WorkOSTokenVerifier(),
    auth=AuthSettings(issuer_url="https://api.workos.com", resource_server_url=f"{settings().public_api_url}/mcp/",
                      validate_token_resource=False),
)


def _principal() -> Principal:
    tok = get_access_token()
    if tok is None or not tok.subject:
        raise PermissionError("not authenticated")
    claims = tok.claims or {}
    p = principal_from_claims({"sub": tok.subject, "org_id": claims.get("org_id"), "role": claims.get("role"),
                               "sid": claims.get("sid"), "exp": tok.expires_at}, tok.token)
    if not p.org_id:
        raise PermissionError("the user has no organization selected")
    return p


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str)


def _spec(spec: dict[str, Any]) -> ChartSpec:
    try:
        return ChartSpec.model_validate(spec)
    except ValidationError as exc:
        raise ValueError(f"invalid chart spec: {exc.errors(include_url=False)}") from exc


@mcp.tool()
def describe_data() -> str:
    """What the data covers: metrics (with units and dimensions), chart types, brands with their
    categories, assistants (platforms), prompt categories, the date range, frequent cited domains,
    and the tables available to run_query. Call this before building a chart."""
    _principal()
    with connection() as conn:
        out = catalog(conn)
    out["query_tables"] = {
        "mart.daily_visibility": "day, platform_id, brand_id, real_answers, mentioning_answers, visibility "
                                 "(null when real_answers=0), avg_position, total_answers",
        "mart.daily_category_visibility": "same as daily_visibility plus category",
        "mart.daily_citations": "day, platform_id, category, domain, citing_answers, citations, real_answers, "
                                "total_citations, citation_share, answer_rate",
        "mart.daily_platform_health": "day, platform_id, total, ok, error, empty, not_answer, truncated, wrong_prompt",
        "config.brands / config.platforms / config.prompts / config.brand_categories": "catalog",
    }
    out["notes"] = ("Visibility = share of real answers to prompts in the brand's categories that name it. "
                    "Pool ratios over days as sum(mentioning_answers)/sum(real_answers), never avg(visibility).")
    return _json(out)


@mcp.tool()
def run_query(sql: str) -> str:
    """Run one read-only SQL SELECT over the metric tables listed by describe_data (max 200 rows,
    5 s timeout). Use it to answer questions or check what a chart would show. Dashboards are not
    readable here; use the dashboard tools."""
    _principal()
    statement = sql.strip().rstrip(";")
    if ";" in statement:
        raise ValueError("one statement only")
    with connection() as conn:
        try:
            conn.execute("set transaction read only")
            conn.execute(f"set local statement_timeout = '{QUERY_TIMEOUT}'")
            conn.execute("set local role geo_readonly")
            cur = conn.execute(statement)
            rows = cur.fetchmany(QUERY_ROW_LIMIT + 1) if cur.description else []
        except psycopg.Error as exc:
            conn.rollback()
            raise ValueError(f"query failed: {exc.diag.message_primary or exc}") from exc
    return _json({"rows": rows[:QUERY_ROW_LIMIT], "truncated": len(rows) > QUERY_ROW_LIMIT})


@mcp.tool()
def preview_chart(spec: dict[str, Any]) -> str:
    """Compute a chart without saving it. spec fields: type (line|bar|table|number), metric,
    group_by (brand|platform|category|domain), brands, platforms, categories, domains (lists of ids),
    date_from / date_to (YYYY-MM-DD) or last_n_days. Returns the series so you can check the data
    answers the question before adding it to a dashboard."""
    _principal()
    with connection() as conn:
        data = chart_data(conn, _spec(spec))
    return _json({"empty": data.empty, "unit": data.unit, "date_from": data.date_from, "date_to": data.date_to,
                  "series": [{"label": s["label"], "points": s["points"][-10:], "n_points": len(s["points"])}
                             for s in data.series[:12]]})


@mcp.tool()
def list_dashboards() -> str:
    """The signed-in user's organization's dashboards."""
    p = _principal()
    with connection() as conn:
        return _json(repo.list_dashboards(conn, p.org_id))


@mcp.tool()
def create_dashboard(name: str, description: str | None = None) -> str:
    """Create an empty dashboard in the user's organization."""
    p = _principal()
    with connection() as conn:
        return _json(repo.create_dashboard(conn, p.org_id, p.user_id, name, description))


@mcp.tool()
def get_dashboard(dashboard_id: str) -> str:
    """A dashboard with its charts (titles and specs)."""
    p = _principal()
    with connection() as conn:
        return _json(repo.get_dashboard(conn, p.org_id, dashboard_id))


@mcp.tool()
def add_chart(dashboard_id: str, title: str, spec: dict[str, Any]) -> str:
    """Add a chart to a dashboard. Refuses a chart whose selection has no data, so the user never
    gets an empty chart; say so instead and suggest what the data can show."""
    p = _principal()
    chart = _spec(spec)
    with connection() as conn:
        if chart_data(conn, chart).empty:
            raise ValueError("this selection has no data; check ids and dates with describe_data")
        return _json(repo.add_chart(conn, p.org_id, p.user_id, dashboard_id, title, chart))


@mcp.tool()
def update_chart(chart_id: str, title: str | None = None, spec: dict[str, Any] | None = None) -> str:
    """Change a chart's title and/or spec (the whole spec is replaced)."""
    p = _principal()
    with connection() as conn:
        return _json(repo.update_chart(conn, p.org_id, chart_id, title, _spec(spec) if spec else None))


@mcp.tool()
def delete_chart(chart_id: str) -> str:
    """Remove a chart from its dashboard."""
    p = _principal()
    with connection() as conn:
        repo.delete_chart(conn, p.org_id, chart_id)
    return _json({"deleted": chart_id})


@mcp.tool()
def today() -> str:
    """Today's date (UTC) and the last day with data, for resolving 'this month' or 'last week'."""
    _principal()
    with connection() as conn:
        last = conn.execute("select max(day) d from mart.daily_visibility").fetchone()["d"]
    return _json({"today": date.today().isoformat(), "last_day_with_data": last})
