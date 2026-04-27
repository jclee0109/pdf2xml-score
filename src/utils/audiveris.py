"""audiveris.py — Audiveris OMR 래퍼

Audiveris는 범용 OMR 엔진으로 오케스트라 악보를 포함한 모든 보표 유형을 지원.
(oemer는 피아노 전용, 현악/관악 보표에서 리듬 감지가 완전히 실패함)

실행 방식: 페이지 단위 PNG → Audiveris batch → .mxl → RawNote 파싱
캐시: output_dir/.audiveris_cache/{page_stem}.mxl (재실행 시 스킵)
"""
from __future__ import annotations

import logging
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

_APP = Path("/Applications/Audiveris.app")
_JAVA = _APP / "Contents/runtime/Contents/Home/bin/java"
_APPDIR = _APP / "Contents/app"

_TYPE_MAP = {
    "whole": "whole", "half": "half", "quarter": "quarter",
    "eighth": "eighth", "16th": "16th", "32nd": "32nd", "64th": "32nd",
}


def is_available() -> bool:
    return _JAVA.exists() and _APPDIR.exists()


def _classpath() -> str:
    return ":".join(str(p) for p in sorted(_APPDIR.glob("*.jar")))


def _run_batch(img_path: Path, out_dir: Path, timeout: int = 180) -> Path | None:
    """Audiveris -batch 실행. 결과 .mxl 경로 반환, 실패 시 None."""
    stem = img_path.stem
    mxl_dst = out_dir / f"{stem}.mxl"
    if mxl_dst.exists():
        log.debug(f"Audiveris cache hit: {mxl_dst.name}")
        return mxl_dst

    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(_JAVA), "-cp", _classpath(), "Audiveris",
        "-batch", "-export",
        "-output", str(out_dir),
        str(img_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning(f"Audiveris timeout ({timeout}s): {img_path.name}")
        return None
    except Exception as e:
        log.warning(f"Audiveris error: {e}")
        return None

    # 타임스탬프 suffix가 붙는 경우 처리 (page-06-20260427T1827.mxl → page-06.mxl)
    candidates = sorted(out_dir.glob(f"{stem}*.mxl"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        log.warning(f"Audiveris: .mxl 결과 없음 ({img_path.name})")
        return None

    latest = candidates[-1]
    if latest != mxl_dst:
        latest.rename(mxl_dst)
    return mxl_dst


def _parse_mxl(
    mxl_path: Path,
    start_measure: int,
    n_parts: int,
) -> dict[int, list[dict]]:
    """
    Audiveris .mxl → {part_0based_idx: [note_dict]}

    measure 번호는 Audiveris 내부(1-based, 페이지 단위)를 전역 번호로 변환:
        global_measure = start_measure + (audiveris_measure_num - 1)
    """
    result: dict[int, list[dict]] = {i: [] for i in range(n_parts)}

    with zipfile.ZipFile(mxl_path) as z:
        xml_name = next(
            (n for n in z.namelist() if n.endswith(".xml") and "META" not in n),
            None,
        )
        if not xml_name:
            return result
        root = ET.fromstring(z.read(xml_name))

    parts = root.findall(".//part")
    for p_idx, part in enumerate(parts):
        if p_idx >= n_parts:
            break

        divisions = 1
        for measure in part.findall("measure"):
            m_num = int(measure.get("number", "1"))
            global_m = start_measure + (m_num - 1)
            beat_counter = 1.0

            for child in measure:
                if child.tag == "attributes":
                    div_el = child.find("divisions")
                    if div_el is not None:
                        try:
                            divisions = int(div_el.text)
                        except (ValueError, TypeError):
                            pass

                elif child.tag == "note":
                    note = child
                    dur_el = note.find("duration")
                    type_el = note.find("type")
                    chord_el = note.find("chord")
                    rest_el = note.find("rest")
                    pitch_el = note.find("pitch")
                    dots = len(note.findall("dot"))
                    tie_els = note.findall("tie")
                    voice_el = note.find("voice")

                    dur_ticks = int(dur_el.text) if dur_el is not None else divisions
                    voice = int(voice_el.text) if voice_el is not None else 1

                    if chord_el is None:
                        beat_val = beat_counter
                        beat_counter += dur_ticks / divisions
                    else:
                        beat_val = beat_counter - dur_ticks / divisions

                    if rest_el is not None:
                        pitch_str = "rest"
                    elif pitch_el is not None:
                        step = pitch_el.find("step").text
                        octave = pitch_el.find("octave").text
                        alter_el = pitch_el.find("alter")
                        acc = ""
                        if alter_el is not None:
                            try:
                                v = float(alter_el.text)
                                acc = "#" if v > 0 else ("b" if v < 0 else "")
                            except (ValueError, TypeError):
                                pass
                        pitch_str = f"{step}{acc}{octave}"
                    else:
                        continue

                    result[p_idx].append({
                        "measure": global_m,
                        "beat": round(beat_val, 3),
                        "pitch": pitch_str,
                        "duration": _TYPE_MAP.get(
                            type_el.text if type_el is not None else "quarter",
                            "quarter",
                        ),
                        "dots": dots,
                        "voice": voice,
                        "tie_start": any(t.get("type") == "start" for t in tie_els),
                        "tie_end": any(t.get("type") == "stop" for t in tie_els),
                        "confidence": 0.7,
                    })

    return result


def extract_notes_page(
    img_path: str | Path,
    start_measure: int,
    active_part_ids: list[str],
    cache_dir: str | Path,
) -> dict[str, list[dict]] | None:
    """
    페이지 PNG에서 Audiveris로 음표 추출.

    Returns:
        {part_id: [note_dict, ...]}  — active_part_ids 순서로 Audiveris 파트를 매핑
        None — Audiveris 실행 실패
    """
    if not is_available():
        log.error("Audiveris 미설치: /Applications/Audiveris.app 없음")
        return None

    img_path = Path(img_path)
    cache_dir = Path(cache_dir)

    mxl = _run_batch(img_path, cache_dir)
    if mxl is None:
        return None

    by_idx = _parse_mxl(mxl, start_measure, len(active_part_ids))
    n_notes = sum(len(v) for v in by_idx.values())
    log.debug(f"Audiveris {img_path.name}: {len(by_idx)}파트, {n_notes}음표")

    return {
        active_part_ids[idx]: notes
        for idx, notes in by_idx.items()
        if idx < len(active_part_ids)
    }
