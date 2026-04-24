"""omr.py — oemer 기반 OMR 래퍼

oemer는 단순 피아노 악보에 최적화된 신경망 OMR 도구입니다.
오케스트라 악보나 복잡한 조표에서는 정확도가 낮을 수 있습니다.
"""
from __future__ import annotations

import logging
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

# 음표 타입 문자열 정규화 (oemer → pipeline 포맷)
_TYPE_MAP = {
    "whole": "whole", "half": "half", "quarter": "quarter",
    "eighth": "eighth", "16th": "16th", "32nd": "32nd",
    "64th": "32nd",  # 최소 단위로 클리핑
}


def _parse_mxl(mxl_bytes: bytes, start_measure: int, end_measure: int) -> dict:
    """
    oemer MusicXML 바이트 → pipeline 포맷 dict 변환.

    Returns:
        {
            "Piano treble": {str(measure): [note_dict, ...]},
            "Piano bass":   {str(measure): [note_dict, ...]},
        }
    """
    root = ET.fromstring(mxl_bytes)
    result: dict[str, dict[str, list]] = {
        "Piano treble": {},
        "Piano bass": {},
    }

    part = root.find("part")
    if part is None:
        return result

    # oemer 마디 번호(1-based) → 실제 마디 번호 매핑
    oemer_measures = part.findall("measure")
    n_total = len(oemer_measures)
    n_span = end_measure - start_measure + 1

    for oemer_idx, measure in enumerate(oemer_measures):
        # 실제 악보 마디 번호
        real_measure = start_measure + oemer_idx
        if real_measure > end_measure:
            break

        m_str = str(real_measure)
        result["Piano treble"][m_str] = []
        result["Piano bass"][m_str] = []

        beat_counter = {1: 1.0, 2: 1.0}

        for note in measure.findall("note"):
            pitch_el = note.find("pitch")
            type_el = note.find("type")
            dots = len(note.findall("dot"))
            staff_el = note.find("staff")
            voice_el = note.find("voice")
            chord_el = note.find("chord")
            tie_els = note.findall("tie")
            dur_el = note.find("duration")
            div_el = measure.find(".//divisions")

            staff_num = int(staff_el.text) if staff_el is not None else 1
            voice = int(voice_el.text) if voice_el is not None else 1

            # 박 위치 계산
            div = int(div_el.text) if div_el is not None else 1
            dur_ticks = int(dur_el.text) if dur_el is not None else div

            if chord_el is None:
                beat_val = beat_counter[staff_num]
                beat_counter[staff_num] += dur_ticks / div
            else:
                beat_val = beat_counter[staff_num] - dur_ticks / div

            # 피치
            if pitch_el is not None:
                step = pitch_el.find("step").text
                octave = pitch_el.find("octave").text
                alter_el = pitch_el.find("alter")
                if alter_el is not None:
                    alter_val = float(alter_el.text)
                    acc = "#" if alter_val > 0 else ("b" if alter_val < 0 else "")
                else:
                    acc = ""
                pitch_str = f"{step}{acc}{octave}"
            else:
                pitch_str = "rest"

            note_dict = {
                "beat": round(beat_val, 2),
                "pitch": pitch_str,
                "duration": _TYPE_MAP.get(type_el.text if type_el is not None else "quarter", "quarter"),
                "dots": dots,
                "voice": voice,
                "tie_start": any(t.get("type") == "start" for t in tie_els),
                "tie_end": any(t.get("type") == "stop" for t in tie_els),
                "confidence": 0.5,  # oemer 출력은 항상 중간 신뢰도
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

    Args:
        cropped: crop_part_range()로 얻은 Piano 보표 이미지
        start_measure: 이 이미지의 첫 마디 번호
        end_measure: 마지막 마디 번호

    Returns:
        {"Piano treble": {m: [notes]}, "Piano bass": {m: [notes]}}
        실패 시 None.
    """
    try:
        import oemer.ete as ete
    except ImportError:
        log.error("oemer 미설치. `pip install oemer` 실행 후 재시도.")
        return None

    import argparse

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. 이미지 저장
        img_path = str(Path(tmpdir) / "piano_crop.png")
        cropped.save(img_path)

        # 2. oemer 실행
        class _Args:
            def __init__(self):
                self.img_path = img_path
                self.output_path = tmpdir
                self.use_tf = False
                self.save_cache = False
                self.without_deskew = True

        try:
            mxl_path = ete.extract(_Args())
        except Exception as e:
            log.warning(f"oemer 추출 실패: {e}")
            return None

        # 3. MusicXML 파싱
        if mxl_path is None or not Path(mxl_path).exists():
            log.warning("oemer MusicXML 출력 없음")
            return None

        mxl_bytes = Path(mxl_path).read_bytes()
        result = _parse_mxl(mxl_bytes, start_measure, end_measure)

    n_treble = sum(len(v) for v in result["Piano treble"].values())
    n_bass = sum(len(v) for v in result["Piano bass"].values())
    log.debug(f"oemer: treble={n_treble}개, bass={n_bass}개 음표")

    return result
