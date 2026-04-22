"""Pass 1: 구조 분석 — 악기 목록, 시스템 레이아웃, 마디번호, 조표, 반복기호"""
import base64
import json
import logging
from pathlib import Path

from PIL import Image

from ..models.score import (
    PartInfo, SystemInfo, ScoreLayout,
    RehearsalMark, RepeatBarline, VoltaBracket,
    TRANSPOSITION_TABLE,
)
from ..utils.json_parser import parse_json_response

log = logging.getLogger(__name__)
MODEL = "claude-opus-4-7"


def _get_client():
    import anthropic
    return anthropic.Anthropic()


def _image_to_base64(img: Image.Image) -> str:
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def _call_claude(img: Image.Image, prompt: str) -> str:
    client = _get_client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _image_to_base64(img),
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return response.content[0].text


# ── Step 1-A: 악기 목록 ────────────────────────────────────────────────────────

STEP1A_PROMPT = """이 악보의 첫 페이지야.
왼쪽 끝에 적힌 악기 이름을 모두 읽어줘.
위에서 아래 순서대로, 각 악기의 클레프(treble/bass/alto/tenor)도 함께 알려줘.
Piano처럼 treble + bass 두 단이 있는 악기는 두 줄로 나눠서 표기해.

반드시 JSON만 응답해. 설명 없이:
{
  "parts": [
    {"name": "Piccolo",      "order": 0, "clef": "treble"},
    {"name": "Flute",        "order": 1, "clef": "treble"},
    {"name": "Piano treble", "order": 14, "clef": "treble"},
    {"name": "Piano bass",   "order": 15, "clef": "bass"}
  ]
}"""


def extract_parts(first_page_img: Image.Image) -> list[PartInfo]:
    raw = _call_claude(first_page_img, STEP1A_PROMPT)
    data = parse_json_response(raw, "step1a")
    if data is None:
        raise RuntimeError("Step 1-A: 악기 목록 파싱 실패")

    parts = []
    for i, p in enumerate(data["parts"]):
        name = p["name"]
        parts.append(PartInfo(
            id=f"P{i}",
            name=name,
            order=p["order"],
            clef=p.get("clef", "treble"),
            transposition_semitones=TRANSPOSITION_TABLE.get(name, 0),
        ))
    return parts


# ── Step 1-B: 페이지별 시스템 구조 ────────────────────────────────────────────

STEP1B_PROMPT = """이 악보 페이지에서 구조 정보만 추출해줘. 음표와 코드는 무시해.

1. 시스템 수 (가로 줄 수)
2. 각 시스템의 첫 마디 번호 (시스템 왼쪽 끝에 인쇄된 숫자)
3. 조표: concert pitch 기준 조성 (예: "G major", "F minor")
4. 박자표 (변화가 있으면 어느 마디에서 바뀌는지)
5. 각 시스템에 포함된 악기 이름 목록 (쉬어서 생략된 파트 제외)
6. 리허설 마크 (박스/원 안 문자, 예: A B C)
7. 반복 기호 (repeat barline 시작/끝, 볼타 브라켓)
8. 각 시스템의 y 좌표 (픽셀, 이미지 상단 기준)

반드시 JSON만 응답해. 설명 없이:
{
  "systems": [
    {
      "start_measure": 1,
      "key": "G major",
      "time": "4/4",
      "y_top_px": 120,
      "y_bottom_px": 450,
      "active_parts": ["Piccolo", "Flute", "Piano treble", "Piano bass"],
      "rehearsal_marks": [{"measure": 1, "label": "A"}],
      "repeat_barlines": [{"measure": 5, "type": "start"}],
      "volta_brackets": []
    }
  ]
}"""


def extract_systems(page_img: Image.Image, page_num: int,
                    name_to_id: dict[str, str]) -> list[SystemInfo]:
    raw = _call_claude(page_img, STEP1B_PROMPT)
    data = parse_json_response(raw, f"step1b_page{page_num}")
    if data is None:
        log.warning(f"Page {page_num}: 시스템 구조 파싱 실패, 빈 시스템 반환")
        return []

    systems = []
    for idx, s in enumerate(data.get("systems", [])):
        # active_parts: 이름 → ID 변환 (매핑 실패는 스킵)
        active_ids = []
        for name in s.get("active_parts", []):
            pid = name_to_id.get(name)
            if pid is None:
                # fuzzy: 부분 문자열 매칭
                pid = next((v for k, v in name_to_id.items() if name in k or k in name), None)
            if pid:
                active_ids.append(pid)
            else:
                log.warning(f"Page {page_num} system {idx}: 파트 이름 매핑 실패 '{name}'")

        system = SystemInfo(
            page=page_num,
            system_index=idx,
            start_measure=s["start_measure"],
            end_measure=0,  # 다음 시스템 처리 후 채움
            key=s.get("key", "C major"),
            time_signature=s.get("time", "4/4"),
            y_top_px=s["y_top_px"],
            y_bottom_px=s["y_bottom_px"],
            active_parts=active_ids,
            rehearsal_marks=[
                RehearsalMark(m["measure"], m["label"])
                for m in s.get("rehearsal_marks", [])
            ],
            repeat_barlines=[
                RepeatBarline(m["measure"], m["type"])
                for m in s.get("repeat_barlines", [])
            ],
            volta_brackets=[
                VoltaBracket(m["start_measure"], m["end_measure"], m["number"])
                for m in s.get("volta_brackets", [])
            ],
        )
        systems.append(system)
    return systems


def _fill_end_measures(all_systems: list[SystemInfo], total_measures: int) -> None:
    """end_measure를 다음 시스템 start - 1로 채운다."""
    for i, sys in enumerate(all_systems):
        if i + 1 < len(all_systems):
            sys.end_measure = all_systems[i + 1].start_measure - 1
        else:
            sys.end_measure = total_measures


# ── 메인 진입점 ────────────────────────────────────────────────────────────────

def layout_from_json(path: str | Path) -> ScoreLayout:
    """사전 추출된 JSON 파일에서 ScoreLayout 로드."""
    data = json.loads(Path(path).read_text())

    parts = [
        PartInfo(
            id=p["id"], name=p["name"], order=p["order"],
            clef=p["clef"],
            transposition_semitones=TRANSPOSITION_TABLE.get(p["name"], 0),
        )
        for p in data["parts"]
    ]
    name_to_id = {p.name: p.id for p in parts}

    systems = []
    for s in data["systems"]:
        systems.append(SystemInfo(
            page=s["page"],
            system_index=s["system_index"],
            start_measure=s["start_measure"],
            end_measure=s["end_measure"],
            key=s["key"],
            time_signature=s.get("time_signature") or s.get("time", "4/4"),
            y_top_px=s["y_top_px"],
            y_bottom_px=s["y_bottom_px"],
            active_parts=s["active_parts"],
            rehearsal_marks=[RehearsalMark(**m) for m in s.get("rehearsal_marks", [])],
            repeat_barlines=[RepeatBarline(**m) for m in s.get("repeat_barlines", [])],
            volta_brackets=[VoltaBracket(**m) for m in s.get("volta_brackets", [])],
        ))

    return ScoreLayout(
        parts=parts,
        systems=systems,
        total_measures=data["total_measures"],
        name_to_id=name_to_id,
    )


def run_pass1(page_images: list[Image.Image]) -> ScoreLayout:
    """전체 Pass 1 실행. ScoreLayout 반환."""
    log.info("Pass 1: 악기 목록 추출 (첫 페이지)")
    parts = extract_parts(page_images[0])
    name_to_id = {p.name: p.id for p in parts}
    log.info(f"  → {len(parts)}개 파트 감지: {[p.name for p in parts]}")

    all_systems: list[SystemInfo] = []
    for page_num, img in enumerate(page_images, start=1):
        log.info(f"Pass 1: 페이지 {page_num}/{len(page_images)} 시스템 구조 추출")
        systems = extract_systems(img, page_num, name_to_id)
        all_systems.extend(systems)
        log.info(f"  → {len(systems)}개 시스템 감지")

    total_measures = all_systems[-1].start_measure + 20 if all_systems else 0
    _fill_end_measures(all_systems, total_measures)

    # total_measures: 마지막 시스템 end_measure 기준
    if all_systems:
        total_measures = all_systems[-1].end_measure

    layout = ScoreLayout(
        parts=parts,
        systems=all_systems,
        total_measures=total_measures,
        name_to_id=name_to_id,
    )
    log.info(f"Pass 1 완료: {len(parts)}파트, {len(all_systems)}시스템, {total_measures}마디")
    return layout
