"""LLM 추상화 — Anthropic / Gemini 통합 인터페이스

환경변수:
  LLM_PROVIDER=anthropic  (기본값) → ANTHROPIC_API_KEY 필요
  LLM_PROVIDER=gemini              → GOOGLE_API_KEY 필요
"""
from __future__ import annotations

import base64
import io
import os
import logging

from PIL import Image

log = logging.getLogger(__name__)

PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")

# 모델 ID
MODELS = {
    "anthropic": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    "gemini":    os.getenv("GEMINI_MODEL",    "gemini-2.5-flash"),
}


def call_vision(image: Image.Image, prompt: str, max_tokens: int = 4096) -> str:
    """이미지 + 텍스트 프롬프트 → 응답 텍스트."""
    if PROVIDER == "gemini":
        return _call_gemini(image, prompt)
    return _call_anthropic(image, prompt, max_tokens)


# ── Anthropic ─────────────────────────────────────────────────────────────────

def _call_anthropic(image: Image.Image, prompt: str, max_tokens: int) -> str:
    import anthropic

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode()

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODELS["anthropic"],
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": b64,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return response.content[0].text


# ── Gemini ────────────────────────────────────────────────────────────────────

def _call_gemini(image: Image.Image, prompt: str) -> str:
    from google import genai

    client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    response = client.models.generate_content(
        model=MODELS["gemini"],
        contents=[image, prompt],
    )
    return response.text
