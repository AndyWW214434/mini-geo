"""API and MCP authorization: sessions are verified on the backend and orgs are isolated.

Uses a local RSA key pair in place of WorkOS's JWKS. Needs TEST_DATABASE_URL (migrated, seeded).
"""

from __future__ import annotations

import os
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="TEST_DATABASE_URL not set")

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class LocalVerifier:
    def verify(self, token: str):
        from geo.api.auth import InvalidToken, principal_from_claims
        try:
            claims = jwt.decode(token, KEY.public_key(), algorithms=["RS256"], options={"require": ["exp", "sub"]})
        except jwt.ExpiredSignatureError as exc:
            raise InvalidToken("session expired") from exc
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc
        return principal_from_claims(claims, token)


def token(user: str, org: str | None, role: str = "member", ttl: int = 300) -> str:
    claims = {"sub": user, "sid": f"s-{user}", "exp": int(time.time()) + ttl, "role": role}
    if org:
        claims["org_id"] = org
    return jwt.encode(claims, KEY, algorithm="RS256")


@pytest.fixture(scope="module")
def client():
    os.environ["DATABASE_URL"] = DB or ""
    from geo.config import settings
    settings.cache_clear()
    from fastapi.testclient import TestClient

    from geo.api import auth
    from geo.api.main import app
    auth.set_verifier(LocalVerifier())
    with TestClient(app) as c:
        yield c
    auth.set_verifier(None)


def h(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def mcp_call(client, tok: str, name: str, args: dict) -> dict:
    resp = client.post("/mcp/", headers={**h(tok), "Accept": "application/json, text/event-stream",
                                         "MCP-Protocol-Version": "2025-06-18"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": name, "arguments": args}})
    return resp


def test_requests_without_valid_session_are_rejected(client):
    assert client.get("/dashboards").status_code == 401
    assert client.get("/dashboards", headers=h("not-a-jwt")).status_code == 401
    assert client.get("/dashboards", headers=h(token("u1", "org_a", ttl=-120))).status_code == 401
    assert client.get("/dashboards", headers=h(token("u1", None))).status_code == 403


def test_org_cannot_see_or_change_another_orgs_dashboards(client):
    a, b = token("alice", f"org_{uuid.uuid4().hex[:6]}"), token("bob", f"org_{uuid.uuid4().hex[:6]}")
    d = client.post("/dashboards", json={"name": "A's board"}, headers=h(a)).json()
    spec = {"type": "line", "metric": "visibility", "brands": ["stripe"], "platforms": ["chatgpt"], "last_n_days": 7}
    c = client.post(f"/dashboards/{d['dashboard_id']}/charts", json={"title": "Stripe", "spec": spec}, headers=h(a))
    assert c.status_code == 201
    chart_id = c.json()["chart_id"]

    assert [x["name"] for x in client.get("/dashboards", headers=h(b)).json()] == []
    assert client.get(f"/dashboards/{d['dashboard_id']}", headers=h(b)).status_code == 404
    assert client.get(f"/charts/{chart_id}/data", headers=h(b)).status_code == 404
    assert client.post(f"/dashboards/{d['dashboard_id']}/charts", json={"title": "x", "spec": spec},
                       headers=h(b)).status_code == 404
    assert client.delete(f"/charts/{chart_id}", headers=h(b)).status_code == 404

    data = client.get(f"/charts/{chart_id}/data", headers=h(a)).json()
    assert data["series"] and data["series"][0]["label"] == "Stripe"

    # Through MCP, as Bob: Alice's dashboard is invisible and cannot be changed.
    r = mcp_call(client, b, "get_dashboard", {"dashboard_id": d["dashboard_id"]})
    assert r.status_code == 200 and r.json()["result"]["isError"] is True
    r = mcp_call(client, b, "list_dashboards", {})
    assert d["dashboard_id"] not in r.text
    r = mcp_call(client, a, "list_dashboards", {})
    assert d["dashboard_id"] in r.text


def test_mcp_requires_a_session(client):
    r = client.post("/mcp/", headers={"Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_mcp_readonly_query_cannot_touch_app_tables(client):
    tok = token("carol", "org_c")
    ok = mcp_call(client, tok, "run_query", {"sql": "select count(*) n from mart.daily_visibility"})
    assert ok.json()["result"]["isError"] is False
    denied = mcp_call(client, tok, "run_query", {"sql": "select * from app.dashboards"})
    assert denied.json()["result"]["isError"] is True
    write = mcp_call(client, tok, "run_query", {"sql": "delete from mart.daily_visibility"})
    assert write.json()["result"]["isError"] is True


def test_add_chart_refuses_empty_selection(client):
    tok = token("dave", f"org_{uuid.uuid4().hex[:6]}")
    d = client.post("/dashboards", json={"name": "d"}, headers=h(tok)).json()
    r = mcp_call(client, tok, "add_chart", {"dashboard_id": d["dashboard_id"], "title": "x",
                                             "spec": {"metric": "visibility", "brands": ["nonexistent"]}})
    assert r.json()["result"]["isError"] is True

