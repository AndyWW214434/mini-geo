"""FastAPI app: dashboards REST API, the MCP server (/mcp/), chat, report and run status."""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID

import structlog
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field, ValidationError
from workos import WorkOSClient
from workos.organization_membership._resource import RoleSingle

from geo.api.auth import Principal, current_principal, org_admin, org_member
from geo.chat.agent import run_chat
from geo.config import settings
from geo.dashboards import repo
from geo.dashboards.charts import ChartSpec, catalog, chart_data
from geo.db import connection
from geo.mcp_server.server import mcp

log = structlog.get_logger()
mcp_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                  host="0.0.0.0")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    async with contextlib.AsyncExitStack() as stack:
        if hasattr(mcp, "session_manager"):
            await stack.enter_async_context(mcp.session_manager.run())
        elif getattr(mcp_app.router, "lifespan_context", None):
            await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
        yield


app = FastAPI(title="Mini GEO API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings().cors_origins, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/mcp", mcp_app)


def workos() -> WorkOSClient:
    s = settings()
    return WorkOSClient(api_key=s.workos_api_key, client_id=s.workos_client_id)


@app.exception_handler(repo.NotFound)
async def _not_found(_, exc: repo.NotFound):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": str(exc)}, status_code=404)


# ------------------------------------------------------------------------------ public
@app.get("/health")
def health() -> dict[str, Any]:
    with connection() as conn:
        conn.execute("select 1")
    return {"ok": True}


@app.get("/status")
def run_status() -> dict[str, Any]:
    """Are the 15-minute runs keeping up? Last windows per platform and the last pipeline steps."""
    with connection() as conn:
        collection = conn.execute("select * from ops.collection_status order by platform_id").fetchall()
        recent = conn.execute(
            """select window_start, platform_id, status, answers_inserted, error from raw.collection_runs
               order by window_start desc, platform_id limit 36""").fetchall()
        steps = conn.execute(
            """select distinct on (step) step, status, started_at, finished_at, rows_out, error
               from ops.pipeline_runs order by step, started_at desc""").fetchall()
    return {"collection": collection, "recent_runs": recent, "pipeline": steps}


@app.get("/reports/{brand_id}", response_class=HTMLResponse)
def latest_report(brand_id: str) -> str:
    with connection() as conn:
        r = conn.execute("""select html from mart.reports where focus_brand = %s
                            order by generated_at desc limit 1""", (brand_id,)).fetchone()
    if not r:
        raise HTTPException(404, "no report yet")
    return r["html"]


# ------------------------------------------------------------------------------ account
@app.get("/me")
def me(p: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"user_id": p.user_id, "org_id": p.org_id, "role": p.role}


class OrgIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)


@app.post("/orgs", status_code=201)
def create_org(body: OrgIn, p: Principal = Depends(current_principal)) -> dict[str, Any]:
    """First sign-in: create an organization and make the user its admin."""
    w = workos()
    org = w.organizations.create_organization(name=body.name)
    w.organization_membership.create_organization_membership(
        user_id=p.user_id, organization_id=org.id, role=RoleSingle(role_slug="admin"))
    log.info("org_created", org_id=org.id, user_id=p.user_id)
    return {"org_id": org.id, "name": org.name}


class InviteIn(BaseModel):
    email: EmailStr
    role: str = "member"


@app.post("/orgs/invitations", status_code=201)
def invite(body: InviteIn, p: Principal = Depends(org_admin)) -> dict[str, Any]:
    inv = workos().user_management.send_invitation(email=body.email, organization_id=p.org_id,
                                                   role_slug=body.role, inviter_user_id=p.user_id)
    return {"invitation_id": inv.id, "email": body.email}


# ------------------------------------------------------------------------------ dashboards
class DashboardIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None


class DashboardPatch(BaseModel):
    name: str | None = None
    description: str | None = None


class ChartIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    spec: ChartSpec


class ChartPatch(BaseModel):
    title: str | None = None
    spec: ChartSpec | None = None


@app.get("/catalog")
def get_catalog(_: Principal = Depends(org_member)) -> dict[str, Any]:
    with connection() as conn:
        return catalog(conn)


@app.get("/dashboards")
def dashboards(p: Principal = Depends(org_member)) -> list[dict[str, Any]]:
    with connection() as conn:
        return repo.list_dashboards(conn, p.org_id)


@app.post("/dashboards", status_code=201)
def new_dashboard(body: DashboardIn, p: Principal = Depends(org_member)) -> dict[str, Any]:
    with connection() as conn:
        return repo.create_dashboard(conn, p.org_id, p.user_id, body.name, body.description)


@app.get("/dashboards/{dashboard_id}")
def dashboard(dashboard_id: UUID, p: Principal = Depends(org_member)) -> dict[str, Any]:
    with connection() as conn:
        return repo.get_dashboard(conn, p.org_id, dashboard_id)


@app.patch("/dashboards/{dashboard_id}")
def patch_dashboard(dashboard_id: UUID, body: DashboardPatch, p: Principal = Depends(org_member)) -> dict[str, Any]:
    with connection() as conn:
        return repo.update_dashboard(conn, p.org_id, dashboard_id, body.name, body.description)


@app.delete("/dashboards/{dashboard_id}", status_code=204)
def remove_dashboard(dashboard_id: UUID, p: Principal = Depends(org_member)) -> None:
    with connection() as conn:
        repo.delete_dashboard(conn, p.org_id, dashboard_id)


@app.post("/dashboards/{dashboard_id}/charts", status_code=201)
def new_chart(dashboard_id: UUID, body: ChartIn, p: Principal = Depends(org_member)) -> dict[str, Any]:
    with connection() as conn:
        return repo.add_chart(conn, p.org_id, p.user_id, dashboard_id, body.title, body.spec)


@app.patch("/charts/{chart_id}")
def patch_chart(chart_id: UUID, body: ChartPatch, p: Principal = Depends(org_member)) -> dict[str, Any]:
    with connection() as conn:
        return repo.update_chart(conn, p.org_id, chart_id, body.title, body.spec)


@app.delete("/charts/{chart_id}", status_code=204)
def remove_chart(chart_id: UUID, p: Principal = Depends(org_member)) -> None:
    with connection() as conn:
        repo.delete_chart(conn, p.org_id, chart_id)


@app.get("/charts/{chart_id}/data")
def chart_series(chart_id: UUID, p: Principal = Depends(org_member)) -> dict[str, Any]:
    with connection() as conn:
        chart = repo.get_chart(conn, p.org_id, chart_id)
        try:
            spec = ChartSpec.model_validate(chart["spec"])
        except ValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        data = chart_data(conn, spec)
    return {"chart_id": chart_id, "title": chart["title"], "spec": data.spec, "unit": data.unit,
            "date_from": data.date_from, "date_to": data.date_to, "series": data.series}


# ------------------------------------------------------------------------------ chat
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(default_factory=list)
    dashboard_id: UUID | None = None


@app.post("/chat")
async def chat(body: ChatIn, p: Principal = Depends(org_member)) -> dict[str, Any]:
    if not settings().anthropic_api_key:
        raise HTTPException(503, "chat is not configured (ANTHROPIC_API_KEY)")
    result = await run_chat(p.token, body.message, body.history,
                            str(body.dashboard_id) if body.dashboard_id else None)
    return {"reply": result.reply, "actions": result.actions, "tool_calls": result.tool_calls}
