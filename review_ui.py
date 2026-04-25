"""
검수 UI v2 — 신뢰도 기반 형광팬 하이라이트 + 음표·코드 편집

실행: streamlit run review_ui.py
"""
import json
import logging
from pathlib import Path

import streamlit as st
from PIL import Image, ImageDraw

logging.disable(logging.CRITICAL)

from src.pipeline.pass1 import layout_from_json
from src.pipeline.pass2a import chords_from_json
from src.pipeline.pass2b import notes_from_json
from src.pipeline.pass2c import lyrics_from_json
from src.pipeline.pass3 import (
    validate_chords, validate_notes, ValidatedChord,
    DURATION_QUARTERS, _time_sig_quarters, check_note_anomalies,
)
from src.models.score import RawNote, SystemInfo, ScoreLayout

# ── 경로 ──────────────────────────────────────────────────────────────────────

OUTPUT_DIR       = Path("output")
LAYOUT_PATH      = OUTPUT_DIR / "pass1_layout.json"
CHORDS_PATH      = OUTPUT_DIR / "pass2a_chords.json"
NOTES_PATH       = OUTPUT_DIR / "pass2b_notes.json"
LYRICS_PATH      = OUTPUT_DIR / "pass2c_lyrics.json"
CORRECTIONS_PATH = OUTPUT_DIR / "corrections.json"

DURATIONS = ["whole", "half", "quarter", "eighth", "16th", "32nd"]

# ── 색상 ──────────────────────────────────────────────────────────────────────

def _conf_rgba(conf: float) -> tuple[int, int, int, int]:
    if conf < 0.50:
        return (255, 50,  50,  100)   # 적색 — 검수 필요
    if conf < 0.75:
        return (255, 210,  0,   80)   # 황색 — 주의
    return (80, 210,  80,  20)        # 연녹 — 정상

def _conf_badge(conf: float) -> str:
    if conf < 0.50: return "🔴"
    if conf < 0.75: return "🟡"
    return "🟢"

# ── 데이터 로드 ───────────────────────────────────────────────────────────────

@st.cache_data
def load_all():
    layout    = layout_from_json(LAYOUT_PATH)
    chords    = chords_from_json(CHORDS_PATH)
    validated = validate_chords(chords, layout)
    notes     = notes_from_json(NOTES_PATH)   if NOTES_PATH.exists()   else []
    lyrics    = lyrics_from_json(LYRICS_PATH) if LYRICS_PATH.exists()  else []
    anomalies = check_note_anomalies(notes, layout) if notes else {}
    return layout, validated, notes, lyrics, anomalies

@st.cache_data
def load_page_img(page: int) -> Image.Image:
    return Image.open(OUTPUT_DIR / f"page-{page}.png").convert("RGB")

# ── Rule 4 플래그 계산 ────────────────────────────────────────────────────────

def compute_rule4_flags(raw_notes: list[RawNote], layout: ScoreLayout) -> set[tuple[str, int]]:
    """duration 불일치 (part_id, measure) 집합."""
    from collections import defaultdict
    by_pm: dict = defaultdict(list)
    for n in raw_notes:
        by_pm[(n.part_id, n.measure)].append(n)

    flags: set[tuple[str, int]] = set()
    for (pid, m), notes in by_pm.items():
        sys = next((s for s in layout.systems if s.start_measure <= m <= s.end_measure), None)
        if sys is None:
            continue
        if all(n.pitch == "rest" for n in notes):
            continue
        expected = _time_sig_quarters(sys.time_signature)
        by_voice: dict[int, list] = defaultdict(list)
        for n in notes:
            by_voice[n.voice].append(n)
        for v_notes in by_voice.values():
            seen: dict[float, float] = {}
            for n in v_notes:
                dur = DURATION_QUARTERS.get(n.duration, 1.0) * (
                    1 + sum(0.5**i for i in range(1, n.dots + 1))
                )
                if n.beat not in seen or dur > seen[n.beat]:
                    seen[n.beat] = dur
            if abs(sum(seen.values()) - expected) > 0.01:
                flags.add((pid, m))
    return flags

# ── 신뢰도 계산 ───────────────────────────────────────────────────────────────

def measure_confidence(
    m: int,
    raw_notes: list[RawNote],
    validated_chords: list[ValidatedChord],
    rule4_flags: set[tuple[str, int]],
    anomalies: dict[tuple[str, int], list[str]],
) -> tuple[float, list[str]]:
    conf  = 1.0
    flags: list[str] = []

    # 코드 심볼 (Rule 1~3)
    for vc in validated_chords:
        if vc.measure == m and vc.needs_review:
            conf = min(conf, vc.confidence)
            flags.append(f"코드 `{vc.chord_text}` — {vc.confidence:.0%}")

    # Rule 4: 박자 불일치
    bad_parts = [pid for (pid, mm) in rule4_flags if mm == m]
    if bad_parts:
        conf = min(conf, 0.40)
        flags.append(f"박자 불일치: {', '.join(bad_parts)}")

    # Rule 5~7: 음표 이상 (도약, 음표 수 이상치, 음역 이탈)
    anom_msgs: list[str] = []
    for (pid, mm), msgs in anomalies.items():
        if mm == m:
            anom_msgs.extend(msgs)
    if anom_msgs:
        # 도약/이상치 → 🟡(0.55), 음역 이탈 → 🔴(0.45)
        has_range = any("Rule 7" in msg for msg in anom_msgs)
        penalty   = 0.45 if has_range else 0.55
        conf      = min(conf, penalty)
        for msg in anom_msgs:
            flags.append(msg)

    return conf, flags

# ── 이미지 렌더링 ─────────────────────────────────────────────────────────────

def _measure_x(system: SystemInfo, m: int, img_w: int) -> tuple[int, int]:
    n   = system.end_measure - system.start_measure + 1
    idx = m - system.start_measure
    w   = img_w / n
    return int(idx * w), int((idx + 1) * w)


def render_system_strip(
    page_img: Image.Image,
    system: SystemInfo,
    conf_map: dict[int, float],
) -> Image.Image:
    """시스템 전체 crop + 마디별 형광팬 오버레이."""
    strip   = page_img.crop((0, system.y_top_px, page_img.width, system.y_bottom_px)).copy()
    overlay = Image.new("RGBA", strip.size, (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    for m in range(system.start_measure, system.end_measure + 1):
        conf = conf_map.get(m, 1.0)
        x1, x2 = _measure_x(system, m, strip.width)
        draw.rectangle([x1, 0, x2 - 1, strip.height], fill=_conf_rgba(conf))

    return Image.alpha_composite(strip.convert("RGBA"), overlay).convert("RGB")


def render_measure_crop(
    page_img: Image.Image,
    system: SystemInfo,
    m: int,
    conf: float,
) -> Image.Image:
    """단일 마디 crop + 형광팬."""
    x1, x2 = _measure_x(system, m, page_img.width)
    crop    = page_img.crop((x1, system.y_top_px, x2, system.y_bottom_px)).copy()
    overlay = Image.new("RGBA", crop.size, _conf_rgba(conf))
    return Image.alpha_composite(crop.convert("RGBA"), overlay).convert("RGB")

# ── Corrections I/O ───────────────────────────────────────────────────────────

def load_corrections() -> dict:
    if CORRECTIONS_PATH.exists():
        return json.loads(CORRECTIONS_PATH.read_text())
    return {"chords": {}, "notes": {}}

def save_corrections(c: dict) -> None:
    CORRECTIONS_PATH.write_text(json.dumps(c, indent=2, ensure_ascii=False))

# ── MusicXML 재생성 ───────────────────────────────────────────────────────────

def rebuild_musicxml(corrections: dict) -> str:
    from src.pipeline.pass3 import ValidatedChord as VC
    from src.pipeline.build import build_musicxml
    from src.models.chord import parse_chord_text

    layout, validated, raw_notes, raw_lyrics, _ = load_all()

    # 코드 수정 적용
    chord_corr = corrections.get("chords", {})
    corrected_chords = []
    for vc in validated:
        if str(vc.measure) in chord_corr and chord_corr[str(vc.measure)].strip():
            t = chord_corr[str(vc.measure)].strip()
            corrected_chords.append(VC(
                measure=vc.measure, beat=vc.beat,
                chord_text=t, normalized=parse_chord_text(t),
                confidence=1.0, flags=["corrected"], needs_review=False,
            ))
        else:
            corrected_chords.append(vc)

    # 음표 수정 적용
    note_corr = corrections.get("notes", {})
    replaced: set[tuple[str, int]] = set()
    corrected_notes: list[RawNote] = []

    for key, nd_list in note_corr.items():
        pid, m_str = key.rsplit("-", 1)
        m = int(m_str)
        replaced.add((pid, m))
        for nd in nd_list:
            corrected_notes.append(RawNote(
                measure=m, beat=nd["beat"], pitch=nd["pitch"],
                duration=nd["duration"], dots=nd.get("dots", 0),
                tie_start=nd.get("tie_start", False), tie_end=nd.get("tie_end", False),
                voice=nd.get("voice", 1), confidence=1.0,
                part_id=pid, source_system=0,
            ))

    for n in raw_notes:
        if (n.part_id, n.measure) not in replaced:
            corrected_notes.append(n)

    corrected_notes = validate_notes(corrected_notes, layout)
    xml_bytes = build_musicxml(layout, corrected_chords, corrected_notes, raw_lyrics or None)
    out_path  = OUTPUT_DIR / "output.musicxml"
    out_path.write_bytes(xml_bytes)
    return str(out_path)

# ── 코드 편집 섹션 ─────────────────────────────────────────────────────────────

def show_chord_section(
    m: int,
    validated_chords: list[ValidatedChord],
    corrections: dict,
) -> None:
    m_chords = [vc for vc in validated_chords if vc.measure == m]
    if not m_chords:
        return

    st.markdown("**코드 심볼**")
    chord_corr = corrections.setdefault("chords", {})

    for vc in m_chords:
        col_orig, col_badge, col_inp, col_btn = st.columns([2, 1, 2, 1])
        col_orig.markdown(f"`{vc.chord_text}`")
        col_badge.markdown(_conf_badge(vc.confidence) + f" {vc.confidence:.0%}")
        new_val = col_inp.text_input(
            "수정", value=chord_corr.get(str(m), ""),
            placeholder=vc.chord_text, key=f"chord_inp_{m}",
            label_visibility="collapsed",
        )
        if col_btn.button("저장", key=f"chord_save_{m}"):
            if new_val.strip():
                chord_corr[str(m)] = new_val.strip()
            else:
                chord_corr.pop(str(m), None)
            save_corrections(corrections)
            st.success("코드 저장됨")
            st.rerun()

# ── 음표 편집 섹션 ─────────────────────────────────────────────────────────────

def _current_notes_for(
    pid: str, m: int,
    raw_notes: list[RawNote],
    note_corr: dict,
) -> list[dict]:
    key = f"{pid}-{m}"
    if key in note_corr:
        return list(note_corr[key])
    return [
        {
            "beat": n.beat, "pitch": n.pitch, "duration": n.duration,
            "dots": n.dots, "voice": n.voice,
            "tie_start": n.tie_start, "tie_end": n.tie_end,
            "confidence": n.confidence,
        }
        for n in raw_notes if n.part_id == pid and n.measure == m
    ]


def show_note_section(
    m: int,
    layout: ScoreLayout,
    raw_notes: list[RawNote],
    rule4_flags: set[tuple[str, int]],
    corrections: dict,
) -> None:
    note_corr   = corrections.setdefault("notes", {})
    parts_in_m  = sorted(set(n.part_id for n in raw_notes if n.measure == m))

    if not parts_in_m:
        st.caption("추출된 음표 없음")
        return

    st.markdown("**음표**")

    for pid in parts_in_m:
        part_name = layout.parts[int(pid[1:])].name
        has_flag  = (pid, m) in rule4_flags
        badge     = "⚠️ " if has_flag else ""
        current   = _current_notes_for(pid, m, raw_notes, note_corr)

        with st.expander(f"{badge}**{part_name}** — {len(current)}개", expanded=has_flag):
            # 헤더 행
            hcols = st.columns([1.2, 2.0, 2.0, 0.7, 0.7, 0.7])
            for col, label in zip(hcols, ["박자", "음높이", "음길이", "점", "성부", "삭제"]):
                col.caption(label)

            with st.form(key=f"note_form_{pid}_{m}"):
                rows_new: list[dict] = []
                deletes:  list[bool] = []

                for i, nd in enumerate(current):
                    c = st.columns([1.2, 2.0, 2.0, 0.7, 0.7, 0.7])
                    beat  = c[0].number_input("박자",  value=float(nd["beat"]),
                                               min_value=0.0, step=0.25,
                                               key=f"b_{pid}_{m}_{i}",
                                               label_visibility="collapsed")
                    pitch = c[1].text_input("음높이", value=nd["pitch"],
                                             key=f"p_{pid}_{m}_{i}",
                                             label_visibility="collapsed")
                    dur_i = DURATIONS.index(nd["duration"]) if nd["duration"] in DURATIONS else 2
                    dur   = c[2].selectbox("음길이", DURATIONS, index=dur_i,
                                            key=f"d_{pid}_{m}_{i}",
                                            label_visibility="collapsed")
                    dots  = c[3].number_input("점",  value=int(nd.get("dots", 0)),
                                               min_value=0, max_value=2,
                                               key=f"dt_{pid}_{m}_{i}",
                                               label_visibility="collapsed")
                    voice = c[4].number_input("성부", value=int(nd.get("voice", 1)),
                                               min_value=1, max_value=4,
                                               key=f"v_{pid}_{m}_{i}",
                                               label_visibility="collapsed")
                    delete = c[5].checkbox("삭제", key=f"del_{pid}_{m}_{i}",
                                            label_visibility="collapsed")

                    # 신뢰도 배지 (미묘하게 표시)
                    nc = float(nd.get("confidence", 1.0))
                    if nc < 0.75:
                        c[1].caption(f"{_conf_badge(nc)} {nc:.0%}")

                    rows_new.append({
                        "beat": beat, "pitch": pitch, "duration": dur,
                        "dots": dots, "voice": voice,
                        "tie_start": nd.get("tie_start", False),
                        "tie_end":   nd.get("tie_end",   False),
                    })
                    deletes.append(delete)

                # 음표 추가 행
                st.markdown("---")
                st.caption("➕ 새 음표 추가 (박자 > 0이면 저장 시 추가)")
                ac = st.columns([1.2, 2.0, 2.0, 0.7, 0.7])
                a_beat  = ac[0].number_input("박자",  value=0.0, min_value=0.0, step=0.25,
                                              key=f"ab_{pid}_{m}",
                                              label_visibility="collapsed")
                a_pitch = ac[1].text_input("음높이", value="", placeholder="예: G4, rest",
                                            key=f"ap_{pid}_{m}",
                                            label_visibility="collapsed")
                a_dur   = ac[2].selectbox("음길이", DURATIONS, index=2,
                                           key=f"ad_{pid}_{m}",
                                           label_visibility="collapsed")
                a_dots  = ac[3].number_input("점",  value=0, min_value=0, max_value=2,
                                              key=f"adt_{pid}_{m}",
                                              label_visibility="collapsed")
                a_voice = ac[4].number_input("성부", value=1, min_value=1, max_value=4,
                                              key=f"av_{pid}_{m}",
                                              label_visibility="collapsed")

                submitted = st.form_submit_button("💾 저장", use_container_width=True)

            if submitted:
                result = [r for r, d in zip(rows_new, deletes) if not d]
                if a_beat > 0 and a_pitch.strip():
                    result.append({
                        "beat": a_beat, "pitch": a_pitch.strip(),
                        "duration": a_dur, "dots": a_dots, "voice": a_voice,
                        "tie_start": False, "tie_end": False,
                    })
                result.sort(key=lambda x: (x["beat"], x["voice"]))
                note_corr[f"{pid}-{m}"] = result
                save_corrections(corrections)
                st.success(f"저장 완료 ({len(result)}개 음표)")
                st.cache_data.clear()
                st.rerun()

# ── 선택 마디 상세 패널 ───────────────────────────────────────────────────────

def show_detail_panel(
    m: int,
    layout: ScoreLayout,
    raw_notes: list[RawNote],
    validated_chords: list[ValidatedChord],
    rule4_flags: set[tuple[str, int]],
    conf_map: dict[int, float],
    flag_map: dict[int, list[str]],
    corrections: dict,
) -> None:
    sys = next((s for s in layout.systems
                if s.start_measure <= m <= s.end_measure), None)
    if sys is None:
        return

    conf  = conf_map.get(m, 1.0)
    flags = flag_map.get(m, [])
    badge = _conf_badge(conf)

    st.markdown(f"## {badge} 마디 {m} &nbsp; `{sys.key}` &nbsp; `{sys.time_signature}`")

    for f in flags:
        st.warning(f, icon="⚠️")

    col_img, col_edit = st.columns([1, 2], gap="large")

    with col_img:
        page_img = load_page_img(sys.page)
        crop     = render_measure_crop(page_img, sys, m, conf)
        # 크롭이 너무 가늘면 높이 확장해서 표시
        if crop.height < 80:
            scale = max(1, 80 // crop.height)
            crop  = crop.resize((crop.width * scale, crop.height * scale), Image.NEAREST)
        st.image(crop, caption=f"m{m} 크롭 (p{sys.page})", use_container_width=True)

        # 신뢰도 게이지
        pct = int(conf * 100)
        color = "#e55" if conf < 0.5 else ("#fa0" if conf < 0.75 else "#4c4")
        st.markdown(
            f"<div style='background:#ddd;border-radius:4px;height:10px;margin-top:6px'>"
            f"<div style='background:{color};width:{pct}%;height:10px;border-radius:4px'></div>"
            f"</div><p style='font-size:12px;color:#888;margin:2px 0'>신뢰도 {pct}%</p>",
            unsafe_allow_html=True,
        )

    with col_edit:
        show_chord_section(m, validated_chords, corrections)
        st.divider()
        show_note_section(m, layout, raw_notes, rule4_flags, corrections)

# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="악보 검수", layout="wide", initial_sidebar_state="expanded")

    if not LAYOUT_PATH.exists():
        st.error("output/ 폴더에 pass1_layout.json이 없습니다. 파이프라인을 먼저 실행하세요.")
        return

    layout, validated, raw_notes, raw_lyrics, anomalies = load_all()
    rule4_flags = compute_rule4_flags(raw_notes, layout)
    corrections  = load_corrections()

    # 마디별 신뢰도 사전 계산
    conf_map: dict[int, float]       = {}
    flag_map: dict[int, list[str]]   = {}
    for m in range(1, layout.total_measures + 1):
        c, f          = measure_confidence(m, raw_notes, validated, rule4_flags, anomalies)
        conf_map[m]   = c
        flag_map[m]   = f

    n_bad  = sum(1 for c in conf_map.values() if c < 0.50)
    n_warn = sum(1 for c in conf_map.values() if 0.50 <= c < 0.75)
    n_ok   = sum(1 for c in conf_map.values() if c >= 0.75)

    # ── 사이드바 ────────────────────────────────────────────────────────────
    with st.sidebar:
        st.title("🎵 악보 검수")
        st.divider()

        col1, col2, col3 = st.columns(3)
        col1.metric("🔴", n_bad,  "검수 필요")
        col2.metric("🟡", n_warn, "주의")
        col3.metric("🟢", n_ok,   "정상")
        st.caption(f"전체 {layout.total_measures}마디")

        st.divider()

        view_mode = st.radio(
            "마디 필터",
            ["🔴 검수 필요만", "🔴+🟡 주의 이상", "전체"],
            index=1,
        )

        st.divider()

        if st.button("🔨 MusicXML 재생성", type="primary", use_container_width=True):
            with st.spinner("MusicXML 빌드 중..."):
                out = rebuild_musicxml(corrections)
            st.success(f"저장됨: {out}")
            st.cache_data.clear()

        n_note_corr  = sum(len(v) for v in corrections.get("notes", {}).values())
        n_chord_corr = len(corrections.get("chords", {}))
        if n_note_corr or n_chord_corr:
            st.divider()
            st.caption(f"저장된 수정: 음표 {n_note_corr}개 · 코드 {n_chord_corr}개")
            if st.button("↩️ 모든 수정 초기화", use_container_width=True):
                save_corrections({"chords": {}, "notes": {}})
                st.cache_data.clear()
                st.rerun()

        st.divider()
        st.caption("**색상 범례**")
        st.markdown(
            "🔴 `conf < 50%` 검수 필요  \n"
            "🟡 `50–75%` 주의  \n"
            "🟢 `≥ 75%` 정상"
        )

    # ── 선택 마디 상세 (상단 고정) ──────────────────────────────────────────
    selected_m = st.session_state.get("selected_measure")

    if selected_m is not None:
        show_detail_panel(
            selected_m, layout, raw_notes, validated,
            rule4_flags, conf_map, flag_map, corrections,
        )
        if st.button("✕ 닫기", key="close_detail"):
            st.session_state.pop("selected_measure", None)
            st.rerun()
        st.divider()

    # ── 악보 개요 (시스템별 형광팬 스트립) ──────────────────────────────────
    st.subheader("악보 개요 — 마디를 클릭해 편집")

    def _show_measure(conf: float) -> bool:
        if view_mode.startswith("🔴 검수"):
            return conf < 0.50
        if view_mode.startswith("🔴+🟡"):
            return conf < 0.75
        return True

    for system in layout.systems:
        page_img  = load_page_img(system.page)
        strip_img = render_system_strip(page_img, system, conf_map)

        st.image(
            strip_img,
            caption=f"p{system.page}  |  m{system.start_measure}–{system.end_measure}"
                    f"  |  {system.key}  {system.time_signature}",
            use_container_width=True,
        )

        # 마디 버튼 행
        n_m   = system.end_measure - system.start_measure + 1
        cols  = st.columns(n_m)
        for i, m in enumerate(range(system.start_measure, system.end_measure + 1)):
            conf  = conf_map.get(m, 1.0)
            badge = _conf_badge(conf)

            if not _show_measure(conf):
                # 필터 밖 마디: 번호만 표시 (회색)
                cols[i].markdown(
                    f"<p style='text-align:center;color:#aaa;font-size:11px;"
                    f"margin:0;padding:0'>m{m}</p>",
                    unsafe_allow_html=True,
                )
                continue

            is_selected = (selected_m == m)
            label = f"{badge} {m}" + (" ◀" if is_selected else "")
            if cols[i].button(label, key=f"mbtn_{m}", use_container_width=True):
                if is_selected:
                    st.session_state.pop("selected_measure", None)
                else:
                    st.session_state["selected_measure"] = m
                st.rerun()

        st.markdown("")   # 시스템 간 간격


if __name__ == "__main__":
    main()
