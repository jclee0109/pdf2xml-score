"""Pass 2b: 음표 추출 — Tier 1~4 per system"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from PIL import Image

from ..models.score import RawNote, ScoreLayout, SystemInfo
from ..utils.json_parser import parse_json_response
from ..utils.render import crop_part_range
from ..utils.llm import call_vision

log = logging.getLogger(__name__)

PIANO_TREBLE_NAMES = {"Piano treble", "Piano"}
PIANO_BASS_NAMES   = {"Piano bass"}

# 파트 이름 기준 Tier 분류
TIERS: dict[int, list[str]] = {
    1: ["Piano treble", "Piano bass"],
    2: ["Violin I", "Violin II", "Viola", "Violoncello", "Contrabass"],
    3: ["Piccolo", "Flute", "Oboe", "Clarinet in Bb", "Bassoon"],
    4: ["Trumpet in Bb 1/2", "Trumpet in Bb 3",
        "Horn in F 1/2", "Horn in F 3/4",
        "Tenor Trombone 1/2", "Bass Trombone", "Tuba", "Timpani"],
}

# Electric Bass는 단독 처리 (treble 악기 사이에 없음)
TIER_EXTRA: list[str] = ["Electric Bass"]





def _build_prompt(start: int, end: int, key: str, time_sig: str) -> str:
    return f"""이 이미지는 오케스트라 악보의 마디 {start}~{end}야.
Piano treble(위 보표)과 Piano bass(아래 보표) 두 단이 보여.
조성: {key} (concert pitch 기준), 박자: {time_sig}

두 파트의 모든 음표를 추출해줘.

규칙:
- pitch: 악보에 적힌 그대로의 음이름+옥타브 (예: G4, F#5, Bb3, rest)
- duration: whole / half / quarter / eighth / 16th / 32nd
- dots: 점음표 수 (0, 1, 2)
- beat: 마디 내 박 위치 (1.0, 2.0, 3.0 … 또는 1.5 등)
- voice: 한 보표 내 성부 (1 또는 2)
- tie_start / tie_end: 붙임줄 시작/끝 여부
- confidence: 읽기 어려우면 낮게 (< 0.7)
- 이조 변환 하지 말 것. 적힌 그대로 출력.

반드시 JSON만 응답해. 설명 없이:
{{
  "Piano treble": {{
    "{start}": [{{"beat": 1.0, "pitch": "D5", "duration": "half", "dots": 1, "voice": 1, "tie_start": false, "tie_end": false, "confidence": 0.9}}],
    "{start + 1}": [...]
  }},
  "Piano bass": {{
    "{start}": [...],
    "{start + 1}": [...]
  }}
}}"""


def _find_piano_indices(system: SystemInfo, layout: ScoreLayout) -> tuple[int, int] | None:
    """active_parts 내 Piano treble/bass 인덱스. 없으면 None."""
    def part_name(pid: str) -> str:
        idx = int(pid[1:])
        return layout.parts[idx].name

    treble_id = next(
        (pid for pid in system.active_parts if part_name(pid) in PIANO_TREBLE_NAMES), None
    )
    bass_id = next(
        (pid for pid in system.active_parts if part_name(pid) in PIANO_BASS_NAMES), None
    )

    if treble_id is None:
        return None
    treble_idx = system.active_parts.index(treble_id)
    bass_idx   = system.active_parts.index(bass_id) if bass_id else treble_idx
    return treble_idx, bass_idx


def _parse_notes_from_response(
    data: dict,
    system: SystemInfo,
    treble_id: str,
    bass_id: str | None,
) -> list[RawNote]:
    notes: list[RawNote] = []

    staff_map = {
        "Piano treble": treble_id,
        "Piano bass":   bass_id or treble_id,
    }

    for staff_label, part_id in staff_map.items():
        by_measure: dict = data.get(staff_label, {})
        for measure_str, note_list in by_measure.items():
            try:
                measure_num = int(measure_str)
            except ValueError:
                continue
            if not (system.start_measure <= measure_num <= system.end_measure):
                continue

            for item in note_list:
                try:
                    notes.append(RawNote(
                        measure=measure_num,
                        beat=float(item.get("beat", 1.0)),
                        pitch=str(item["pitch"]),
                        duration=str(item.get("duration", "quarter")),
                        dots=int(item.get("dots", 0)),
                        tie_start=bool(item.get("tie_start", False)),
                        tie_end=bool(item.get("tie_end", False)),
                        voice=int(item.get("voice", 1)),
                        confidence=float(item.get("confidence", 0.5)),
                        part_id=part_id,
                        source_system=system.system_index,
                    ))
                except (KeyError, ValueError, TypeError) as e:
                    log.warning(f"Pass 2b: 음표 항목 파싱 오류 {item}: {e}")

    return notes


def extract_notes_for_system(
    page_img: Image.Image,
    system: SystemInfo,
    layout: ScoreLayout,
) -> list[RawNote]:
    indices = _find_piano_indices(system, layout)
    if indices is None:
        log.debug(f"System {system.system_index} (p{system.page}): Piano 없음, 스킵")
        return []

    treble_idx, bass_idx = indices

    def part_id_at(idx: int) -> str:
        return system.active_parts[idx]

    treble_id = part_id_at(treble_idx)
    bass_id   = part_id_at(bass_idx) if bass_idx != treble_idx else None

    cropped = crop_part_range(
        page_img,
        system.y_top_px, system.y_bottom_px,
        treble_idx, bass_idx, len(system.active_parts),
        extra_top=25, extra_bottom=10,
    )

    prompt = _build_prompt(
        system.start_measure, system.end_measure,
        system.key, system.time_signature,
    )
    raw  = call_vision(cropped, prompt)
    data = parse_json_response(raw, f"pass2b_p{system.page}_s{system.system_index}")

    if data is None:
        log.warning(f"Pass 2b: p{system.page}/s{system.system_index} 파싱 실패")
        return []

    notes = _parse_notes_from_response(data, system, treble_id, bass_id)
    return notes


# ── 파일 기반 로더 ─────────────────────────────────────────────────────────────

def notes_from_json(path: str | Path) -> list[RawNote]:
    """사전 추출된 JSON에서 RawNote 로드."""
    data = json.loads(Path(path).read_text())
    return [
        RawNote(
            measure=n["measure"], beat=n["beat"],
            pitch=n["pitch"], duration=n["duration"],
            dots=n["dots"], tie_start=n["tie_start"], tie_end=n["tie_end"],
            voice=n["voice"], confidence=n["confidence"],
            part_id=n["part_id"], source_system=n["source_system"],
        )
        for n in data
    ]


def notes_to_json(notes: list[RawNote], path: str | Path) -> None:
    data = [
        {
            "measure": n.measure, "beat": n.beat,
            "pitch": n.pitch, "duration": n.duration,
            "dots": n.dots, "tie_start": n.tie_start, "tie_end": n.tie_end,
            "voice": n.voice, "confidence": n.confidence,
            "part_id": n.part_id, "source_system": n.source_system,
        }
        for n in notes
    ]
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ── Tier 2~4: 단일 파트 추출 ──────────────────────────────────────────────────

def _build_prompt_single(
    part_name: str, start: int, end: int, key: str, time_sig: str, clef: str
) -> str:
    return f"""이 이미지는 오케스트라 악보의 마디 {start}~{end}야.
{part_name} 파트 한 단이 보여.
조성: {key} (concert pitch 기준), 박자: {time_sig}, 클레프: {clef}

이 파트의 모든 음표를 추출해줘.

규칙:
- pitch: 악보에 적힌 그대로의 음이름+옥타브 (예: G4, F#5, Bb3, rest)
- duration: whole / half / quarter / eighth / 16th / 32nd
- dots: 점음표 수 (0, 1, 2)
- beat: 마디 내 박 위치 (1.0, 2.0 …)
- voice: 성부 (1 또는 2)
- tie_start / tie_end: 붙임줄
- confidence: 읽기 어려우면 낮게 (< 0.7)
- 이조 변환 하지 말 것.

반드시 JSON만 응답해:
{{
  "{start}": [{{"beat": 1.0, "pitch": "G4", "duration": "quarter", "dots": 0, "voice": 1, "tie_start": false, "tie_end": false, "confidence": 0.9}}],
  "{start + 1}": [...]
}}"""


def extract_notes_single_part(
    page_img: Image.Image,
    system: SystemInfo,
    layout: ScoreLayout,
    part_name: str,
) -> list[RawNote]:
    """단일 파트 음표 추출."""
    active = system.active_parts

    def part_name_of(pid: str) -> str:
        return layout.parts[int(pid[1:])].name

    part_id = next(
        (pid for pid in active if part_name_of(pid) == part_name), None
    )
    if part_id is None:
        return []

    idx = active.index(part_id)
    part_info = layout.parts[int(part_id[1:])]

    cropped = crop_part_range(
        page_img,
        system.y_top_px, system.y_bottom_px,
        idx, idx, len(active),
        extra_top=20, extra_bottom=8,
    )

    prompt = _build_prompt_single(
        part_name, system.start_measure, system.end_measure,
        system.key, system.time_signature, part_info.clef,
    )
    raw  = call_vision(cropped, prompt)
    data = parse_json_response(raw, f"pass2b_{part_name}_p{system.page}")

    if data is None or not isinstance(data, dict):
        log.warning(f"Pass 2b [{part_name}]: p{system.page} 파싱 실패")
        return []

    notes: list[RawNote] = []
    for measure_str, note_list in data.items():
        try:
            m_num = int(measure_str)
        except ValueError:
            continue
        if not (system.start_measure <= m_num <= system.end_measure):
            continue
        for item in note_list:
            try:
                notes.append(RawNote(
                    measure=m_num,
                    beat=float(item.get("beat", 1.0)),
                    pitch=str(item["pitch"]),
                    duration=str(item.get("duration", "quarter")),
                    dots=int(item.get("dots", 0)),
                    tie_start=bool(item.get("tie_start", False)),
                    tie_end=bool(item.get("tie_end", False)),
                    voice=int(item.get("voice", 1)),
                    confidence=float(item.get("confidence", 0.5)),
                    part_id=part_id,
                    source_system=system.system_index,
                ))
            except (KeyError, ValueError, TypeError) as e:
                log.warning(f"Pass 2b [{part_name}]: 항목 오류 {item}: {e}")
    return notes


# ── 메인 진입점 ───────────────────────────────────────────────────────────────

def run_pass2b(
    page_images: list[Image.Image],
    layout: ScoreLayout,
    tiers: list[int] | None = None,
) -> list[RawNote]:
    """Pass 2b 실행. tiers=None이면 Tier 1만, [1,2]이면 Tier 1+2."""
    target_tiers = tiers if tiers is not None else [1]
    all_notes: list[RawNote] = []

    for system in layout.systems:
        page_img = page_images[system.page - 1]
        log.info(
            f"Pass 2b: p{system.page} s{system.system_index} "
            f"(m{system.start_measure}~{system.end_measure})"
        )

        # Tier 1: Piano
        if 1 in target_tiers:
            notes = extract_notes_for_system(page_img, system, layout)
            all_notes.extend(notes)
            log.info(f"  Tier1 → {len(notes)}개")

        # Tier 2~4: 개별 파트
        for tier in target_tiers:
            if tier == 1:
                continue
            for part_name in TIERS.get(tier, []):
                notes = extract_notes_single_part(page_img, system, layout, part_name)
                if notes:
                    all_notes.extend(notes)
                    log.info(f"  Tier{tier} [{part_name}] → {len(notes)}개")

        # Extra (Electric Bass 등)
        if max(target_tiers) >= 2:
            for part_name in TIER_EXTRA:
                notes = extract_notes_single_part(page_img, system, layout, part_name)
                if notes:
                    all_notes.extend(notes)

    log.info(f"Pass 2b 완료: 총 {len(all_notes)}개 음표")
    return all_notes
