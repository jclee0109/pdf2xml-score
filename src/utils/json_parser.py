import json
import re
import logging

log = logging.getLogger(__name__)


def parse_json_response(raw: str, context: str = "") -> dict | list | None:
    """Claude 응답에서 JSON 추출. 실패 시 None 반환."""
    # 1차: ```json ... ``` 블록 또는 naked JSON 추출
    patterns = [
        r"```json\s*([\s\S]*?)```",
        r"```\s*([\s\S]*?)```",
        r"(\[[\s\S]*\])",
        r"(\{[\s\S]*\})",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw)
        if match:
            candidate = match.group(1).strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

    log.warning(f"JSON parse failed [{context}]: {raw[:200]}")
    return None
