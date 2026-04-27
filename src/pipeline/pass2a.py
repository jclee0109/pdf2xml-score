"""Pass 2a: 코드 심볼 추출 — Piano 영역 crop + pytesseract OCR"""
import json
import logging
from pathlib import Path

from PIL import Image

from ..models.score import RawChord, ScoreLayout, SystemInfo
from ..utils.render import crop_part_range
from ..utils.ocr import extract_chord_symbols

log = logging.getLogger(__name__)

# 코드 심볼이 보표 위에 표기되는 주요 파트 이름 (treble 기준)
CHORD_SYMBOL_TREBLE_NAMES = {"Piano treble", "Piano", "Guitar", "Keyboard", "Harp"}
CHORD_SYMBOL_BASS_NAMES   = {"Piano bass"}

# 하위 호환 별칭
PIANO_TREBLE_NAMES = CHORD_SYMBOL_TREBLE_NAMES
PIANO_BASS_NAMES   = CHORD_SYMBOL_BASS_NAMES


def _assign_measures(
    x_centers: list[int],
    img_width: int,
    start_measure: int,
    end_measure: int,
) -> list[int]:
    """x 위치 기반으로 코드를 마디에 균등 배분."""
    n_measures = max(end_measure - start_measure + 1, 1)
    measure_width = img_width / n_measures
    measures = []
    for x in x_centers:
        idx = int(x // measure_width)
        idx = min(idx, n_measures - 1)
        measures.append(start_measure + idx)
    return measures


def _find_chord_part_indices(
    system: SystemInfo, layout: ScoreLayout
) -> tuple[int, int, str] | None:
    """코드 심볼이 있는 파트의 (treble_idx, bass_idx, treble_part_id) 반환.

    우선순위:
    1. CHORD_SYMBOL_TREBLE_NAMES에 해당하는 파트 (Piano, Guitar 등)
    2. 없으면 treble clef를 가진 첫 번째 파트로 fallback
    """
    def part_name(pid: str) -> str:
        return layout.parts[int(pid[1:])].name

    def part_clef(pid: str) -> str:
        return layout.parts[int(pid[1:])].clef

    # 1순위: 알려진 코드 심볼 파트
    treble_id = next(
        (pid for pid in system.active_parts if part_name(pid) in CHORD_SYMBOL_TREBLE_NAMES),
        None,
    )
    bass_id = next(
        (pid for pid in system.active_parts if part_name(pid) in CHORD_SYMBOL_BASS_NAMES),
        None,
    )

    # 2순위: treble clef 첫 파트 (fallback — 클래식 악보 등)
    if treble_id is None:
        treble_id = next(
            (pid for pid in system.active_parts if part_clef(pid) == "treble"),
            None,
        )
        if treble_id:
            log.debug(
                f"System {system.system_index}: 코드 심볼 파트 없음 → "
                f"'{part_name(treble_id)}' fallback"
            )

    if treble_id is None:
        return None

    treble_idx = system.active_parts.index(treble_id)
    bass_idx   = system.active_parts.index(bass_id) if bass_id else treble_idx
    return treble_idx, bass_idx, treble_id


def extract_chords_for_system(
    page_img: Image.Image,
    system: SystemInfo,
    layout: ScoreLayout,
) -> list[RawChord]:
    result = _find_chord_part_indices(system, layout)
    if result is None:
        log.debug(f"System {system.system_index} (p{system.page}): treble 파트 없음, 스킵")
        return []
    treble_idx, bass_idx, treble_id = result
    n_parts = len(system.active_parts)

    cropped = crop_part_range(
        page_img,
        system.y_top_px, system.y_bottom_px,
        treble_idx, bass_idx, n_parts,
    )

    hits = extract_chord_symbols(cropped)
    if not hits:
        log.debug(f"Pass 2a: p{system.page}/s{system.system_index} 코드 없음")
        return []

    x_centers = [x for x, _ in hits]
    measures = _assign_measures(
        x_centers, cropped.width,
        system.start_measure, system.end_measure,
    )

    chords = []
    for (x, chord_text), measure in zip(hits, measures):
        chords.append(RawChord(
            measure=measure,
            beat=1.0,
            chord_text=chord_text,
            confidence=0.6,
            source_page=system.page,
            source_system=system.system_index,
        ))

    return chords


def chords_to_json(chords: list[RawChord], path: str | Path) -> None:
    """RawChord 목록을 JSON으로 저장."""
    import json as _json
    data = [
        {
            "measure": c.measure, "beat": c.beat,
            "chord_text": c.chord_text, "confidence": c.confidence,
            "source_page": c.source_page, "source_system": c.source_system,
        }
        for c in chords
    ]
    Path(path).write_text(_json.dumps(data, ensure_ascii=False, indent=2))


def chords_from_json(path: str | Path) -> list[RawChord]:
    """사전 추출된 JSON 파일에서 RawChord 로드."""
    data = json.loads(Path(path).read_text())
    return [
        RawChord(
            measure=c["measure"], beat=c["beat"],
            chord_text=c["chord_text"], confidence=c["confidence"],
            source_page=c["source_page"], source_system=c["source_system"],
        )
        for c in data
    ]


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
