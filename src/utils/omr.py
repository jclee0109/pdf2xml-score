"""omr.py — oemer 기반 OMR 래퍼

oemer는 단순 피아노 악보에 최적화된 신경망 OMR 도구입니다.
오케스트라 악보나 복잡한 조표에서는 정확도가 낮을 수 있습니다.

속도 최적화:
- save_cache=True: .pkl 캐시 저장 → 재실행 시 ONNX 추론 스킵 (<1초)
- 고정 캐시 디렉토리: tmpdir 대신 output_dir/.oemer_cache/ 사용
"""
from __future__ import annotations

import hashlib
import logging
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

_TYPE_MAP = {
    "whole": "whole", "half": "half", "quarter": "quarter",
    "eighth": "eighth", "16th": "16th", "32nd": "32nd",
    "64th": "32nd",
}

# 전역 캐시 디렉토리 (첫 호출 시 설정)
_CACHE_DIR: Path | None = None


def set_cache_dir(path: str | Path) -> None:
    """캐시 디렉토리 설정. run_pass2b() 전에 호출."""
    global _CACHE_DIR
    _CACHE_DIR = Path(path)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _get_cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        _CACHE_DIR = Path("output/.oemer_cache")
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


def _img_hash(img: Image.Image) -> str:
    """이미지 내용 기반 짧은 해시 (캐시 키)."""
    import hashlib
    data = img.tobytes()
    return hashlib.md5(data).hexdigest()[:12]


def _parse_mxl(mxl_bytes: bytes, start_measure: int, end_measure: int) -> dict:
    """oemer MusicXML 바이트 → pipeline 포맷 dict 변환."""
    root = ET.fromstring(mxl_bytes)
    result: dict[str, dict[str, list]] = {
        "Piano treble": {},
        "Piano bass": {},
    }

    part = root.find("part")
    if part is None:
        return result

    for oemer_idx, measure in enumerate(part.findall("measure")):
        real_measure = start_measure + oemer_idx
        if real_measure > end_measure:
            break

        m_str = str(real_measure)
        result["Piano treble"][m_str] = []
        result["Piano bass"][m_str] = []

        beat_counter = {1: 1.0, 2: 1.0}

        for note in measure.findall("note"):
            pitch_el = note.find("pitch")
            type_el  = note.find("type")
            dots     = len(note.findall("dot"))
            staff_el = note.find("staff")
            voice_el = note.find("voice")
            chord_el = note.find("chord")
            tie_els  = note.findall("tie")
            dur_el   = note.find("duration")
            div_el   = measure.find(".//divisions")

            staff_num = int(staff_el.text) if staff_el is not None else 1
            voice     = int(voice_el.text) if voice_el is not None else 1
            div       = int(div_el.text)   if div_el   is not None else 1
            dur_ticks = int(dur_el.text)   if dur_el   is not None else div

            if chord_el is None:
                beat_val = beat_counter[staff_num]
                beat_counter[staff_num] += dur_ticks / div
            else:
                beat_val = beat_counter[staff_num] - dur_ticks / div

            if pitch_el is not None:
                step   = pitch_el.find("step").text
                octave = pitch_el.find("octave").text
                alter_el = pitch_el.find("alter")
                acc = ""
                if alter_el is not None:
                    v = float(alter_el.text)
                    acc = "#" if v > 0 else ("b" if v < 0 else "")
                pitch_str = f"{step}{acc}{octave}"
            else:
                pitch_str = "rest"

            note_dict = {
                "beat":      round(beat_val, 2),
                "pitch":     pitch_str,
                "duration":  _TYPE_MAP.get(
                    type_el.text if type_el is not None else "quarter", "quarter"
                ),
                "dots":      dots,
                "voice":     voice,
                "tie_start": any(t.get("type") == "start" for t in tie_els),
                "tie_end":   any(t.get("type") == "stop"  for t in tie_els),
                "confidence": 0.5,
            }

            staff_key = "Piano treble" if staff_num == 1 else "Piano bass"
            result[staff_key][m_str].append(note_dict)

    return result


def extract_notes_oemer(
    cropped: Image.Image,
    start_measure: int,
    end_measure: int,
) -> dict | None:
    """
    Piano treble+bass 영역 이미지에서 oemer로 음표 추출.

    캐시 히트 시 ONNX 추론 없이 기존 .pkl 재사용 → 거의 즉시 완료.

    Returns:
        {"Piano treble": {m: [notes]}, "Piano bass": {m: [notes]}}
        실패 시 None.
    """
    try:
        import oemer.ete as ete
    except ImportError:
        log.error("oemer 미설치. `pip install oemer` 실행 후 재시도.")
        return None

    cache_dir = _get_cache_dir()
    img_key   = _img_hash(cropped)
    img_path  = str(cache_dir / f"crop_{img_key}.png")
    mxl_path  = str(cache_dir / f"crop_{img_key}.musicxml")

    # 이미지 저장 (캐시 키 파일명으로 고정)
    if not Path(img_path).exists():
        cropped.save(img_path)

    class _Args:
        def __init__(self):
            self.img_path      = img_path
            self.output_path   = str(cache_dir)
            self.use_tf        = False
            self.save_cache    = True   # ← pkl 캐시 저장
            self.without_deskew = True

    # MusicXML이 이미 캐시에 있으면 oemer 전체 스킵
    if Path(mxl_path).exists():
        log.debug(f"oemer cache hit: {img_key}")
        result = _parse_mxl(Path(mxl_path).read_bytes(), start_measure, end_measure)
    else:
        try:
            out = ete.extract(_Args())
        except Exception as e:
            log.warning(f"oemer 추출 실패: {e}")
            return None

        if out is None or not Path(out).exists():
            log.warning("oemer MusicXML 출력 없음")
            return None

        result = _parse_mxl(Path(out).read_bytes(), start_measure, end_measure)

    n_treble = sum(len(v) for v in result["Piano treble"].values())
    n_bass   = sum(len(v) for v in result["Piano bass"].values())
    log.debug(f"oemer: treble={n_treble}, bass={n_bass} notes")

    return result
