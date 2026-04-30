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
from ..utils.ocr import extract_instrument_names, is_plausible_instrument
from ..utils.staff_detect import analyze_page

log = logging.getLogger(__name__)


# ── Step 1-A: 악기 목록 (pytesseract OCR) ────────────────────────────────────

def _infer_parts_from_staves(img: Image.Image) -> list[dict]:
    """악기명 OCR 실패 시 보표 수로 파트 추론. 피아노/리드시트 등 단순 악보용."""
    from ..utils.staff_detect import _to_gray, count_staves_per_system, detect_staff_systems
    systems = detect_staff_systems(img)
    if not systems:
        log.info("시스템 감지 실패 → 피아노 2단보 기본값")
        return [{"name": "Piano treble", "clef": "treble"},
                {"name": "Piano bass",   "clef": "bass"}]
    gray = _to_gray(img)
    sys0 = systems[0]
    n = count_staves_per_system(gray, sys0["y_top"], sys0["y_bottom"])
    log.info(f"보표 수 감지: {n}개")
    if n <= 1:
        return [{"name": "Piano", "clef": "treble"}]
    elif n == 2:
        return [{"name": "Piano treble", "clef": "treble"},
                {"name": "Piano bass",   "clef": "bass"}]
    else:
        parts = []
        for i in range(n):
            clef = "bass" if i == n - 1 else "treble"
            parts.append({"name": f"Staff {i+1}", "clef": clef})
        return parts


def extract_parts(page_images: list[Image.Image]) -> list[PartInfo]:
    """악기 목록 추출. OCR 실패 또는 비악기명이면 보표 수 기반 fallback."""
    raw_parts: list[dict] = []
    for img in page_images[:3]:
        raw_parts = extract_instrument_names(img)
        if len(raw_parts) >= 2:
            break
        log.debug(f"악기명 OCR 결과 부족({len(raw_parts)}개), 다음 페이지 시도")

    if raw_parts:
        plausible = [p for p in raw_parts if is_plausible_instrument(p["name"])]
        if plausible:
            raw_parts = plausible
        else:
            log.info(f"OCR 결과 {[p['name'] for p in raw_parts]} → 악기명 아님, 보표 수 fallback")
            raw_parts = []

    if not raw_parts:
        raw_parts = _infer_parts_from_staves(page_images[0])
        log.info(f"보표 수 기반 파트: {[p['name'] for p in raw_parts]}")

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


def _repair_measure_sequence(all_systems: list[SystemInfo]) -> None:
    """OCR 오인식/미감지 마디번호를 앵커 기반 선형 보간으로 보정.

    전략:
    1. 비율(measures/system)이 타당한 OCR 값만 앵커로 채택 (0.5 ~ 30 범위)
    2. 앵커 사이는 선형 보간, 앵커 바깥은 평균 비율로 외삽
    3. 최종 단조 증가 보장

    타당한 비율 범위는 가장 빠른 팝 곡(~8 measures/page) 부터
    느린 교향곡(~2 measures/page)까지 커버할 수 있게 여유 있게 설정.
    """
    if not all_systems:
        return

    MAX_RATE = 30.0   # measures per system (보수적 상한)
    MIN_RATE = 0.5    # measures per system (하한)
    n = len(all_systems)

    # Step 1: 앵커 후보 — 인접한 유효쌍에서 비율 검사
    non_zero = [(i, s.start_measure) for i, s in enumerate(all_systems)
                if s.start_measure > 0]

    anchors: list[tuple[int, int]] = []
    if non_zero:
        anchors.append(non_zero[0])
        for j in range(1, len(non_zero)):
            i2, m2 = non_zero[j]
            i1, m1 = anchors[-1]
            dist = i2 - i1
            rate = (m2 - m1) / dist if dist > 0 else -1
            if MIN_RATE <= rate <= MAX_RATE:
                anchors.append((i2, m2))
            else:
                log.debug(
                    f"앵커 제외: p{all_systems[i2].page} m={m2} "
                    f"(직전 앵커 m={m1}, rate={rate:.1f})"
                )

    if not anchors:
        # 앵커가 없으면 순번 사용
        for i, s in enumerate(all_systems):
            s.start_measure = i + 1
        return

    # 평균 비율 (외삽용)
    if len(anchors) >= 2:
        total_m = anchors[-1][1] - anchors[0][1]
        total_i = anchors[-1][0] - anchors[0][0]
        avg_rate = total_m / total_i if total_i > 0 else 4.0
    else:
        avg_rate = 4.0

    # Step 2: 전체 시스템에 보간/외삽 적용
    for i, sys in enumerate(all_systems):
        # 앞·뒤 앵커 탐색
        lo = next(((ai, am) for ai, am in reversed(anchors) if ai <= i), None)
        hi = next(((ai, am) for ai, am in anchors if ai >= i), None)

        if lo is not None and hi is not None and lo[0] != hi[0]:
            frac = (i - lo[0]) / (hi[0] - lo[0])
            est = lo[1] + frac * (hi[1] - lo[1])
        elif lo is not None:
            est = lo[1] + (i - lo[0]) * avg_rate
        elif hi is not None:
            est = hi[1] - (hi[0] - i) * avg_rate
        else:
            est = i + 1

        new_m = max(1, round(est))
        if new_m != sys.start_measure and sys.start_measure != 0:
            log.warning(
                f"마디번호 보정: p{sys.page} s{sys.system_index} "
                f"{sys.start_measure} → {new_m}"
            )
        sys.start_measure = new_m

    # Step 3: 단조 증가 보장
    prev = 0
    for sys in all_systems:
        if sys.start_measure <= prev:
            sys.start_measure = prev + 1
        prev = sys.start_measure


def _fill_end_measures(all_systems: list[SystemInfo], total_measures: int) -> None:
    """end_measure를 다음 시스템 start - 1로 채운다."""
    for i, sys in enumerate(all_systems):
        if i + 1 < len(all_systems):
            sys.end_measure = all_systems[i + 1].start_measure - 1
        else:
            sys.end_measure = total_measures


# ── 메인 진입점 ────────────────────────────────────────────────────────────────

def layout_to_json(layout: ScoreLayout, path: str | Path) -> None:
    """ScoreLayout을 JSON으로 저장."""
    data = {
        "parts": [
            {"id": p.id, "name": p.name, "order": p.order, "clef": p.clef}
            for p in layout.parts
        ],
        "systems": [
            {
                "page": s.page,
                "system_index": s.system_index,
                "start_measure": s.start_measure,
                "end_measure": s.end_measure,
                "key": s.key,
                "time_signature": s.time_signature,
                "y_top_px": s.y_top_px,
                "y_bottom_px": s.y_bottom_px,
                "active_parts": s.active_parts,
                "rehearsal_marks": [{"measure": m.measure, "label": m.label} for m in s.rehearsal_marks],
                "repeat_barlines": [{"measure": m.measure, "type": m.type} for m in s.repeat_barlines],
                "volta_brackets": [{"start_measure": m.start_measure, "end_measure": m.end_measure, "number": m.number} for m in s.volta_brackets],
                "key_changes": [{"measure": m.measure, "key": m.key} for m in s.key_changes],
            }
            for s in layout.systems
        ],
        "total_measures": layout.total_measures,
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2))


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
    parts = extract_parts(page_images)
    name_to_id = {p.name: p.id for p in parts}
    log.info(f"  → {len(parts)}개 파트 감지: {[p.name for p in parts]}")

    all_systems: list[SystemInfo] = []
    prev_key, prev_time = "C major", "4/4"
    first_music_page = True  # 악보가 실제로 있는 첫 페이지에만 m=1 앵커 적용
    for page_num, img in enumerate(page_images, start=1):
        log.info(f"Pass 1: 페이지 {page_num}/{len(page_images)} 시스템 구조 추출")
        systems = extract_systems(img, page_num, name_to_id,
                                  prev_key=prev_key, prev_time=prev_time,
                                  default_measure=1 if first_music_page else None)
        if systems:
            first_music_page = False  # 이후 페이지는 OCR로 마디번호 결정
            prev_key = systems[0].key
            prev_time = systems[0].time_signature
        all_systems.extend(systems)
        log.info(f"  → {len(systems)}개 시스템 감지")

    _repair_measure_sequence(all_systems)

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
