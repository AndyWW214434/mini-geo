"""Dashboards and charts, always scoped to one organization.

Every function takes `org_id` first and filters on it in SQL. Callers (REST API, MCP server)
pass the org from the verified session token, so there is no code path that can read or
change another organization's rows.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from geo.dashboards.charts import ChartSpec


class NotFound(Exception):
    pass


def list_dashboards(conn: psycopg.Connection, org_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        """select d.dashboard_id, d.name, d.description, d.updated_at, count(c.chart_id) as charts
           from app.dashboards d left join app.charts c on c.dashboard_id = d.dashboard_id and c.org_id = d.org_id
           where d.org_id = %s group by d.dashboard_id order by d.updated_at desc""", (org_id,)).fetchall()


def create_dashboard(conn: psycopg.Connection, org_id: str, user_id: str, name: str,
                     description: str | None = None) -> dict[str, Any]:
    return conn.execute(
        """insert into app.dashboards (org_id, name, description, created_by) values (%s, %s, %s, %s)
           returning dashboard_id, name, description, updated_at""", (org_id, name, description, user_id)).fetchone()


def get_dashboard(conn: psycopg.Connection, org_id: str, dashboard_id: UUID | str) -> dict[str, Any]:
    d = conn.execute("""select dashboard_id, name, description, created_at, updated_at from app.dashboards
                        where org_id = %s and dashboard_id = %s""", (org_id, str(dashboard_id))).fetchone()
    if not d:
        raise NotFound(f"dashboard {dashboard_id} not found")
    d["charts"] = conn.execute(
        """select chart_id, title, spec, position, updated_at from app.charts
           where org_id = %s and dashboard_id = %s order by position, created_at""",
        (org_id, str(dashboard_id))).fetchall()
    return d


def update_dashboard(conn: psycopg.Connection, org_id: str, dashboard_id: UUID | str, name: str | None,
                     description: str | None) -> dict[str, Any]:
    d = conn.execute(
        """update app.dashboards set name = coalesce(%s, name), description = coalesce(%s, description),
                  updated_at = now()
           where org_id = %s and dashboard_id = %s returning dashboard_id, name, description, updated_at""",
        (name, description, org_id, str(dashboard_id))).fetchone()
    if not d:
        raise NotFound(f"dashboard {dashboard_id} not found")
    return d


def delete_dashboard(conn: psycopg.Connection, org_id: str, dashboard_id: UUID | str) -> None:
    if not conn.execute("delete from app.dashboards where org_id = %s and dashboard_id = %s",
                        (org_id, str(dashboard_id))).rowcount:
        raise NotFound(f"dashboard {dashboard_id} not found")


def add_chart(conn: psycopg.Connection, org_id: str, user_id: str, dashboard_id: UUID | str, title: str,
              spec: ChartSpec) -> dict[str, Any]:
    get_dashboard(conn, org_id, dashboard_id)  # raises NotFound for another org's dashboard
    row = conn.execute(
        """insert into app.charts (dashboard_id, org_id, title, spec, position, created_by)
           values (%s, %s, %s, %s,
                   (select coalesce(max(position) + 1, 0) from app.charts where dashboard_id = %s), %s)
           returning chart_id, dashboard_id, title, spec, position""",
        (str(dashboard_id), org_id, title, Jsonb(spec.model_dump(mode="json")), str(dashboard_id), user_id)).fetchone()
    conn.execute("update app.dashboards set updated_at = now() where dashboard_id = %s", (str(dashboard_id),))
    return row


def get_chart(conn: psycopg.Connection, org_id: str, chart_id: UUID | str) -> dict[str, Any]:
    c = conn.execute("""select chart_id, dashboard_id, title, spec, position from app.charts
                        where org_id = %s and chart_id = %s""", (org_id, str(chart_id))).fetchone()
    if not c:
        raise NotFound(f"chart {chart_id} not found")
    return c


def update_chart(conn: psycopg.Connection, org_id: str, chart_id: UUID | str, title: str | None,
                 spec: ChartSpec | None) -> dict[str, Any]:
    c = conn.execute(
        """update app.charts set title = coalesce(%s, title), spec = coalesce(%s, spec), updated_at = now()
           where org_id = %s and chart_id = %s returning chart_id, dashboard_id, title, spec, position""",
        (title, Jsonb(spec.model_dump(mode="json")) if spec else None, org_id, str(chart_id))).fetchone()
    if not c:
        raise NotFound(f"chart {chart_id} not found")
    return c


def delete_chart(conn: psycopg.Connection, org_id: str, chart_id: UUID | str) -> None:
    if not conn.execute("delete from app.charts where org_id = %s and chart_id = %s",
                        (org_id, str(chart_id))).rowcount:
        raise NotFound(f"chart {chart_id} not found")
