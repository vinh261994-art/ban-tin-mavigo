"""Minimal Gemini 2.5 Flash REST client for weekly-report narrative generation.

Endpoint docs: https://ai.google.dev/api/generate-content

Usage:
    text = generate(
        prompt="Tóm tắt tuần qua bằng giọng đanh đá...",
        system="Bạn là bot mỉa mai chủ shop Etsy/eBay...",
    )

Honors DRY_RUN=1 env var — returns a placeholder instead of calling the API.
"""
from __future__ import annotations

import os
import sys

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MODEL = "gemini-2.5-flash"
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

DEFAULT_SYSTEM = (
    "Bạn là bot bản tin bán hàng của Mavigo. Giọng văn tiếng Việt đanh đá, "
    "mỉa mai, trêu chủ shop — kiểu như người bạn thân lật tẩy nhưng không ác ý. "
    "Ví dụ câu: 'bán thế này có mà cạp đất ăn em nhỉ?', 'vườn hồng mở cửa chả "
    "ma nào vào', 'shop ngủ đông X ngày — khách mở cửa thấy bảng đi ăn phở à?'. "
    "Tránh format markdown, tránh bullet, chỉ cần 1-2 đoạn văn xuôi ngắn, "
    "mỗi đoạn 2-4 câu. Xen kẽ giữa mỉa mai và lời khuyên cụ thể."
)


class GeminiError(RuntimeError):
    pass


def generate(prompt: str,
             system: str = DEFAULT_SYSTEM,
             temperature: float = 0.9,
             max_tokens: int = 1024) -> str:
    """Generate text via Gemini. Raises GeminiError on any failure.

    Returns a DRY_RUN placeholder if DRY_RUN=1 (so weekly_report can preview
    the rest of the report without burning API credits).
    """
    dry = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")
    if dry:
        return ("[DRY_RUN Gemini placeholder] Tuần này nhìn qua là biết, "
                "bán chậm như rùa mà cứ đòi giàu to. Vào mà xem lại listing "
                "đi, SEO title toàn từ chung chung, ai mà tìm ra shop của ông bà?")

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise GeminiError("GEMINI_API_KEY not set in environment")

    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    try:
        with httpx.Client(timeout=120.0) as client:
            r = client.post(ENDPOINT, params={"key": key}, json=body)
    except httpx.HTTPError as e:
        raise GeminiError(f"Gemini transport error: {type(e).__name__}: {e}")

    if r.status_code != 200:
        # Don't leak the API key if it somehow got into error body
        raise GeminiError(f"Gemini HTTP {r.status_code}: {r.text[:500]}")

    try:
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, ValueError) as e:
        raise GeminiError(f"Unexpected Gemini response shape ({e}): {r.text[:500]}")


if __name__ == "__main__":
    # Smoke test: `python gemini_client.py "say hi in Vietnamese sarcastic tone"`
    test_prompt = " ".join(sys.argv[1:]) or "Chào chủ shop một câu đanh đá."
    try:
        out = generate(test_prompt, max_tokens=200)
        print(out)
    except GeminiError as e:
        print(f"[gemini_client] {e}", file=sys.stderr)
        sys.exit(1)
