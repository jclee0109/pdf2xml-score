"""Pass 2c: 가사 추출 — 성악 보표에서 pytesseract OCR"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from PIL import Image

from ..models.score import RawLyric, ScoreLayout, SystemInfo
from ..utils.render import crop_part_range
from ..utils.ocr import extract_lyrics_from_stave

log = logging.getLogger(__name__)

# 성악 파트 이름 매칭 패턴 (대소문자 무관)
_VOCAL_RE = re.compile(
    r"(?i)(vocal|voice|soprano|mezzo|alto|tenor|baritone|"
    r"bass\s*voice|lead|melody|singer|vox|"
    r"보컬|멜로디|노래|소프라노|알토|테너|바리톤)"
)


def find_vocal_part_ids(layout: ScoreLayout) -> list[str]:
    """레이아웃에서 성악 파트 ID 목록 반환."""
    ids = [p.id for p in layout.parts if _VOCAL_RE.search(p.name)]
    if ids:
        log.debug(f"성악 파트 감지: {[layout.parts[int(pid[1:])].name for pid in ids]}")
    return ids


def _x_to_beat(x: int, img_width: int, start_m: int, end_m: int, time_sig: str) -> tuple[int, float]:
    """x 픽셀 위치 → (마디번호, 박자).
    마디가 균등 분할된다고 가정한다."""
    n_measures = max(end_m - start_m + 1, 1)
    beats_str, beat_type_str = time_sig.split("/")
    beats_per_m = int(beats_str) * 4.0 / int(beat_type_str)

    measure_w = img_width / n_measures
    m_idx = int(x // measure_w)
    m_idx = min(m_idx, n_measures - 1)
    measure = start_m + m_idx

    # 마디 내 x 비율 → 박자 (1.0 ~ beats_per_m)
    x_in_m = x - m_idx * measure_w
    beat = 1.0 + (x_in_m / measure_w) * beats_per_m
    beat = round(min(beat, beats_per_m), 2)

    return measure, beat


def extract_lyrics_for_system(
    page_img: Image.Image,
    system: SystemInfo,
    layout: ScoreLayout,
    vocal_part_ids: list[str],
) -> list[RawLyric]:
    """시스템 내 성악 파트에서 가사 추출."""
    lyrics: list[RawLyric] = []

    active = system.active_parts
    n_parts = len(active)

    for pid in vocal_part_ids:
        if pid not in active:
            continue
        part_idx = active.index(pid)

        # 단일 파트 스트라이프 크롭 (extra_top 최소: 코드 심볼 제외)
        stave_crop = crop_part_range(
            page_img,
            system.y_top_px, system.y_bottom_px,
            part_idx, part_idx, n_parts,
            extra_top=5, extra_bottom=30,   # 하단 여백: 가사가 보표 아래에 있음
        )

        hits = extract_lyrics_from_stave(stave_crop)
        if not hits:
            log.debug(f"Pass 2c: p{system.page}/s{system.system_index} [{pid}] 가사 없음")
            continue

        for x, text in hits:
            measure, beat = _x_to_beat(
                x, stave_crop.width,
                system.start_measure, system.end_measure,
                system.time_signature,
            )
            lyrics.append(RawLyric(
                measure=measure,
                beat=beat,
                text=text,
                part_id=pid,
                source_system=system.system_index,
            ))

        log.debug(
            f"Pass 2c: p{system.page}/s{system.system_index} [{pid}] "
            f"{len(hits)}개 음절"
        )

    return lyrics


def run_pass2c(
    page_images: list[Image.Image],
    layout: ScoreLayout,
) -> list[RawLyric]:
    """Pass 2c 전체 실행. RawLyric 목록 반환."""
    vocal_ids = find_vocal_part_ids(layout)
    if not vocal_ids:
        log.info("Pass 2c: 성악 파트 없음, 스킵")
        return []

    all_lyrics: list[RawLyric] = []
    for system in layout.systems:
        page_img = page_images[system.page - 1]
        lyr = extract_lyrics_for_system(page_img, system, layout, vocal_ids)
        all_lyrics.extend(lyr)

    log.info(f"Pass 2c 완료: 총 {len(all_lyrics)}개 음절")
    return all_lyrics


# ── 파일 기반 직렬화 ──────────────────────────────────────────────────────────

def lyrics_to_json(lyrics: list[RawLyric], path: str | Path) -> None:
    data = [
        {
            "measure": l.measure, "beat": l.beat,
            "text": l.text, "part_id": l.part_id,
            "source_system": l.source_system,
        }
        for l in lyrics
    ]
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


def lyrics_from_json(path: str | Path) -> list[RawLyric]:
    data = json.loads(Path(path).read_text())
    return [
        RawLyric(
            measure=d["measure"], beat=d["beat"],
            text=d["text"], part_id=d["part_id"],
            source_system=d["source_system"],
        )
        for d in data
    ]
