"""The chat agent's MCP client path over real HTTP, as the signed-in user (no LLM call)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path_factory.mktemp("k") / "pub.pem"
    pem.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen([sys.executable, str(Path(__file__).parent / "_local_api.py"), str(pem), str(port)],
                            env={**os.environ, "DATABASE_URL": DB, "PYTHONPATH": str(Path(__file__).parents[1])})
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield port, key
    proc.terminate()
    proc.wait(5)


def test_mcp_session_as_user(server):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    from geo.chat.agent import _tool_text

    port, key = server
    tok = jwt.encode({"sub": "erin", "org_id": f"org_{uuid.uuid4().hex[:6]}", "exp": int(time.time()) + 300},
                     key, algorithm="RS256")

    async def go() -> set[str]:
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {tok}"})
        async with http, streamable_http_client(f"http://127.0.0.1:{port}/mcp/", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                created, err = _tool_text(await s.call_tool("create_dashboard", {"name": "via mcp"}))
                assert not err, created
                added, err = _tool_text(await s.call_tool("add_chart", {
                    "dashboard_id": json.loads(created)["dashboard_id"], "title": "Stripe vs Adyen on ChatGPT",
                    "spec": {"type": "line", "metric": "visibility", "brands": ["stripe", "adyen"],
                             "platforms": ["chatgpt"], "last_n_days": 30}}))
                assert not err, added
                return names

    names = asyncio.run(asyncio.wait_for(go(), 30))
    assert {"describe_data", "run_query", "preview_chart", "add_chart", "create_dashboard"} <= names
