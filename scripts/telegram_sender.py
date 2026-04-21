"""Minimal Telegram sender.

Honors DRY_RUN=1 env var by printing to stdout instead of hitting the API.
"""
from __future__ import annotations

import os
import sys

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4096   # Telegram hard limit


def _split(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split long text into Telegram-sized chunks on newline boundaries."""
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit:
            if buf:
                parts.append(buf)
            buf = line
            # Single line longer than limit (rare) — hard-chop
            while len(buf) > limit:
                parts.append(buf[:limit])
                buf = buf[limit:]
        else:
            buf += line
    if buf:
        parts.append(buf)
    return parts


def send(message: str, *, parse_mode: str = "HTML") -> None:
    """Send a message to the configured Telegram chat.

    Reads TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DRY_RUN from env.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    dry = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")

    if dry or not token or not chat_id:
        reason = "DRY_RUN" if dry else "missing TELEGRAM_BOT_TOKEN/CHAT_ID"
        print(f"[telegram] {reason} — printing instead:\n{'─' * 60}")
        print(message)
        print("─" * 60)
        return

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    for chunk in _split(message):
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            })
            if r.status_code != 200:
                raise RuntimeError(f"Telegram API {r.status_code}: {r.text}")


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) or "<b>ban-tin-mavigo</b> test message ✅"
    send(msg)
