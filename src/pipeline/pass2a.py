"""Pass 2a: 코드 심볼 추출 — Piano 영역 crop + 구조 컨텍스트 주입"""
import base64
import io
import logging

import anthropic
from PIL import Image

from ..models.score import RawChord, ScoreLayout, SystemInfo
from ..utils.json_parser import parse_json_response
from ..utils.render import crop_part_range

log = logging.getLogger(__name__)
client = anthropic.Anthropic()
MODEL = "claude-opus-4-7"

PIANO_TREBLE_NAMES = {"Piano treble", "Piano"}
PIANO_BASS_NAMES = {"Piano bass"}


def _image_to_base64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def _build_prompt(start: int, end: int, key: str, time_sig: str) -> str:
    return f"""이 이미지는 악보의 마디 {start}~{end}에 해당하는 Piano 파트야.
현재 조성: {key}, 박자: {time_sig}

보표 위에 적힌 코드 심볼을 모두 추출해줘.
각 코드의 마디 번호는 이미지 왼쪽 숫자를 기준으로 확인해.
코드가 없는 마디는 건너뛰어.

반드시 JSON만 응답해. 설명 없이:
[
  {{"measure": 1, "beat": 1.0, "chord": "G", "confidence": 0.98}},
  {{"measure": 2, "beat": 1.0, "chord": "Gmaj7", "confidence": 0.95}}
]

읽기 어려운 경우 confidence를 낮게 (< 0.7) 표시해."""


def _find_piano_indices(system: SystemInfo, layout: ScoreLayout) -> tuple[int, int] | None:
    """active_parts 내 Piano treble/bass 인덱스 반환. 없으면 None."""
    treble_id = next((pid for pid in system.active_parts
                      if layout.parts[int(pid[1:])].name in PIANO_TREBLE_NAMES), None)
    bass_id = next((pid for pid in system.active_parts
                    if layout.parts[int(pid[1:])].name in PIANO_BASS_NAMES), None)

    if treble_id is None:
        return None

    treble_idx = system.active_parts.index(treble_id)
    # bass가 없으면 treble만 crop
    bass_idx = system.active_parts.index(bass_id) if bass_id else treble_idx
    return treble_idx, bass_idx


def extract_chords_for_system(
    page_img: Image.Image,
    system: SystemInfo,
    layout: ScoreLayout,
) -> list[RawChord]:
    indices = _find_piano_indices(system, layout)
    if indices is None:
        log.debug(f"System {system.system_index} (p{system.page}): Piano 파트 없음, 스킵")
        return []

    treble_idx, bass_idx = indices
    n_parts = len(system.active_parts)

    cropped = crop_part_range(
        page_img,
        system.y_top_px, system.y_bottom_px,
        treble_idx, bass_idx, n_parts,
    )

    prompt = _build_prompt(system.start_measure, system.end_measure,
                           system.key, system.time_signature)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _image_to_base64(cropped),
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    raw = response.content[0].text
    data = parse_json_response(raw, f"pass2a_p{system.page}_s{system.system_index}")

    if data is None:
        log.warning(f"Pass 2a: 시스템 p{system.page}/s{system.system_index} 파싱 실패")
        return []

    chords = []
    for item in data:
        try:
            chords.append(RawChord(
                measure=int(item["measure"]),
                beat=float(item.get("beat", 1.0)),
                chord_text=str(item["chord"]),
                confidence=float(item.get("confidence", 0.5)),
                source_page=system.page,
                source_system=system.system_index,
            ))
        except (KeyError, ValueError) as e:
            log.warning(f"Pass 2a: 코드 항목 파싱 오류 {item}: {e}")

    return chords


def run_pass2a(page_images: list[Image.Image], layout: ScoreLayout) -> list[RawChord]:
    """전체 Pass 2a 실행. RawChord 목록 반환."""
    all_chords: list[RawChord] = []

    for system in layout.systems:
        page_img = page_images[system.page - 1]
        log.info(f"Pass 2a: p{system.page} s{system.system_index} "
                 f"(m{system.start_measure}~{system.end_measure}, {system.key})")
        chords = extract_chords_for_system(page_img, system, layout)
        all_chords.extend(chords)
        log.info(f"  → {len(chords)}개 코드 추출")

    log.info(f"Pass 2a 완료: 총 {len(all_chords)}개 코드")
    return all_chords
