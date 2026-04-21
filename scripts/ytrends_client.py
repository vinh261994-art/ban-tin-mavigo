"""YTrends MCP client over streamable HTTP (JSON-RPC 2.0).

Public server: https://mcp.trends.ytuong.ai/mcp (no auth, 60 calls/min).

Usage:
    with YTrendsClient() as y:
        snap = y.call_tool("ytrends_market_snapshot", {"country": "US"})
        kw = y.call_tool("ytrends_research_keyword", {"keyword": "linen apron"})
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from typing import Any, Optional

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MCP_URL = "https://mcp.trends.ytuong.ai/mcp"
PROTOCOL_VERSION = "2024-11-05"


class YTrendsError(RuntimeError):
    pass


class YTrendsClient:
    def __init__(self, url: str = MCP_URL, timeout: float = 60.0):
        self.url = url
        self.session_id: Optional[str] = None
        self.client = httpx.Client(
            timeout=timeout,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        self._initialized = False

    def __enter__(self) -> "YTrendsClient":
        self.initialize()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def _parse_sse(self, text: str) -> dict:
        """Parse Server-Sent Events response → first data JSON object."""
        for line in text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        # Fallback: plain JSON (some servers skip SSE wrapping)
        stripped = text.strip()
        if stripped.startswith("{"):
            return json.loads(stripped)
        raise YTrendsError(f"Unexpected response (no SSE data line): {text[:300]}")

    def _rpc(self, method: str, params: Optional[dict] = None, notification: bool = False) -> Any:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = str(uuid.uuid4())

        headers: dict[str, str] = {}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        r = self.client.post(self.url, json=payload, headers=headers)
        r.raise_for_status()

        # Capture session id from initialize response
        sid = r.headers.get("mcp-session-id")
        if sid and not self.session_id:
            self.session_id = sid

        if notification:
            return None

        data = self._parse_sse(r.text)
        if "error" in data:
            raise YTrendsError(f"MCP error from {method}: {data['error']}")
        return data.get("result")

    def initialize(self) -> None:
        if self._initialized:
            return
        self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ban-tin-mavigo", "version": "0.1.0"},
            },
        )
        self._rpc("notifications/initialized", notification=True)
        self._initialized = True

    def list_tools(self) -> list[dict]:
        if not self._initialized:
            self.initialize()
        return self._rpc("tools/list", {})["tools"]

    def call_tool(self, name: str, arguments: Optional[dict] = None, retries: int = 3) -> dict:
        """Call a YTrends tool. Returns the `result` field of the MCP response.

        Auto-retries with backoff on 429 (rate limit).
        """
        if not self._initialized:
            self.initialize()
        arguments = arguments or {}

        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return self._rpc("tools/call", {"name": name, "arguments": arguments})
            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code == 429:
                    wait = 15 * (attempt + 1)
                    print(f"[ytrends] 429 rate limit, wait {wait}s (attempt {attempt+1}/{retries})")
                    time.sleep(wait)
                    continue
                raise
            except (httpx.TransportError, YTrendsError) as e:
                last_exc = e
                if attempt + 1 < retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        raise YTrendsError(f"Tool {name} failed after {retries} retries: {last_exc}")


def extract_text_content(result: dict) -> str:
    """Extract first text block from a tool call result (MCP content format)."""
    for block in result.get("content", []):
        if block.get("type") == "text":
            return block.get("text", "")
    return ""


def extract_structured(result: dict) -> Any:
    """Extract structured content if the tool declares an outputSchema."""
    return result.get("structuredContent")


if __name__ == "__main__":
    with YTrendsClient() as y:
        tools = y.list_tools()
        print(f"[ytrends] {len(tools)} tools available:")
        for t in tools:
            print(f"  - {t['name']}")

        print("\n[ytrends] test call: ytrends_market_snapshot(country=US)")
        res = y.call_tool("ytrends_market_snapshot", {"country": "US"})
        struct = extract_structured(res)
        if struct:
            print(json.dumps(struct, indent=2, ensure_ascii=False)[:1500])
        else:
            print(extract_text_content(res)[:1500])
