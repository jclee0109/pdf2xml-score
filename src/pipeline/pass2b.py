"""Pass 2b: 음표 추출 — Tier 1 oemer OMR (병렬), Tier 2~4 미구현"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from ..models.score import RawNote, ScoreLayout, SystemInfo
from ..utils.render import crop_part_range
from ..utils.omr import extract_notes_oemer, set_cache_dir

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

    data = extract_notes_oemer(cropped, system.start_measure, system.end_measure)

    if data is None:
        log.warning(f"Pass 2b (oemer): p{system.page}/s{system.system_index} 추출 실패")
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

def extract_notes_single_part(
    page_img: Image.Image,
    system: SystemInfo,
    layout: ScoreLayout,
    part_name: str,
) -> list[RawNote]:
    """단일 파트 음표 추출 — Tier 2~4 미구현 (빈 리스트 반환)."""
    log.debug(f"Pass 2b [{part_name}]: Tier 2~4 LLM-free 미구현, 스킵")
    return []


# ── 병렬 처리용 최상위 함수 (subprocess-safe) ─────────────────────────────────

def _extract_system_worker(
    img_path: str,
    system_dict: dict,
    parts_list: list[dict],
    cache_dir: str,
) -> list[dict]:
    """ProcessPoolExecutor worker: PIL 이미지 대신 파일 경로로 통신."""
    from ..utils.omr import extract_notes_oemer, set_cache_dir
    from ..utils.render import crop_part_range

    set_cache_dir(cache_dir)

    img = Image.open(img_path)

    # system_dict → 최소 필드 복원
    class _Sys:
        pass
    sys = _Sys()
    sys.page             = system_dict["page"]
    sys.system_index     = system_dict["system_index"]
    sys.start_measure    = system_dict["start_measure"]
    sys.end_measure      = system_dict["end_measure"]
    sys.y_top_px         = system_dict["y_top_px"]
    sys.y_bottom_px      = system_dict["y_bottom_px"]
    sys.active_parts     = system_dict["active_parts"]

    # piano index 탐색
    treble_id = next(
        (pid for pid in sys.active_parts
         if parts_list[int(pid[1:])]["name"] in ("Piano treble", "Piano")), None
    )
    bass_id = next(
        (pid for pid in sys.active_parts
         if parts_list[int(pid[1:])]["name"] == "Piano bass"), None
    )
    if treble_id is None:
        return []

    treble_idx = sys.active_parts.index(treble_id)
    bass_idx   = sys.active_parts.index(bass_id) if bass_id else treble_idx
    bass_pid   = bass_id

    cropped = crop_part_range(
        img, sys.y_top_px, sys.y_bottom_px,
        treble_idx, bass_idx, len(sys.active_parts),
        extra_top=25, extra_bottom=10,
    )

    data = extract_notes_oemer(cropped, sys.start_measure, sys.end_measure)
    if data is None:
        return []

    notes_out = []
    staff_map = {
        "Piano treble": treble_id,
        "Piano bass":   bass_pid or treble_id,
    }
    for staff_label, part_id in staff_map.items():
        for m_str, note_list in data.get(staff_label, {}).items():
            try:
                m_num = int(m_str)
            except ValueError:
                continue
            for item in note_list:
                notes_out.append({
                    "measure":       m_num,
                    "beat":          float(item.get("beat", 1.0)),
                    "pitch":         str(item["pitch"]),
                    "duration":      str(item.get("duration", "quarter")),
                    "dots":          int(item.get("dots", 0)),
                    "tie_start":     bool(item.get("tie_start", False)),
                    "tie_end":       bool(item.get("tie_end", False)),
                    "voice":         int(item.get("voice", 1)),
                    "confidence":    float(item.get("confidence", 0.5)),
                    "part_id":       part_id,
                    "source_system": sys.system_index,
                })
    return notes_out


# ── 메인 진입점 ───────────────────────────────────────────────────────────────

def run_pass2b(
    page_images: list[Image.Image],
    layout: ScoreLayout,
    tiers: list[int] | None = None,
    cache_dir: str | Path = "output/.oemer_cache",
    parallel: bool = True,
) -> list[RawNote]:
    """Pass 2b 실행.

    parallel=True (기본): 모든 시스템을 ProcessPoolExecutor로 동시 실행.
    tiers=None이면 Tier 1(Piano)만 처리.
    """
    target_tiers = tiers if tiers is not None else [1]
    set_cache_dir(cache_dir)
    cache_dir_str = str(Path(cache_dir).resolve())

    if 1 not in target_tiers or not parallel:
        # 순차 처리 폴백
        all_notes: list[RawNote] = []
        for system in layout.systems:
            page_img = page_images[system.page - 1]
            log.info(f"Pass 2b: p{system.page} s{system.system_index} (m{system.start_measure}~{system.end_measure})")
            notes = extract_notes_for_system(page_img, system, layout)
            all_notes.extend(notes)
            log.info(f"  Tier1 → {len(notes)}개")
        log.info(f"Pass 2b 완료: 총 {len(all_notes)}개 음표")
        return all_notes

    # ── 병렬 처리: 이미지를 임시 파일로 저장 후 worker에 경로 전달 ─────────────
    import tempfile

    parts_list = [
        {"name": p.name, "clef": p.clef}
        for p in layout.parts
    ]

    # 페이지 이미지 → 임시 PNG 경로
    tmp_dir = Path(cache_dir_str) / "_pages_tmp"
    tmp_dir.mkdir(exist_ok=True)
    img_paths: list[str] = []
    for i, img in enumerate(page_images):
        p = str(tmp_dir / f"page-{i+1}.png")
        if not Path(p).exists():
            img.save(p)
        img_paths.append(p)

    # 시스템별 작업 제출
    system_dicts = [
        {
            "page": s.page, "system_index": s.system_index,
            "start_measure": s.start_measure, "end_measure": s.end_measure,
            "y_top_px": s.y_top_px, "y_bottom_px": s.y_bottom_px,
            "active_parts": s.active_parts,
        }
        for s in layout.systems
    ]

    max_workers = min(len(layout.systems), os.cpu_count() or 4)
    log.info(f"Pass 2b: {len(layout.systems)}시스템 병렬 처리 (workers={max_workers})")

    results: dict[int, list[dict]] = {}
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        future_to_idx = {
            ex.submit(
                _extract_system_worker,
                img_paths[sd["page"] - 1],
                sd,
                parts_list,
                cache_dir_str,
            ): i
            for i, sd in enumerate(system_dicts)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            sd = system_dicts[idx]
            try:
                note_dicts = future.result()
                results[idx] = note_dicts
                log.info(
                    f"  p{sd['page']} s{sd['system_index']} "
                    f"m{sd['start_measure']}~{sd['end_measure']} → {len(note_dicts)}개"
                )
            except Exception as e:
                log.warning(f"  p{sd['page']} s{sd['system_index']} 실패: {e}")
                results[idx] = []

    # 순서대로 합치기
    all_raw: list[RawNote] = []
    for idx in sorted(results):
        for nd in results[idx]:
            all_raw.append(RawNote(**nd))

    log.info(f"Pass 2b 완료: 총 {len(all_raw)}개 음표")
    return all_raw
