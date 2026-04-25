"""omr.py — oemer 기반 OMR 래퍼

oemer는 단순 피아노 악보에 최적화된 신경망 OMR 도구입니다.
오케스트라 악보나 복잡한 조표에서는 정확도가 낮을 수 있습니다.

속도 최적화:
- save_cache=True: .pkl 캐시 저장 → 재실행 시 ONNX 추론 스킵 (<1초)
- 고정 캐시 디렉토리: tmpdir 대신 output_dir/.oemer_cache/ 사용

Confidence:
- oemer 내부 seg_net이 notehead channel(ch2) float32 확률맵을 계산하지만
  기본적으로 버린다. monkey-patch로 캡처하여 bbox 평균 → per-note confidence.
- 첫 실행(ONNX): .conf.json 사이드카에 저장
- 재실행(캐시 히트): .conf.json 로드
"""
from __future__ import annotations

import hashlib
import json
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

# oemer generate_pred monkey-patch 적용 여부 (프로세스당 1회)
_OEMER_PATCHED: bool = False


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
    data = img.tobytes()
    return hashlib.md5(data).hexdigest()[:12]


# ── oemer monkey-patch: seg_probs 캡처 ───────────────────────────────────────

def _patch_oemer_for_probs() -> None:
    """oemer.ete.generate_pred()를 monkey-patch하여 seg_net 확률맵을 layers에 등록.

    패치는 프로세스당 1회만 적용된다 (_OEMER_PATCHED 플래그).
    oemer는 generate_pred() 내에서 두 번의 ONNX inference를 수행한다:
      1. unet_big: staff + symbols 분리
      2. seg_net: stems/noteheads/clefs 분리
    두 번째 inference의 반환값 `out` (float32, H×W×4) 에서
    channel 2(notehead 확률)를 layers["seg_probs"]로 저장한다.
    """
    global _OEMER_PATCHED
    if _OEMER_PATCHED:
        return
    try:
        import oemer.ete    as _ete
        import oemer.layers as _layers
    except ImportError:
        return

    _orig = _ete.generate_pred

    def _patched(img_path: str, use_tf: bool = False):
        import numpy as np
        from oemer.inference import inference

        MODULE_PATH = _ete.MODULE_PATH

        # pass 1: unet_big (staff + symbols)
        staff_symbols_map, _ = inference(
            os.path.join(MODULE_PATH, "checkpoints/unet_big"),
            img_path,
            step_size=256,
            use_tf=use_tf,
        )
        staff   = np.where(staff_symbols_map == 1, 1, 0)
        symbols = np.where(staff_symbols_map == 2, 1, 0)

        # pass 2: seg_net (noteheads) — 확률맵 캡처
        sep, seg_probs = inference(
            os.path.join(MODULE_PATH, "checkpoints/seg_net"),
            img_path,
            step_size=256,
            manual_th=None,
            use_tf=use_tf,
        )
        stems_rests = np.where(sep == 1, 1, 0)
        notehead    = np.where(sep == 2, 1, 0)
        clefs_keys  = np.where(sep == 3, 1, 0)

        # seg_probs: (H, W, 4) float32 — ch2 = notehead probability
        _layers.register_layer("seg_probs", seg_probs)

        return staff, symbols, stems_rests, notehead, clefs_keys

    _ete.generate_pred = _patched
    _OEMER_PATCHED = True
    log.debug("oemer generate_pred patched for seg_probs capture")


# ── confidence 계산 ──────────────────────────────────────────────────────────

def _run_segnet_inference(img_path: str):
    """seg_net ONNX만 단독 실행하여 notehead 확률맵 반환.

    pkl 캐시 히트 시 generate_pred()가 스킵되어 seg_probs가 없을 때 사용.
    약 30~60초 소요 (일회성: .conf.json에 캐싱 후 재실행 불필요).
    """
    try:
        import oemer.ete as ete
        from oemer.inference import inference
        _, seg_probs = inference(
            os.path.join(ete.MODULE_PATH, "checkpoints/seg_net"),
            img_path,
            step_size=256,
            manual_th=None,
            use_tf=False,
        )
        log.debug(f"seg_net 단독 실행 완료: {img_path}")
        return seg_probs
    except Exception as e:
        log.debug(f"seg_net 단독 실행 실패: {e}")
        return None


def _compute_note_confidences(img_path: str) -> dict[str, float]:
    """extract() 완료 후 layers에서 per-note confidence 계산.

    pkl 캐시 히트 시 seg_probs가 layers에 없으면 seg_net을 단독 실행한다.

    NoteHead.pitch가 None인 oemer 특성 때문에 pitch 기반 매칭 대신
    (track, x-정렬 인덱스) 방식을 사용한다.
    MusicXML <note> 요소의 순서 = NoteHead를 (track, x_center) 정렬한 순서.

    Returns:
        {"0:0": 0.87, "0:1": 0.92, "1:0": 0.75, ...}
        key = f"{track_0indexed}:{note_index_within_track}"
        빈 dict = 계산 불가
    """
    try:
        import oemer.layers as lyr
    except ImportError:
        return {}

    # seg_probs: layers에 있으면 사용, 없으면 seg_net 단독 실행
    try:
        seg_probs = lyr.get_layer("seg_probs")
    except KeyError:
        log.debug("seg_probs 없음 (pkl 캐시 히트) — seg_net 단독 실행")
        seg_probs = _run_segnet_inference(img_path)
        if seg_probs is not None:
            lyr.register_layer("seg_probs", seg_probs)

    try:
        notes = lyr.get_layer("notes")
    except KeyError:
        return {}

    if seg_probs is None or notes is None:
        return {}

    # (track, x_center) 기준 정렬 → MusicXML note 순서와 일치
    note_confs: list[tuple[int, float, float]] = []   # (track, x_center, confidence)
    for note in notes:
        bbox  = getattr(note, "bbox", None)
        track = int(getattr(note, "track", 0))

        if bbox is None:
            continue

        x1, y1 = max(0, int(bbox[0])), max(0, int(bbox[1]))
        x2, y2 = min(seg_probs.shape[1], int(bbox[2])), min(seg_probs.shape[0], int(bbox[3]))

        region = seg_probs[y1:y2, x1:x2, 2]   # notehead channel
        if region.size == 0:
            continue

        x_center = (bbox[0] + bbox[2]) / 2
        note_confs.append((track, x_center, float(region.mean())))

    # track별로 x_center 정렬 후 인덱스 부여
    result: dict[str, float] = {}
    track_notes: dict[int, list[tuple[float, float]]] = {}
    for track, x_center, conf in note_confs:
        track_notes.setdefault(track, []).append((x_center, conf))

    for track, items in track_notes.items():
        items.sort(key=lambda t: t[0])   # x 오름차순 = 왼쪽→오른쪽
        for idx, (_, conf) in enumerate(items):
            result[f"{track}:{idx}"] = conf

    log.debug(f"confidence 계산: {len(result)}개 (track:index 키)")
    return result


def _load_conf_sidecar(conf_path: str) -> dict[str, float]:
    try:
        return json.loads(Path(conf_path).read_text())
    except Exception:
        return {}


def _save_conf_sidecar(conf_path: str, data: dict[str, float]) -> None:
    try:
        Path(conf_path).write_text(json.dumps(data))
    except Exception as e:
        log.debug(f"conf sidecar 저장 실패: {e}")


# ── MusicXML 파서 ─────────────────────────────────────────────────────────────

def _parse_mxl(
    mxl_bytes: bytes,
    start_measure: int,
    end_measure: int,
    conf_lookup: dict[str, float] | None = None,
) -> dict:
    """oemer MusicXML 바이트 → pipeline 포맷 dict 변환.

    conf_lookup: {"0:0": 0.87, "0:1": 0.92, ...}
    key = "{track_0indexed}:{note_index_within_track}"
    rests는 인덱스에서 제외 (NoteHead로 표현되지 않음).
    """
    root = ET.fromstring(mxl_bytes)
    result: dict[str, dict[str, list]] = {
        "Piano treble": {},
        "Piano bass": {},
    }

    part = root.find("part")
    if part is None:
        return result

    # track별 비쉼표 음표 인덱스 (conf_lookup 매칭용)
    track_note_idx: dict[int, int] = {0: 0, 1: 0}

    for oemer_idx, measure in enumerate(part.findall("measure")):
        real_measure = start_measure + oemer_idx
        if real_measure > end_measure:
            break

        m_str = str(real_measure)
        result["Piano treble"][m_str] = []
        result["Piano bass"][m_str]   = []

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
                step     = pitch_el.find("step").text
                octave   = pitch_el.find("octave").text
                alter_el = pitch_el.find("alter")
                acc = ""
                if alter_el is not None:
                    v = float(alter_el.text)
                    acc = "#" if v > 0 else ("b" if v < 0 else "")
                pitch_str = f"{step}{acc}{octave}"
            else:
                pitch_str = "rest"

            # confidence: (track, note_index) 방식으로 룩업
            # chord 음표는 같은 인덱스 사용 (동일 NoteHead 일부)
            track = staff_num - 1
            if conf_lookup and pitch_str != "rest":
                idx  = track_note_idx[track]
                conf = conf_lookup.get(f"{track}:{idx}", 0.5)
                if chord_el is None:
                    track_note_idx[track] += 1
            else:
                conf = 0.5

            note_dict = {
                "beat":       round(beat_val, 2),
                "pitch":      pitch_str,
                "duration":   _TYPE_MAP.get(
                    type_el.text if type_el is not None else "quarter", "quarter"
                ),
                "dots":       dots,
                "voice":      voice,
                "tie_start":  any(t.get("type") == "start" for t in tie_els),
                "tie_end":    any(t.get("type") == "stop"  for t in tie_els),
                "confidence": conf,
            }

            staff_key = "Piano treble" if staff_num == 1 else "Piano bass"
            result[staff_key][m_str].append(note_dict)

    return result


def _parse_mxl_single(
    mxl_bytes: bytes,
    start_measure: int,
    end_measure: int,
    conf_lookup: dict[str, float] | None = None,
) -> list[dict]:
    """단일 보표 oemer MusicXML → note dict list.

    conf_lookup: {"0:0": 0.87, "0:1": 0.92, ...}
    단일 보표이므로 track=0 고정.
    """
    root = ET.fromstring(mxl_bytes)
    notes_out: list[dict] = []

    part = root.find("part")
    if part is None:
        return notes_out

    beat_counter = 1.0
    div = 1
    note_idx = 0   # track=0 비쉼표 음표 인덱스

    for oemer_idx, measure in enumerate(part.findall("measure")):
        real_measure = start_measure + oemer_idx
        if real_measure > end_measure:
            break

        beat_counter = 1.0

        for child in measure:
            if child.tag == "attributes":
                div_el = child.find("divisions")
                if div_el is not None:
                    try:
                        div = int(div_el.text)
                    except (ValueError, TypeError):
                        pass
            elif child.tag == "note":
                note = child
                pitch_el = note.find("pitch")
                type_el  = note.find("type")
                dots     = len(note.findall("dot"))
                voice_el = note.find("voice")
                chord_el = note.find("chord")
                tie_els  = note.findall("tie")
                dur_el   = note.find("duration")

                voice     = int(voice_el.text) if voice_el is not None else 1
                dur_ticks = int(dur_el.text)   if dur_el   is not None else div

                if chord_el is None:
                    beat_val = beat_counter
                    beat_counter += dur_ticks / div
                else:
                    beat_val = beat_counter - dur_ticks / div

                if pitch_el is not None:
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
                    pitch_str = "rest"

                if conf_lookup and pitch_str != "rest":
                    conf = conf_lookup.get(f"0:{note_idx}", 0.5)
                    if chord_el is None:
                        note_idx += 1
                else:
                    conf = 0.5

                notes_out.append({
                    "measure":    real_measure,
                    "beat":       round(beat_val, 3),
                    "pitch":      pitch_str,
                    "duration":   _TYPE_MAP.get(
                        type_el.text if type_el is not None else "quarter", "quarter"
                    ),
                    "dots":       dots,
                    "voice":      voice,
                    "tie_start":  any(t.get("type") == "start" for t in tie_els),
                    "tie_end":    any(t.get("type") == "stop"  for t in tie_els),
                    "confidence": conf,
                })

    return notes_out


# ── 공개 추출 함수 ────────────────────────────────────────────────────────────

def extract_notes_oemer_single(
    cropped: Image.Image,
    start_measure: int,
    end_measure: int,
) -> list[dict] | None:
    """단일 보표 이미지에서 oemer로 음표 추출 (Tier 2-4)."""
    try:
        import oemer.ete as ete
    except ImportError:
        log.error("oemer 미설치.")
        return None

    _patch_oemer_for_probs()

    cache_dir = _get_cache_dir()
    img_key   = f"s{_img_hash(cropped)}"
    img_path  = str(cache_dir / f"{img_key}.png")
    mxl_path  = str(cache_dir / f"{img_key}.musicxml")
    conf_path = str(cache_dir / f"{img_key}.conf.json")

    if not Path(img_path).exists():
        cropped.save(img_path)

    class _Args:
        def __init__(self):
            self.img_path       = img_path
            self.output_path    = str(cache_dir)
            self.use_tf         = False
            self.save_cache     = True
            self.without_deskew = True

    if Path(mxl_path).exists() and Path(conf_path).exists():
        log.debug(f"oemer single full cache hit: {img_key}")
        conf_lookup = _load_conf_sidecar(conf_path)
        return _parse_mxl_single(Path(mxl_path).read_bytes(), start_measure, end_measure,
                                  conf_lookup or None)

    # musicxml은 있지만 conf.json이 없거나, 둘 다 없는 경우 → extract 실행
    try:
        out = ete.extract(_Args())
    except Exception as e:
        log.warning(f"oemer single 추출 실패 ({img_key}): {e}")
        return None

    if out is None or not Path(out).exists():
        log.warning(f"oemer single MusicXML 없음 ({img_key})")
        return None

    conf_lookup = _compute_note_confidences(img_path)
    if conf_lookup:
        _save_conf_sidecar(conf_path, conf_lookup)
        log.debug(f"oemer single conf saved: {len(conf_lookup)} entries")

    return _parse_mxl_single(Path(out).read_bytes(), start_measure, end_measure,
                              conf_lookup or None)


def extract_notes_oemer(
    cropped: Image.Image,
    start_measure: int,
    end_measure: int,
) -> dict | None:
    """Piano treble+bass 영역 이미지에서 oemer로 음표 추출.

    캐시 히트 시 ONNX 추론 없이 기존 .pkl 재사용 → 거의 즉시 완료.
    confidence는 .conf.json 사이드카에 캐싱.

    Returns:
        {"Piano treble": {m: [notes]}, "Piano bass": {m: [notes]}}
        실패 시 None.
    """
    try:
        import oemer.ete as ete
    except ImportError:
        log.error("oemer 미설치. `pip install oemer` 실행 후 재시도.")
        return None

    _patch_oemer_for_probs()

    cache_dir = _get_cache_dir()
    img_key   = _img_hash(cropped)
    img_path  = str(cache_dir / f"crop_{img_key}.png")
    mxl_path  = str(cache_dir / f"crop_{img_key}.musicxml")
    conf_path = str(cache_dir / f"crop_{img_key}.conf.json")

    if not Path(img_path).exists():
        cropped.save(img_path)

    class _Args:
        def __init__(self):
            self.img_path       = img_path
            self.output_path    = str(cache_dir)
            self.use_tf         = False
            self.save_cache     = True
            self.without_deskew = True

    if Path(mxl_path).exists() and Path(conf_path).exists():
        log.debug(f"oemer full cache hit: {img_key}")
        conf_lookup = _load_conf_sidecar(conf_path)
        result = _parse_mxl(Path(mxl_path).read_bytes(), start_measure, end_measure,
                             conf_lookup or None)
    else:
        # musicxml 없거나 conf.json 없음 → extract 실행
        # pkl이 있으면 fast path(~2초), 없으면 ONNX full run
        try:
            out = ete.extract(_Args())
        except Exception as e:
            log.warning(f"oemer 추출 실패: {e}")
            return None

        if out is None or not Path(out).exists():
            log.warning("oemer MusicXML 출력 없음")
            return None

        conf_lookup = _compute_note_confidences(img_path)
        if conf_lookup:
            _save_conf_sidecar(conf_path, conf_lookup)
            log.debug(f"oemer conf saved: {len(conf_lookup)} entries")

        result = _parse_mxl(Path(out).read_bytes(), start_measure, end_measure,
                             conf_lookup or None)

    n_treble = sum(len(v) for v in result["Piano treble"].values())
    n_bass   = sum(len(v) for v in result["Piano bass"].values())
    log.debug(f"oemer: treble={n_treble}, bass={n_bass} notes")

    return result
