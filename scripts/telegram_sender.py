"""Minimal Telegram sender.

Honors DRY_RUN=1 env var by printing to stdout instead of hitting the API.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Iterable

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4096   # Telegram hard limit for sendMessage
MAX_CAPTION_LEN = 1024   # Telegram hard limit for photo/media captions


def _smart_trim(text: str, limit: int) -> str:
    """Trim text to `limit` chars, but prefer cutting at line/word boundaries.

    Also closes dangling HTML <a>/<b>/<i> tags so Telegram doesn't reject
    the message with a parse error.
    """
    if len(text) <= limit:
        return text
    # First, try to cut at the last newline before limit
    cut = text.rfind("\n", 0, limit - 1)
    if cut == -1 or cut < int(limit * 0.6):
        # No good line break — fall back to last space
        cut = text.rfind(" ", 0, limit - 1)
    if cut == -1 or cut < int(limit * 0.6):
        cut = limit - 1
    trimmed = text[:cut].rstrip() + "…"

    # Close any dangling tags (very simple balancing — good enough for our tags)
    for tag in ("a", "b", "i", "code"):
        opens = trimmed.count(f"<{tag}")
        closes = trimmed.count(f"</{tag}>")
        while closes < opens:
            trimmed += f"</{tag}>"
            closes += 1
    return trimmed


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
            while len(buf) > limit:
                parts.append(buf[:limit])
                buf = buf[limit:]
        else:
            buf += line
    if buf:
        parts.append(buf)
    return parts


def _creds() -> tuple[str, str, bool]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    dry = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")
    return token, chat_id, dry


def send(message: str, *, parse_mode: str = "HTML") -> None:
    """Send a text message to the configured Telegram chat."""
    token, chat_id, dry = _creds()

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


def send_photo(photo_url: str, caption: str = "", *, parse_mode: str = "HTML") -> None:
    """Send a single photo by URL with HTML caption."""
    token, chat_id, dry = _creds()
    caption = _smart_trim(caption, MAX_CAPTION_LEN)

    if dry or not token or not chat_id:
        reason = "DRY_RUN" if dry else "missing creds"
        print(f"[telegram photo] {reason}: {photo_url}\n  caption: {caption[:200]}")
        return

    url = f"{TELEGRAM_API}/bot{token}/sendPhoto"
    with httpx.Client(timeout=30) as client:
        r = client.post(url, json={
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": parse_mode,
        })
        if r.status_code != 200:
            # Fall back to text-only if photo upload fails (broken image URL etc.)
            print(f"[telegram] sendPhoto failed ({r.status_code}) — falling back to text")
            send(caption + f'\n<a href="{photo_url}">[ảnh]</a>', parse_mode=parse_mode)


def send_media_group(items: Iterable[dict], *, parse_mode: str = "HTML") -> None:
    """Send an album (2-10 photos). Each item: {photo: url, caption: str}.

    Telegram requires >= 2 items for sendMediaGroup. If only 1, we route to sendPhoto.
    If 0, no-op.
    """
    items = list(items)
    if not items:
        return
    if len(items) == 1:
        send_photo(items[0]["photo"], items[0].get("caption", ""), parse_mode=parse_mode)
        return

    token, chat_id, dry = _creds()
    # Telegram allows max 10 per album
    items = items[:10]

    media = []
    for it in items:
        cap = _smart_trim(it.get("caption") or "", MAX_CAPTION_LEN)
        media.append({
            "type": "photo",
            "media": it["photo"],
            "caption": cap,
            "parse_mode": parse_mode,
        })

    if dry or not token or not chat_id:
        reason = "DRY_RUN" if dry else "missing creds"
        print(f"[telegram album] {reason} — {len(media)} ảnh:")
        for m in media:
            print(f"  • {m['media']}")
            cap_preview = m['caption'].replace("\n", " | ")
            print(f"    [{len(m['caption'])} chars] {cap_preview[:500]}")
        return

    url = f"{TELEGRAM_API}/bot{token}/sendMediaGroup"
    with httpx.Client(timeout=60) as client:
        r = client.post(url, json={
            "chat_id": chat_id,
            "media": media,
        })
        if r.status_code != 200:
            print(f"[telegram] sendMediaGroup failed ({r.status_code}): {r.text[:300]}")
            # Fall back to sending each photo separately (some URLs may fail)
            for it in items:
                try:
                    send_photo(it["photo"], it.get("caption", ""), parse_mode=parse_mode)
                except Exception as e:
                    print(f"[telegram] fallback sendPhoto failed: {e}")


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) or "<b>ban-tin-mavigo</b> test message"
    send(msg)
