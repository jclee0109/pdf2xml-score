"""Pass 1: 구조 분석 — 악기 목록, 시스템 레이아웃, 마디번호, 조표, 반복기호"""
import json
import logging
from pathlib import Path

from PIL import Image

from ..models.score import (
    PartInfo, SystemInfo, ScoreLayout,
    RehearsalMark, RepeatBarline, VoltaBracket, KeyChange,
    TRANSPOSITION_TABLE, _transposition_semitones,
)
from ..utils.ocr import extract_instrument_names
from ..utils.staff_detect import analyze_page

log = logging.getLogger(__name__)


# ── Step 1-A: 악기 목록 (pytesseract OCR) ────────────────────────────────────

def extract_parts(first_page_img: Image.Image) -> list[PartInfo]:
    raw_parts = extract_instrument_names(first_page_img)
    if not raw_parts:
        raise RuntimeError("Step 1-A: 악기 목록 OCR 실패 — 왼쪽 여백에서 텍스트를 찾지 못했습니다")

    parts = []
    for i, p in enumerate(raw_parts):
        name = p["name"]
        parts.append(PartInfo(
            id=f"P{i}",
            name=name,
            order=i,
            clef=p["clef"],
            transposition_semitones=_transposition_semitones(name),
        ))
    return parts


# ── Step 1-B: 페이지별 시스템 구조 (OpenCV + pytesseract) ────────────────────

def extract_systems(
    page_img: Image.Image,
    page_num: int,
    name_to_id: dict[str, str],
    prev_key: str = "C major",
    prev_time: str = "4/4",
    default_measure: int | None = None,
) -> list[SystemInfo]:
    page_info = analyze_page(page_img, prev_key=prev_key, prev_time=prev_time,
                             default_measure=default_measure)

    # active_parts: 현재 이름 목록 전체를 ID 순서대로 할당
    # (부분 생략 감지는 미구현, 모든 파트를 활성으로 간주)
    all_ids = list(name_to_id.values())

    systems = []
    for idx, sys_bounds in enumerate(page_info["systems"]):
        system = SystemInfo(
            page=page_num,
            system_index=idx,
            start_measure=page_info["start_measure"] if idx == 0 else 0,
            end_measure=0,
            key=page_info["key"],
            time_signature=page_info["time"],
            y_top_px=sys_bounds["y_top"],
            y_bottom_px=sys_bounds["y_bottom"],
            active_parts=all_ids,
            rehearsal_marks=[],
            repeat_barlines=[],
            volta_brackets=[],
        )
        systems.append(system)

    log.debug(
        f"Page {page_num}: {len(systems)}시스템, "
        f"start_m={page_info['start_measure']}, "
        f"key={page_info['key']}, time={page_info['time']}"
    )
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

    id_to_name = {p.id: p.name for p in parts}

    def _normalize_active(raw: list[str]) -> list[str]:
        """active_parts 가 이름이면 ID로 변환, 이미 ID면 그대로."""
        result = []
        for item in raw:
            if item in name_to_id:          # item == name
                result.append(name_to_id[item])
            elif item in id_to_name:        # item == ID (e.g. "P0")
                result.append(item)
            else:
                # 부분 문자열 매칭 폴백
                pid = next((v for k, v in name_to_id.items() if item in k or k in item), None)
                if pid:
                    result.append(pid)
        return result

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
            active_parts=_normalize_active(s["active_parts"]),
            rehearsal_marks=[RehearsalMark(**m) for m in s.get("rehearsal_marks", [])],
            repeat_barlines=[RepeatBarline(**m) for m in s.get("repeat_barlines", [])],
            volta_brackets=[VoltaBracket(**m) for m in s.get("volta_brackets", [])],
            key_changes=[KeyChange(**m) for m in s.get("key_changes", [])],
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
    prev_key, prev_time = "C major", "4/4"
    for page_num, img in enumerate(page_images, start=1):
        log.info(f"Pass 1: 페이지 {page_num}/{len(page_images)} 시스템 구조 추출")
        systems = extract_systems(img, page_num, name_to_id,
                                  prev_key=prev_key, prev_time=prev_time,
                                  default_measure=1 if page_num == 1 else None)
        if systems:
            prev_key = systems[0].key
            prev_time = systems[0].time_signature
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
