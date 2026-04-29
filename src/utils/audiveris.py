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
    """Audiveris -batch 실행. 결과 .mxl 경로 반환, 실패 시 None.

    성공: {stem}.mxl 캐시
    실패: {stem}.failed 마커 파일 → 다음 실행에서 즉시 skip
    """
    stem = img_path.stem
    mxl_dst = out_dir / f"{stem}.mxl"
    failed_marker = out_dir / f"{stem}.failed"

    if mxl_dst.exists():
        log.debug(f"Audiveris cache hit: {mxl_dst.name}")
        return mxl_dst
    if failed_marker.exists():
        log.debug(f"Audiveris 이전 실패 skip: {img_path.name}")
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(_JAVA),
        "-Djava.awt.headless=true",   # AWT headless — Dock 아이콘 억제
        "-Dapple.awt.UIElement=true", # macOS 전용 — Dock 및 앱 전환기에서 숨김
        "-cp", _classpath(), "Audiveris",
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
        # 로그에서 실패 이유 감지
        log_candidates = sorted(out_dir.glob(f"{stem}*.log"), key=lambda p: p.stat().st_mtime)
        reason = ""
        if log_candidates:
            try:
                content = log_candidates[-1].read_text(errors="ignore")
                if "too low interline" in content:
                    reason = " (interline too low — 보표 밀도 과다)"
            except Exception:
                pass
        log.warning(f"Audiveris: .mxl 결과 없음 ({img_path.name}){reason}")
        failed_marker.touch()  # 실패 기록 — 다음 실행 시 즉시 skip
        return None

    latest = candidates[-1]
    if latest != mxl_dst:
        latest.rename(mxl_dst)
    return mxl_dst


def _run_batch_multi(
    img_paths: list[Path],
    out_dir: Path,
    timeout: int = 300,
) -> dict[str, Path | None]:
    """이미지 여러 개를 JVM 한 번에 처리. {stem: mxl_path | None} 반환.

    캐시/실패 마커가 있는 항목은 즉시 skip하고 새 항목만 Audiveris에 넘긴다.
    """
    if not img_paths:
        return {}

    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path | None] = {}
    to_process: list[Path] = []

    for img_path in img_paths:
        stem = img_path.stem
        mxl_dst = out_dir / f"{stem}.mxl"
        failed_marker = out_dir / f"{stem}.failed"
        if mxl_dst.exists():
            log.debug(f"Audiveris cache hit: {mxl_dst.name}")
            results[stem] = mxl_dst
        elif failed_marker.exists():
            log.debug(f"Audiveris 이전 실패 skip: {img_path.name}")
            results[stem] = None
        else:
            to_process.append(img_path)

    if not to_process:
        return results

    cmd = [
        str(_JAVA),
        "-Djava.awt.headless=true",
        "-Dapple.awt.UIElement=true",
        "-cp", _classpath(), "Audiveris",
        "-batch", "-export",
        "-output", str(out_dir),
    ] + [str(p) for p in to_process]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning(f"Audiveris multi-batch timeout ({timeout}s): {len(to_process)}개 이미지")
        for p in to_process:
            results[p.stem] = None
        return results
    except Exception as e:
        log.warning(f"Audiveris multi-batch error: {e}")
        for p in to_process:
            results[p.stem] = None
        return results

    for img_path in to_process:
        stem = img_path.stem
        mxl_dst = out_dir / f"{stem}.mxl"
        candidates = sorted(out_dir.glob(f"{stem}*.mxl"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            log.warning(f"Audiveris: .mxl 결과 없음 ({img_path.name})")
            (out_dir / f"{stem}.failed").touch()
            results[stem] = None
        else:
            latest = candidates[-1]
            if latest != mxl_dst:
                latest.rename(mxl_dst)
            results[stem] = mxl_dst

    return results


def _parse_mxl(
    mxl_path: Path,
    start_measure: int,
    n_parts: int,
    end_measure: int | None = None,
) -> dict[int, list[dict]]:
    """
    Audiveris .mxl → {part_0based_idx: [note_dict]}

    Audiveris는 첫 마디를 0 또는 1로 시작할 수 있다.
    첫 마디 번호를 자동 감지해 offset을 계산:
        global_measure = start_measure + (m_num - first_m_num)

    end_measure: 이 값을 초과하는 마디는 무시 (다음 페이지 범위 침범 방지).
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
        all_measures = part.findall("measure")
        # Audiveris가 0-based 또는 1-based로 시작할 수 있으므로 첫 마디번호를 기준으로 offset 결정
        first_m_num = int(all_measures[0].get("number", "1")) if all_measures else 1

        for measure in all_measures:
            m_num = int(measure.get("number", "1"))
            global_m = start_measure + (m_num - first_m_num)
            # 시스템 범위 초과 마디는 다음 페이지에 귀속 — 건너뜀
            if end_measure is not None and global_m > end_measure:
                continue
            beat_counter = 1.0

            for child in measure:
                if child.tag == "attributes":
                    div_el = child.find("divisions")
                    if div_el is not None:
                        try:
                            d = int(div_el.text)
                            if d > 0:
                                divisions = d
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


_FIFTHS_TO_KEY = {
    0: "C major", 1: "G major", 2: "D major", 3: "A major",
    4: "E major", 5: "B major", 6: "F# major", 7: "C# major",
    -1: "F major", -2: "Bb major", -3: "Eb major", -4: "Ab major",
    -5: "Db major", -6: "Gb major", -7: "Cb major",
}


def extract_key_signature(mxl_path: Path) -> str | None:
    """Audiveris MusicXML에서 조표 추출. 없으면 None."""
    try:
        with zipfile.ZipFile(mxl_path) as z:
            xml_name = next(
                (n for n in z.namelist() if n.endswith(".xml") and "META" not in n),
                None,
            )
            if not xml_name:
                return None
            root = ET.fromstring(z.read(xml_name))
        key_el = root.find(".//key")
        if key_el is None:
            return None
        fifths_el = key_el.find("fifths")
        mode_el = key_el.find("mode")
        if fifths_el is None:
            return None
        fifths = int(fifths_el.text)
        mode = mode_el.text if mode_el is not None else "major"
        key = _FIFTHS_TO_KEY.get(fifths)
        if key and mode == "minor":
            key = key.replace("major", "minor")
        return key
    except Exception:
        pass
    return None


def extract_time_signature(mxl_path: Path) -> str | None:
    """Audiveris MusicXML에서 박자표 추출. 없으면 None."""
    try:
        with zipfile.ZipFile(mxl_path) as z:
            xml_name = next(
                (n for n in z.namelist() if n.endswith(".xml") and "META" not in n),
                None,
            )
            if not xml_name:
                return None
            root = ET.fromstring(z.read(xml_name))
        time_el = root.find(".//time")
        if time_el is None:
            return None
        b = time_el.find("beats")
        bt = time_el.find("beat-type")
        if b is not None and bt is not None:
            return f"{b.text}/{bt.text}"
    except Exception:
        pass
    return None


def extract_notes_page(
    img_path: str | Path,
    start_measure: int,
    active_part_ids: list[str],
    cache_dir: str | Path,
    end_measure: int | None = None,
) -> dict[str, list[dict]] | None:
    """
    페이지 PNG에서 Audiveris로 음표 추출.

    end_measure: 이 마디 번호를 초과하는 음표는 제외 (다음 페이지와 범위 겹침 방지).

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

    by_idx = _parse_mxl(mxl, start_measure, len(active_part_ids), end_measure=end_measure)
    n_notes = sum(len(v) for v in by_idx.values())
    log.debug(f"Audiveris {img_path.name}: {len(by_idx)}파트, {n_notes}음표")

    return {
        active_part_ids[idx]: notes
        for idx, notes in by_idx.items()
        if idx < len(active_part_ids)
    }
