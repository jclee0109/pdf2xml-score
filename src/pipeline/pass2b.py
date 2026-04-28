"""Pass 2b: 음표 추출 — Tier 1 oemer OMR (병렬), Tier 2~4 단일 보표 oemer"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from ..models.score import RawNote, ScoreLayout, SystemInfo
from ..utils.render import crop_part_range
from ..utils.omr import extract_notes_oemer, extract_notes_oemer_single, set_cache_dir

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


# ── Tier 2~4: 단일 보표 oemer 추출 ───────────────────────────────────────────

def _extract_single_part_worker(
    img_path: str,
    system_dict: dict,
    part_idx: int,
    part_id: str,
    cache_dir: str,
) -> list[dict]:
    """ProcessPoolExecutor worker: 단일 보표 파트 oemer 추출 (Tier 2~4).

    Piano 2단 worker(_extract_system_worker)와 독립적으로 실행.
    반환: note dict list (part_id / source_system 포함).
    """
    from ..utils.omr import extract_notes_oemer_single, set_cache_dir
    from ..utils.render import crop_part_range

    set_cache_dir(cache_dir)
    img = Image.open(img_path)

    n_parts = len(system_dict["active_parts"])

    cropped = crop_part_range(
        img,
        system_dict["y_top_px"], system_dict["y_bottom_px"],
        part_idx, part_idx, n_parts,
        extra_top=10, extra_bottom=5,
    )

    notes_raw = extract_notes_oemer_single(
        cropped,
        system_dict["start_measure"],
        system_dict["end_measure"],
    )
    if notes_raw is None:
        return []

    return [
        {**n, "part_id": part_id, "source_system": system_dict["system_index"]}
        for n in notes_raw
    ]


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

def _tier_parts(layout: ScoreLayout, tiers: list[int]) -> list[tuple[str, str]]:
    """지정 Tier에 해당하는 (part_id, part_name) 목록. Tier 1(Piano)은 제외."""
    tier_names: set[str] = set()
    for t in tiers:
        if t in TIERS:
            tier_names.update(TIERS[t])
    if "extra" in [t for t in tiers if isinstance(t, str)]:
        tier_names.update(TIER_EXTRA)

    return [
        (p.id, p.name)
        for p in layout.parts
        if p.name in tier_names
    ]


def run_pass2b(
    page_images: list[Image.Image],
    layout: ScoreLayout,
    tiers: list[int] | None = None,
    cache_dir: str | Path = "output/.oemer_cache",
    parallel: bool = True,
) -> list[RawNote]:
    """Pass 2b 실행.

    tiers=None → Tier 1 (Piano)만 처리.
    tiers=[1,2,3,4] → 전체 처리 (Tier 2-4: 단일 보표 oemer, 병렬).
    parallel=True: ProcessPoolExecutor 사용.
    """
    target_tiers = tiers if tiers is not None else [1]
    do_tier1     = 1 in target_tiers
    extra_tiers  = [t for t in target_tiers if t != 1]

    set_cache_dir(cache_dir)
    cache_dir_str = str(Path(cache_dir).resolve())

    # ── 이미지 → 임시 파일 ───────────────────────────────────────────────────
    parts_list = [{"name": p.name, "clef": p.clef} for p in layout.parts]

    tmp_dir = Path(cache_dir_str) / "_pages_tmp"
    tmp_dir.mkdir(exist_ok=True)
    img_paths: list[str] = []
    for i, img in enumerate(page_images):
        p = str(tmp_dir / f"page-{i+1}.png")
        if not Path(p).exists():
            img.save(p)
        img_paths.append(p)

    system_dicts = [
        {
            "page": s.page, "system_index": s.system_index,
            "start_measure": s.start_measure, "end_measure": s.end_measure,
            "y_top_px": s.y_top_px, "y_bottom_px": s.y_bottom_px,
            "active_parts": s.active_parts,
        }
        for s in layout.systems
    ]

    # ── 작업 목록 구성 ────────────────────────────────────────────────────────
    # (worker_fn, *args) 튜플 리스트. label은 로그용.
    tasks: list[tuple] = []

    if do_tier1 and parallel:
        for i, sd in enumerate(system_dicts):
            tasks.append(("tier1", i, _extract_system_worker,
                          img_paths[sd["page"] - 1], sd, parts_list, cache_dir_str))

    if extra_tiers:
        tier_part_list = _tier_parts(layout, extra_tiers)
        for si, sd in enumerate(system_dicts):
            active = sd["active_parts"]
            for pid, pname in tier_part_list:
                if pid not in active:
                    continue
                part_idx = active.index(pid)
                tasks.append(("tier24", (si, pid), _extract_single_part_worker,
                              img_paths[sd["page"] - 1], sd, part_idx, pid, cache_dir_str))

    n_tasks = len(tasks)
    max_workers = min(n_tasks or 1, os.cpu_count() or 4)
    log.info(
        f"Pass 2b: Tier {target_tiers}, {n_tasks}개 작업 병렬 처리 (workers={max_workers})"
    )

    # ── 병렬 실행 ─────────────────────────────────────────────────────────────
    tier1_results:  dict[int, list[dict]] = {}
    tier24_results: dict[tuple, list[dict]] = {}

    if not parallel or not tasks:
        # 순차 폴백 (Tier 1 only, parallel=False)
        all_notes_seq: list[RawNote] = []
        for system in layout.systems:
            page_img = page_images[system.page - 1]
            notes = extract_notes_for_system(page_img, system, layout)
            all_notes_seq.extend(notes)
        log.info(f"Pass 2b 완료 (순차): 총 {len(all_notes_seq)}개 음표")
        return all_notes_seq

    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        future_map: dict = {}
        for task in tasks:
            kind = task[0]
            key  = task[1]
            fn   = task[2]
            args = task[3:]
            fut  = ex.submit(fn, *args)
            future_map[fut] = (kind, key)

        for future in as_completed(future_map):
            kind, key = future_map[future]
            try:
                note_dicts = future.result()
                if kind == "tier1":
                    tier1_results[key] = note_dicts
                    sd = system_dicts[key]
                    log.info(
                        f"  [Tier1] p{sd['page']} s{sd['system_index']} "
                        f"m{sd['start_measure']}~{sd['end_measure']} → {len(note_dicts)}개"
                    )
                else:
                    si, pid = key
                    tier24_results[key] = note_dicts
                    sd = system_dicts[si]
                    log.info(
                        f"  [Tier2-4] {pid} p{sd['page']} "
                        f"m{sd['start_measure']}~{sd['end_measure']} → {len(note_dicts)}개"
                    )
            except Exception as e:
                if kind == "tier1":
                    sd = system_dicts[key]
                    log.warning(f"  [Tier1] p{sd['page']} s{key} 실패: {e}")
                    tier1_results[key] = []
                else:
                    si, pid = key
                    log.warning(f"  [Tier2-4] {pid} sys{si} 실패: {e}")
                    tier24_results[key] = []

    # ── 결과 합치기 ───────────────────────────────────────────────────────────
    all_raw: list[RawNote] = []

    for idx in sorted(tier1_results):
        for nd in tier1_results[idx]:
            all_raw.append(RawNote(**nd))

    for key in sorted(tier24_results, key=lambda k: (k[0], k[1])):
        for nd in tier24_results[key]:
            all_raw.append(RawNote(**nd))

    log.info(f"Pass 2b 완료: 총 {len(all_raw)}개 음표")
    return all_raw


# ── Audiveris 파트별 fallback ─────────────────────────────────────────────────

def _extract_parts_individually(
    img_path: str,
    system: "SystemInfo",
    layout: "ScoreLayout",
    start_measure: int,
    end_measure: int,
    cache_dir: Path,
    strip_size: int = 7,
    max_workers: int = 3,
) -> dict[str, list[dict]]:
    """
    전체 페이지 Audiveris 실패 시 N파트씩 strip으로 crop + 업스케일해 처리.

    N파트 strip을 ThreadPoolExecutor로 병렬 실행:
    - Audiveris는 subprocess이므로 GIL 무관, Thread 기반으로 충분
    - max_workers=3: 동시 Audiveris 프로세스 수 제한 (메모리 ~1.5GB)

    strip 실패 시 (악기 패밀리 경계 가로지름) → 개별 파트도 병렬 재시도
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..utils.audiveris import _run_batch, _parse_mxl
    from ..utils.render import load_image, crop_part_range
    from PIL import Image as _Image

    _AUDIVERIS_MAX_PX = 18_000_000

    page_img = load_image(img_path)
    active_parts = system.active_parts
    n_parts = len(active_parts)
    result: dict[str, list[dict]] = {}

    strip_starts = list(range(0, n_parts, strip_size))
    log.info(f"  → strip 모드: {n_parts}파트 / {strip_size} = {len(strip_starts)}개 strip (병렬 {max_workers})")

    # ── strip crop 이미지 미리 생성 (IO는 직렬로) ────────────────────────────
    strip_infos: list[tuple[int, int, list[str], Path]] = []
    for s_start in strip_starts:
        s_end = min(s_start + strip_size - 1, n_parts - 1)
        strip_pids = active_parts[s_start:s_end + 1]
        strip_stem = f"strip_p{system.page}_{s_start}_{s_end}"
        crop_path = cache_dir / f"{strip_stem}.png"

        if not (cache_dir / f"{strip_stem}.mxl").exists() and \
           not (cache_dir / f"{strip_stem}.failed").exists() and \
           not crop_path.exists():
            crop = crop_part_range(
                page_img, system.y_top_px, system.y_bottom_px,
                s_start, s_end, n_parts,
            )
            w, h = crop.size
            if w * h == 0:
                continue
            scale = min(3.0, (_AUDIVERIS_MAX_PX / (w * h)) ** 0.5)
            if scale > 1.05:
                crop = crop.resize((int(w * scale), int(h * scale)), _Image.LANCZOS)
            crop.save(crop_path)

        strip_infos.append((s_start, s_end, strip_pids, crop_path))

    # ── strip 병렬 실행 ───────────────────────────────────────────────────────
    failed_strips: list[tuple[int, int, list[str]]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_map = {
            ex.submit(_run_batch, crop_path, cache_dir, 120): (s_start, s_end, strip_pids)
            for s_start, s_end, strip_pids, crop_path in strip_infos
        }
        for future in as_completed(future_map):
            s_start, s_end, strip_pids = future_map[future]
            n_strip_parts = s_end - s_start + 1
            mxl = future.result()
            if mxl is None:
                log.info(f"  strip {s_start}~{s_end} 실패 → 개별 파트 재시도")
                failed_strips.append((s_start, s_end, strip_pids))
            else:
                strip_notes = _parse_mxl(mxl, start_measure, n_strip_parts, end_measure=end_measure)
                for local_idx, pid in enumerate(strip_pids):
                    result[pid] = strip_notes.get(local_idx, [])

    # ── 실패 strip → 개별 파트 병렬 재시도 ──────────────────────────────────
    if failed_strips:
        # 개별 파트 crop 이미지 준비
        part_tasks: list[tuple[str, Path]] = []
        for s_start, s_end, strip_pids in failed_strips:
            for i, pid in enumerate(strip_pids):
                part_stem = f"part_{pid}_p{system.page}"
                p_path = cache_dir / f"{part_stem}.png"
                if not (cache_dir / f"{part_stem}.mxl").exists() and \
                   not (cache_dir / f"{part_stem}.failed").exists() and \
                   not p_path.exists():
                    p_crop = crop_part_range(
                        page_img, system.y_top_px, system.y_bottom_px,
                        s_start + i, s_start + i, n_parts,
                    )
                    pw, ph = p_crop.size
                    if pw * ph == 0:
                        continue
                    p_scale = min(5.0, (_AUDIVERIS_MAX_PX / (pw * ph)) ** 0.5)
                    if p_scale > 1.05:
                        p_crop = p_crop.resize(
                            (int(pw * p_scale), int(ph * p_scale)), _Image.LANCZOS
                        )
                    p_crop.save(p_path)
                part_tasks.append((pid, p_path))

        with ThreadPoolExecutor(max_workers=max_workers + 1) as ex:
            future_map = {
                ex.submit(_run_batch, p_path, cache_dir, 90): pid
                for pid, p_path in part_tasks
            }
            for future in as_completed(future_map):
                pid = future_map[future]
                p_mxl = future.result()
                if p_mxl:
                    part_notes = _parse_mxl(p_mxl, start_measure, 1, end_measure=end_measure)
                    result[pid] = part_notes.get(0, [])

    n_total = sum(len(v) for v in result.values())
    log.info(f"  → strip 완료: {len(result)}/{n_parts}파트, {n_total}개 음표")
    return result


# ── Audiveris 경로 ────────────────────────────────────────────────────────────

def run_pass2b_audiveris(
    page_img_paths: list[str],
    layout: ScoreLayout,
    cache_dir: str | Path = "output/.audiveris_cache",
) -> list[RawNote]:
    """Audiveris로 전체 음표 추출.

    페이지 단위로 Audiveris를 실행하고, 파트 위치(인덱스)로 ScoreLayout에 매핑.
    Piano 파트가 있어도 Audiveris로 통합 처리 (오케스트라 + 피아노 혼합 악보 지원).

    Args:
        page_img_paths: 이미 저장된 페이지 PNG 경로 목록 (page-01.png, ...)
        layout: ScoreLayout
        cache_dir: Audiveris 결과 캐시 디렉토리
    """
    from ..utils.audiveris import extract_notes_page, is_available

    if not is_available():
        log.error("Audiveris 미설치 — /Applications/Audiveris.app 필요")
        return []

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 페이지별 시스템 그루핑 (1-based page number)
    page_to_systems: dict[int, list[SystemInfo]] = {}
    for s in layout.systems:
        page_to_systems.setdefault(s.page, []).append(s)

    all_raw: list[RawNote] = []
    processed_pages: set[int] = set()

    for page_num in sorted(page_to_systems):
        systems_on_page = page_to_systems[page_num]
        img_path = page_img_paths[page_num - 1]

        # 페이지 첫 시스템 기준으로 start_measure 결정
        first_sys = min(systems_on_page, key=lambda s: s.start_measure)
        active_parts = first_sys.active_parts

        log.info(
            f"Pass 2b [Audiveris]: p{page_num} "
            f"m{first_sys.start_measure}~{systems_on_page[-1].end_measure} "
            f"({len(active_parts)}파트)"
        )

        last_sys = max(systems_on_page, key=lambda s: s.end_measure)
        by_part = extract_notes_page(
            img_path=img_path,
            start_measure=first_sys.start_measure,
            active_part_ids=active_parts,
            cache_dir=cache_dir,
            end_measure=last_sys.end_measure,
        )

        if by_part is None:
            # 전체 페이지 실패 → 파트별 개별 crop + 업스케일 fallback
            log.info(f"  → 전체 페이지 실패, 파트별 개별 처리 시도: p{page_num}")
            by_part = _extract_parts_individually(
                img_path=img_path,
                system=first_sys,
                layout=layout,
                start_measure=first_sys.start_measure,
                end_measure=last_sys.end_measure,
                cache_dir=cache_dir,
            )
            if not by_part:
                log.warning(f"  → 개별 처리도 실패: p{page_num}")
                continue

        n_notes = sum(len(v) for v in by_part.values())
        log.info(f"  → {n_notes}개 음표")

        for pid, note_dicts in by_part.items():
            part_idx = int(pid[1:])
            part = layout.parts[part_idx]
            source_sys = first_sys.system_index
            for nd in note_dicts:
                all_raw.append(RawNote(
                    measure=nd["measure"],
                    beat=nd["beat"],
                    pitch=nd["pitch"],
                    duration=nd["duration"],
                    dots=nd["dots"],
                    tie_start=nd["tie_start"],
                    tie_end=nd["tie_end"],
                    voice=nd["voice"],
                    confidence=nd["confidence"],
                    part_id=pid,
                    source_system=source_sys,
                ))

        processed_pages.add(page_num)

    log.info(f"Pass 2b [Audiveris] 완료: {len(processed_pages)}페이지, 총 {len(all_raw)}개 음표")
    return all_raw
