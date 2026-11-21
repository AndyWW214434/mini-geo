"""The dashboard chat: Claude + the MCP server, acting as the signed-in user.

The backend opens an MCP session with the user's own access token, so every tool call is
authorized as that user and scoped to their organization by the MCP server, not by this
loop. The loop is a plain Messages API tool loop: it lives in our API because it must hold
the user's session token, and it needs no sandbox.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import anthropic
import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from geo.config import settings

log = structlog.get_logger()
MAX_TOOL_ROUNDS = 12
MUTATING = {"create_dashboard", "add_chart", "update_chart", "delete_chart"}

SYSTEM = """You build dashboards for a product that tracks how AI assistants (ChatGPT, Perplexity, Gemini,
Google AI Mode, Microsoft Copilot, Grok) mention brands when buyers ask about payments, billing, payouts
and fraud.

How to work:
- Call describe_data first to learn the ids of metrics, brands, platforms, categories and the date range.
  Map names the user says ("Adyen", "ChatGPT", "this month") to those ids and dates (use `today`).
- Before adding a chart, preview it. If the data cannot answer the question (a brand, platform or metric
  we don't track, dates outside the range, or an empty preview), say so plainly and offer the closest
  chart the data does support. Never invent numbers.
- When asked for a chart, add it to the dashboard the user is looking at (its id is given below). If there
  is none, create a dashboard with a sensible name first.
- For questions that need a number rather than a chart, use run_query or preview_chart and answer with the
  figure and its date range.
- Reply in two or three short sentences: what you added or found, with the key numbers.
"""


@dataclass
class ChatResult:
    reply: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0


def _tool_text(result: Any) -> tuple[str, bool]:
    parts = [getattr(c, "text", "") for c in (getattr(result, "content", None) or [])]
    is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    return "\n".join(p for p in parts if p) or "{}", is_error


async def run_chat(token: str, message: str, history: list[dict[str, str]],
                   dashboard_id: str | None) -> ChatResult:
    s = settings()
    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    context = f"\n\nThe user is looking at dashboard id {dashboard_id}." if dashboard_id else \
        "\n\nThe user is not looking at a dashboard."
    messages: list[Any] = [{"role": m["role"], "content": m["content"]} for m in history[-12:]
                           if m.get("role") in ("user", "assistant") and m.get("content")]
    messages.append({"role": "user", "content": message})
    actions: list[dict[str, Any]] = []
    calls = 0

    async with http, streamable_http_client(s.mcp_url, http_client=http) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = [{"name": t.name, "description": t.description or "",
                      "input_schema": getattr(t, "inputSchema", None) or getattr(t, "input_schema", None)}
                     for t in listed.tools]

            for _ in range(MAX_TOOL_ROUNDS):
                resp = await client.beta.messages.create(
                    model=s.chat_model,
                    max_tokens=16000,
                    system=SYSTEM + context,
                    tools=tools,
                    messages=messages,
                    output_config={"effort": "medium"},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
                messages.append({"role": "assistant", "content": resp.content})
                if resp.stop_reason == "refusal":
                    return ChatResult("I can't help with that request.", actions, calls)
                if resp.stop_reason == "pause_turn":
                    continue
                uses = [b for b in resp.content if b.type == "tool_use"]
                if resp.stop_reason != "tool_use" or not uses:
                    text = " ".join(b.text for b in resp.content if b.type == "text").strip()
                    return ChatResult(text or "Done.", actions, calls)
                results = []
                for b in uses:  # all results go back in one user message
                    calls += 1
                    try:
                        out = await session.call_tool(b.name, b.input)
                        text, is_error = _tool_text(out)
                    except Exception as exc:  # a failed tool is reported to the model, not dropped
                        text, is_error = f"Error: {exc}", True
                    log.info("chat_tool", tool=b.name, error=is_error)
                    if b.name in MUTATING and not is_error:
                        actions.append({"tool": b.name, "result": _safe_json(text)})
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": text,
                                    "is_error": is_error})
                messages.append({"role": "user", "content": results})
    return ChatResult("I stopped after too many steps. Try a narrower request.", actions, calls)


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text
