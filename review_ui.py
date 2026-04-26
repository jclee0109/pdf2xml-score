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

    m_notes = [n for n in raw_notes if n.measure == m and n.pitch != "rest"]
    if m_notes:
        mn = min(n.confidence for n in m_notes)
        if mn < 0.70:
            conf = min(conf, mn)
            flags.append(f"음표 신뢰도 {mn:.0%}")

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

# ── 생성본 오선보 렌더링 ──────────────────────────────────────────────────────

_STEP_NUM = {'C': 0, 'D': 1, 'E': 2, 'F': 3, 'G': 4, 'A': 5, 'B': 6}

# 클레프별 하단 기준 음표 (= 오선 1번 선)
_CLEF_BASE: dict[str, int] = {
    "treble": 4 * 7 + 2,   # E4 절대 다이아토닉 위치
    "bass":   2 * 7 + 4,   # G2
    "alto":   3 * 7 + 2,   # E3
    "tenor":  3 * 7 + 4,   # G3
}

def _pitch_staff_pos(pitch: str, clef: str = "treble") -> float | None:
    """pitch ('G4', 'F#5', 'rest') → 오선 위치 (0=하단선, 4=상단선, 소수=공간)."""
    if pitch == "rest" or not pitch:
        return None
    step = pitch[0].upper()
    rest = pitch[1:]
    if rest and rest[0] in ('#', 'b'):
        rest = rest[1:]
    try:
        octave = int(rest)
    except ValueError:
        return None
    abs_d = octave * 7 + _STEP_NUM.get(step, 0)
    base  = _CLEF_BASE.get(clef, _CLEF_BASE["treble"])
    return (abs_d - base) * 0.5


def _note_color(conf: float) -> tuple[int, int, int]:
    if conf < 0.50: return (210, 50, 50)
    if conf < 0.75: return (210, 160, 0)
    return (50, 170, 80)


def render_extracted_notation(
    m: int,
    raw_notes: list[RawNote],
    layout: ScoreLayout,
    validated_chords: list[ValidatedChord],
    time_sig: str,
    anomalies: dict[tuple[str, int], list[str]],
    selected_key: str | None = None,
    width: int = 860,
) -> Image.Image:
    """마디 m의 추출된 음표를 오선보로 렌더링.

    - 음표 색상: 🔴 conf<0.5 / 🟡 0.5~0.75 / 🟢 ≥0.75
    - 선택된 음표(selected_key = "partid:beatidx")는 파란 테두리로 강조
    - 플래그된 음표에 ✕ 마크
    - 파트별 오선 스택, 코드 심볼 상단 표시
    """
    # 이 마디의 파트 목록 (음표 있는 것만)
    m_notes = [n for n in raw_notes if n.measure == m and n.pitch != "rest"]
    parts_with_notes = sorted({n.part_id for n in m_notes})

    # 코드 심볼
    chord_texts = [vc.chord_text for vc in validated_chords if vc.measure == m]
    chord_label = " / ".join(chord_texts) if chord_texts else ""

    # 이상 플래그 (파트 무관 메시지)
    all_anom_msgs: list[str] = []
    for (pid, mm), msgs in anomalies.items():
        if mm == m:
            all_anom_msgs.extend(msgs)

    # ── 레이아웃 상수 (크고 선명하게) ────────────────────────────────────────
    MARGIN_L   = 70
    MARGIN_R   = 24
    LS         = 16          # line spacing (px) — 이전보다 크게
    STAFF_H    = 4 * LS      # 5선 높이
    NOTE_RX    = 7
    NOTE_RY    = 5
    PART_GAP   = 40
    CHORD_H    = 28
    LABEL_H    = 20          # 피치 레이블 영역
    FLAG_H     = max(0, len(all_anom_msgs) * 18 + 10)

    n_staves = max(len(parts_with_notes), 1)
    total_h  = CHORD_H + n_staves * (STAFF_H + PART_GAP + LABEL_H) + FLAG_H + 20

    img  = Image.new("RGB", (width, total_h), (252, 252, 252))
    draw = ImageDraw.Draw(img)

    # ── 코드 심볼 ─────────────────────────────────────────────────────────────
    if chord_label:
        draw.text((MARGIN_L, 6), chord_label, fill=(40, 40, 200))

    # ── 박자 → x 변환 ─────────────────────────────────────────────────────────
    beats_num, beat_type = time_sig.split("/")
    beats_per_m = int(beats_num) * 4.0 / int(beat_type)
    staff_w = width - MARGIN_L - MARGIN_R

    def beat_x(beat: float) -> int:
        ratio = max(0.0, min(1.0, (beat - 1.0) / beats_per_m))
        return int(MARGIN_L + 20 + ratio * (staff_w - 30))

    # ── 파트별 오선 그리기 ─────────────────────────────────────────────────────
    for s_idx, pid in enumerate(parts_with_notes):
        part      = layout.parts[int(pid[1:])]
        clef      = part.clef
        staff_top = CHORD_H + s_idx * (STAFF_H + PART_GAP + LABEL_H)
        staff_bot = staff_top + STAFF_H

        # 파트명
        draw.text((2, staff_top + LS), part.name[:14], fill=(100, 100, 100))

        # 배경 밴드 (파트 구분)
        draw.rectangle(
            [(0, staff_top - 6), (width, staff_bot + LABEL_H + PART_GAP // 2)],
            fill=(248, 248, 255),
        )

        # 오선 5개
        for li in range(5):
            ly = staff_bot - li * LS
            lw = 1 if li != 2 else 2   # 중간선 두껍게
            draw.line([(MARGIN_L, ly), (width - MARGIN_R, ly)], fill=(160, 160, 170), width=lw)

        # 세로 바라인 (시작 · 끝)
        draw.line([(MARGIN_L, staff_top), (MARGIN_L, staff_bot)], fill=(80, 80, 80), width=2)
        draw.line([(width - MARGIN_R, staff_top), (width - MARGIN_R, staff_bot)], fill=(80, 80, 80), width=1)

        # 박자 그리드 (반투명 수직선)
        half_beat = beats_per_m / 2
        for b_frac in range(1, int(beats_per_m * 2)):
            bx = beat_x(1.0 + b_frac * 0.5)
            lc = (200, 200, 220) if b_frac % 2 == 0 else (220, 220, 235)
            draw.line([(bx, staff_top), (bx, staff_bot)], fill=lc, width=1)

        # 이 파트의 이마디 음표
        part_notes = [n for n in m_notes if n.part_id == pid]

        # beat별 묶기 (화음)
        by_beat: dict[float, list[RawNote]] = {}
        for n in part_notes:
            by_beat.setdefault(round(n.beat, 3), []).append(n)

        for b_idx, (beat, chord_notes) in enumerate(sorted(by_beat.items())):
            x = beat_x(beat)

            # 선택된 음표 하이라이트 (배경 박스)
            note_key = f"{pid}:{b_idx}"
            is_selected = (selected_key == note_key)
            if is_selected:
                draw.rectangle(
                    [(x - 14, staff_top - 2), (x + 14, staff_bot + 2)],
                    fill=(220, 235, 255), outline=(60, 120, 220), width=2,
                )

            for n in chord_notes:
                pos = _pitch_staff_pos(n.pitch, clef)
                if pos is None:
                    continue

                y = staff_bot - int(pos * LS)

                # 올려 긋기선
                for lp in range(-2, int(pos * 2) - 1, -2):
                    ly = staff_bot - int(lp / 2 * LS)
                    draw.line([(x - 11, ly), (x + 11, ly)], fill=(140, 140, 150), width=1)
                for lp in range(10, int(pos * 2) + 2, 2):
                    ly = staff_bot - int(lp / 2 * LS)
                    draw.line([(x - 11, ly), (x + 11, ly)], fill=(140, 140, 150), width=1)

                # 음표 머리
                color = _note_color(n.confidence)
                outline = (60, 120, 220) if is_selected else (30, 30, 30)
                draw.ellipse(
                    [(x - NOTE_RX, y - NOTE_RY), (x + NOTE_RX, y + NOTE_RY)],
                    fill=color, outline=outline, width=2 if is_selected else 1,
                )

                # 이상 플래그 ✕
                if (pid, m) in anomalies:
                    draw.line([(x - 8, y - 8), (x + 8, y + 8)], fill=(180, 0, 0), width=2)
                    draw.line([(x + 8, y - 8), (x - 8, y + 8)], fill=(180, 0, 0), width=2)

            # 음표 레이블 (오선 아래)
            label_y = staff_bot + 4
            label_parts = []
            for n in chord_notes:
                label_parts.append(n.pitch)
            draw.text((x - 8, label_y), ",".join(label_parts[:2]), fill=(80, 80, 100))

        # 쉼표 마디
        if not part_notes:
            mid_y = (staff_top + staff_bot) // 2
            draw.rectangle(
                [(width // 2 - 18, mid_y - 5), (width // 2 + 18, mid_y + 3)],
                fill=(190, 190, 200),
            )
            draw.text((width // 2 - 12, mid_y + 6), "쉼표", fill=(150, 150, 160))

    # ── 플래그 설명 ──────────────────────────────────────────────────────────
    flag_y = CHORD_H + n_staves * (STAFF_H + PART_GAP + LABEL_H) + 4
    for msg in all_anom_msgs:
        fc = (180, 0, 0) if "Rule 7" in msg else (150, 90, 0)
        draw.text((MARGIN_L, flag_y), f"⚠ {msg}", fill=fc)
        flag_y += 18

    # ── 신뢰도 범례 (우하단) ─────────────────────────────────────────────────
    lx, ly_leg = width - 145, total_h - 18
    for lbl, lc in [("낮음", (210,50,50)), ("중간", (210,160,0)), ("높음", (50,170,80))]:
        draw.ellipse([(lx, ly_leg+1), (lx+10, ly_leg+11)], fill=lc)
        draw.text((lx + 13, ly_leg), lbl, fill=(110, 110, 110))
        lx += 50

    return img


# ── 시스템 전체 오선보 렌더링 ────────────────────────────────────────────────────

def render_system_notation(
    system: SystemInfo,
    raw_notes: list[RawNote],
    layout: ScoreLayout,
    validated_chords: list[ValidatedChord],
    anomalies: dict[tuple[str, int], list[str]],
    conf_map: dict[int, float],
    selected_m: int | None = None,
    width: int = 1100,
) -> Image.Image:
    """시스템(한 줄 = 여러 마디) 전체를 하나의 오선보 이미지로 렌더링.

    - 원본과 같은 행 단위 레이아웃
    - 음표 음역에 따라 오선 위/아래 공간 동적 확장 (overflow 방지)
    - 마디별 confidence 배경 tint
    - 선택 마디: 파란 외곽선 강조
    """
    measures  = list(range(system.start_measure, system.end_measure + 1))
    n_m       = len(measures)
    sys_notes = [n for n in raw_notes
                 if system.start_measure <= n.measure <= system.end_measure]

    # 음표 있는 파트만
    parts_in_sys = sorted({n.part_id for n in sys_notes if n.pitch != "rest"})

    # ── 상수 ────────────────────────────────────────────────────────────────
    ML   = 82   # left margin (part name + clef)
    MR   = 8    # right margin
    LS   = 12   # line spacing (px per staff position step)
    MNUM_H = 18  # measure number header height
    PAD  = 6    # vertical padding between stave area and boundary

    measure_w = (width - ML - MR) / max(n_m, 1)

    # ── 파트별 음역 계산 → 동적 높이 ────────────────────────────────────────
    part_info: list[dict] = []
    for pid in parts_in_sys:
        part  = layout.parts[int(pid[1:])]
        clef  = part.clef
        p_notes = [n for n in sys_notes if n.part_id == pid and n.pitch != "rest"]
        poss = [_pitch_staff_pos(n.pitch, clef) for n in p_notes]
        poss = [p for p in poss if p is not None]

        if poss:
            low  = min(poss)
            high = max(poss)
        else:
            low, high = 0.0, 4.0

        # 오선 영역(0~4) + 음표 범위에 맞춰 여백 추가 (최대 4칸 = 4개 올림줄)
        below = max(0.0, -low + 0.5)      # 오선 아래 추가 (clamp 4)
        above = max(0.0, high - 4 + 0.5)  # 오선 위 추가
        below = min(below, 4.0)
        above = min(above, 4.0)

        stave_h = int((4 + below + above) * LS) + PAD * 2
        part_info.append({
            "pid": pid, "clef": clef, "name": part.name,
            "below": below, "above": above, "stave_h": stave_h,
        })

    PART_GAP = 14
    total_h  = MNUM_H + sum(pi["stave_h"] + PART_GAP for pi in part_info) + 10

    img  = Image.new("RGB", (width, total_h), (253, 253, 253))
    draw = ImageDraw.Draw(img)

    # ── 마디별 배경 tint + 마디 번호 ─────────────────────────────────────────
    for i, m in enumerate(measures):
        x1 = ML + i * measure_w
        x2 = x1 + measure_w - 1
        conf = conf_map.get(m, 1.0)

        # 매우 연한 confidence tint
        r, g, b, _ = _conf_rgba(conf)
        tint = (int(r * 0.08 + 253 * 0.92),
                int(g * 0.08 + 253 * 0.92),
                int(b * 0.08 + 253 * 0.92))
        draw.rectangle([(x1, 0), (x2, total_h)], fill=tint)

        # 선택 마디: 파란 테두리
        if m == selected_m:
            draw.rectangle([(x1 + 1, 1), (x2 - 1, total_h - 2)],
                           outline=(60, 120, 220), width=2)

        # 마디 번호
        m_label = f"m{m}"
        badge   = _conf_badge(conf)
        draw.text((int(x1 + 4), 3), f"{badge}{m_label}", fill=(90, 90, 110))

    # 왼쪽 경계선
    draw.rectangle([(0, 0), (ML - 1, total_h)], fill=(245, 245, 248))

    # ── 파트별 오선 + 음표 ────────────────────────────────────────────────────
    y_cur = MNUM_H
    for pi in part_info:
        pid   = pi["pid"]
        clef  = pi["clef"]
        below = pi["below"]
        above = pi["above"]
        stave_h = pi["stave_h"]

        # 오선 1번 선(하단) y 좌표
        staff_bot = y_cur + PAD + int((below) * LS) + 4 * LS
        staff_top = staff_bot - 4 * LS

        # 파트명
        draw.text((2, staff_bot - 2 * LS), pi["name"][:13], fill=(100, 100, 100))

        # 오선 5개
        for li in range(5):
            ly = staff_bot - li * LS
            lw = 1 if li != 2 else 2
            draw.line([(ML, ly), (width - MR, ly)], fill=(155, 155, 165), width=lw)

        # 좌측 바라인
        draw.line([(ML, staff_top), (ML, staff_bot)], fill=(60, 60, 60), width=2)

        # 마디 바라인 (오른쪽)
        for i in range(n_m):
            bx = int(ML + (i + 1) * measure_w)
            draw.line([(bx, staff_top), (bx, staff_bot)], fill=(120, 120, 120), width=1)

        # ── 음표 그리기 ────────────────────────────────────────────────────
        p_notes = [n for n in sys_notes if n.part_id == pid]
        by_m_beat: dict[tuple[int, float], list[RawNote]] = {}
        for n in p_notes:
            key = (n.measure, round(n.beat, 3))
            by_m_beat.setdefault(key, []).append(n)

        beats_num, beat_type = system.time_signature.split("/")
        beats_per_m = int(beats_num) * 4.0 / int(beat_type)

        for (m_num, beat), chord_notes in sorted(by_m_beat.items()):
            m_idx = m_num - system.start_measure
            x_m_start = ML + m_idx * measure_w
            beat_ratio = max(0.0, min(1.0, (beat - 1.0) / beats_per_m))
            x = int(x_m_start + 8 + beat_ratio * (measure_w - 14))

            for n in chord_notes:
                if n.pitch == "rest":
                    # 쉼표: 가는 사각형
                    draw.rectangle(
                        [(x - 6, staff_bot - 2 * LS - 2),
                         (x + 6, staff_bot - 2 * LS + 2)],
                        fill=(180, 180, 190),
                    )
                    continue

                pos = _pitch_staff_pos(n.pitch, clef)
                if pos is None:
                    continue

                # 오선 범위 초과 시 클리핑 표시 (화살표)
                clamped = False
                if pos < -below - 0.5:
                    pos = -below - 0.1
                    clamped = True
                elif pos > 4 + above + 0.5:
                    pos = 4 + above + 0.1
                    clamped = True

                y = int(staff_bot - pos * LS)
                color = _note_color(n.confidence)

                # 올려 긋기선
                for lp in range(-2, int(pos * 2) - 1, -2):
                    if lp < -int(below * 2 + 0.5):
                        break
                    ly = staff_bot - int(lp / 2 * LS)
                    draw.line([(x - 9, ly), (x + 9, ly)], fill=(150, 150, 155), width=1)
                for lp in range(10, int(pos * 2) + 2, 2):
                    if lp > int((4 + above) * 2 + 0.5):
                        break
                    ly = staff_bot - int(lp / 2 * LS)
                    draw.line([(x - 9, ly), (x + 9, ly)], fill=(150, 150, 155), width=1)

                NRX, NRY = 6, 4
                if clamped:
                    draw.polygon([(x, y - 7), (x - 5, y + 1), (x + 5, y + 1)],
                                 fill=color, outline=(80, 80, 80))
                else:
                    draw.ellipse([(x - NRX, y - NRY), (x + NRX, y + NRY)],
                                 fill=color, outline=(30, 30, 30), width=1)

                # 이상 플래그 ✕
                if (pid, m_num) in anomalies:
                    draw.line([(x - 7, y - 7), (x + 7, y + 7)], fill=(180, 0, 0), width=2)
                    draw.line([(x + 7, y - 7), (x - 7, y + 7)], fill=(180, 0, 0), width=2)

        y_cur += stave_h + PART_GAP

    return img


# ── 원본 이미지 렌더링 ─────────────────────────────────────────────────────────

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

_DUR_SYM = {
    "whole": "𝅝", "half": "♩", "quarter": "♩", "eighth": "♪",
    "16th": "𝅘𝅥𝅮", "32nd": "𝅘𝅥𝅯",
}


def show_detail_panel(
    m: int,
    layout: ScoreLayout,
    raw_notes: list[RawNote],
    validated_chords: list[ValidatedChord],
    rule4_flags: set[tuple[str, int]],
    conf_map: dict[int, float],
    flag_map: dict[int, list[str]],
    corrections: dict,
    anomalies: dict[tuple[str, int], list[str]],
) -> None:
    sys_info = next((s for s in layout.systems
                     if s.start_measure <= m <= s.end_measure), None)
    if sys_info is None:
        return

    conf  = conf_map.get(m, 1.0)
    flags = flag_map.get(m, [])
    badge = _conf_badge(conf)

    # 선택된 음표 state 키
    sel_key_state = f"sel_note_{m}"
    selected_key  = st.session_state.get(sel_key_state)

    # ── 헤더 ──────────────────────────────────────────────────────────────────
    hcol1, hcol2 = st.columns([3, 1])
    hcol1.markdown(f"## {badge} 마디 {m} &nbsp; `{sys_info.key}` &nbsp; `{sys_info.time_signature}`")
    pct   = int(conf * 100)
    gc    = "#e55" if conf < 0.5 else ("#fa0" if conf < 0.75 else "#4c4")
    hcol2.markdown(
        f"<div style='margin-top:18px'>"
        f"<div style='background:#ddd;border-radius:4px;height:10px'>"
        f"<div style='background:{gc};width:{pct}%;height:10px;border-radius:4px'></div>"
        f"</div><p style='font-size:11px;color:#888;margin:2px 0 0'>신뢰도 {pct}%</p></div>",
        unsafe_allow_html=True,
    )

    # 플래그
    for f in flags:
        st.warning(f, icon="⚠️")

    # ── 코드 심볼 인라인 편집 ─────────────────────────────────────────────────
    m_chords = [vc for vc in validated_chords if vc.measure == m]
    chord_corr = corrections.setdefault("chords", {})
    if m_chords:
        c_cols = st.columns([1, 2, 2, 1])
        c_cols[0].markdown("**코드**")
        vc = m_chords[0]
        c_cols[1].markdown(
            f"`{vc.chord_text}` {_conf_badge(vc.confidence)} "
            f"{'⚠️' if vc.needs_review else ''}"
        )
        new_chord = c_cols[2].text_input(
            "수정", value=chord_corr.get(str(m), ""),
            placeholder=vc.chord_text, label_visibility="collapsed",
            key=f"chord_inline_{m}",
        )
        if c_cols[3].button("저장", key=f"chord_save_inline_{m}"):
            if new_chord.strip():
                chord_corr[str(m)] = new_chord.strip()
            else:
                chord_corr.pop(str(m), None)
            save_corrections(corrections)
            st.rerun()

    # ── 오선지 (메인 뷰) ──────────────────────────────────────────────────────
    notation = render_extracted_notation(
        m, raw_notes, layout, validated_chords,
        sys_info.time_signature, anomalies,
        selected_key=selected_key,
        width=860,
    )
    st.image(notation, use_container_width=True)
    st.caption("🔴 낮은 confidence  🟡 중간  🟢 높음  &nbsp;|&nbsp; ✕ 이상 탐지된 음표")

    # ── 음표 칩 (파트별, 클릭 → 인라인 편집) ─────────────────────────────────
    note_corr = corrections.setdefault("notes", {})
    parts_in_m = sorted({n.part_id for n in raw_notes if n.measure == m})

    for pid in parts_in_m:
        part_name = layout.parts[int(pid[1:])].name
        part_notes_raw = [n for n in raw_notes if n.part_id == pid and n.measure == m]

        # corrections가 있으면 그걸 표시, 없으면 원본
        corr_key = f"{pid}-{m}"
        if corr_key in note_corr:
            display_notes = note_corr[corr_key]   # list of dict
            is_corrected  = True
        else:
            display_notes = [
                {"beat": n.beat, "pitch": n.pitch, "duration": n.duration,
                 "dots": n.dots, "voice": n.voice,
                 "tie_start": n.tie_start, "tie_end": n.tie_end,
                 "confidence": n.confidence}
                for n in sorted(part_notes_raw, key=lambda n: (n.beat, n.voice))
            ]
            is_corrected = False

        corrected_label = " ✏️" if is_corrected else ""
        st.markdown(f"**{part_name}**{corrected_label}")

        if not display_notes:
            st.caption("  (쉼표)")
            continue

        # 음표 칩 행 + [+추가] 버튼
        n_chips = len(display_notes) + 1
        chip_cols = st.columns(n_chips)

        for b_idx, nd in enumerate(display_notes):
            badge_n = _conf_badge(nd.get("confidence", 0.5))
            dur_sym = _DUR_SYM.get(nd["duration"], "♩")
            dot_str = "." * nd.get("dots", 0)
            label   = f"{badge_n} {nd['pitch']}\n{dur_sym}{dot_str}"
            chip_key = f"{pid}:{b_idx}"
            is_sel   = (selected_key == chip_key)

            with chip_cols[b_idx]:
                btn_type = "primary" if is_sel else "secondary"
                if st.button(label, key=f"chip_{m}_{pid}_{b_idx}",
                             type=btn_type, use_container_width=True):
                    if is_sel:
                        st.session_state.pop(sel_key_state, None)
                    else:
                        st.session_state[sel_key_state] = chip_key
                    st.rerun()

        # [+추가] 버튼
        with chip_cols[-1]:
            if st.button("＋", key=f"add_{m}_{pid}", use_container_width=True):
                st.session_state[sel_key_state] = f"{pid}:new"
                st.rerun()

        # ── 선택된 음표 인라인 편집 폼 ───────────────────────────────────────
        if selected_key and selected_key.startswith(f"{pid}:"):
            suffix = selected_key.split(":", 1)[1]
            is_new = (suffix == "new")
            edit_idx = None if is_new else int(suffix)

            with st.container():
                st.markdown(
                    f"<div style='background:#eef3ff;border-left:3px solid #4488ee;"
                    f"padding:8px 12px;margin:4px 0;border-radius:4px'>",
                    unsafe_allow_html=True,
                )

                if is_new:
                    nd = {"beat": 1.0, "pitch": "", "duration": "quarter",
                          "dots": 0, "voice": 1}
                else:
                    nd = display_notes[edit_idx]

                with st.form(key=f"edit_{m}_{pid}_{suffix}"):
                    ec = st.columns([1.2, 2, 2, 0.7, 0.7, 1, 1])
                    beat  = ec[0].number_input("박자", value=float(nd["beat"]),
                                               min_value=0.0, step=0.25,
                                               label_visibility="collapsed")
                    pitch = ec[1].text_input("음높이", value=nd["pitch"],
                                             placeholder="G4, F#5, rest…",
                                             label_visibility="collapsed")
                    dur_i = DURATIONS.index(nd["duration"]) if nd["duration"] in DURATIONS else 2
                    dur   = ec[2].selectbox("음길이", DURATIONS, index=dur_i,
                                            label_visibility="collapsed")
                    dots  = ec[3].number_input("점", value=int(nd.get("dots", 0)),
                                               min_value=0, max_value=2,
                                               label_visibility="collapsed")
                    voice = ec[4].number_input("성부", value=int(nd.get("voice", 1)),
                                               min_value=1, max_value=4,
                                               label_visibility="collapsed")
                    save_btn   = ec[5].form_submit_button("💾 저장", use_container_width=True)
                    delete_btn = ec[6].form_submit_button("🗑️ 삭제", use_container_width=True)

                if save_btn:
                    new_nd = {"beat": beat, "pitch": pitch.strip(),
                              "duration": dur, "dots": dots, "voice": voice,
                              "tie_start": False, "tie_end": False}
                    updated = list(display_notes)
                    if is_new:
                        updated.append(new_nd)
                    else:
                        updated[edit_idx] = new_nd
                    updated.sort(key=lambda x: (x["beat"], x["voice"]))
                    note_corr[corr_key] = updated
                    save_corrections(corrections)
                    st.session_state.pop(sel_key_state, None)
                    st.cache_data.clear()
                    st.rerun()

                if delete_btn and not is_new:
                    updated = [n for i, n in enumerate(display_notes) if i != edit_idx]
                    note_corr[corr_key] = updated
                    save_corrections(corrections)
                    st.session_state.pop(sel_key_state, None)
                    st.cache_data.clear()
                    st.rerun()

                st.markdown("</div>", unsafe_allow_html=True)

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
            anomalies,
        )
        if st.button("✕ 닫기", key="close_detail"):
            st.session_state.pop("selected_measure", None)
            st.rerun()
        st.divider()

    # ── 마디 선택 (시스템별 버튼 그리드) ────────────────────────────────────
    st.subheader("마디 선택")

    def _show_measure(conf: float) -> bool:
        if view_mode.startswith("🔴 검수"):
            return conf < 0.50
        if view_mode.startswith("🔴+🟡"):
            return conf < 0.75
        return True

    for system in layout.systems:
        st.caption(
            f"p{system.page}  ·  m{system.start_measure}–{system.end_measure}"
            f"  ·  {system.key}  {system.time_signature}"
        )
        n_m  = system.end_measure - system.start_measure + 1
        cols = st.columns(n_m)

        for i, m in enumerate(range(system.start_measure, system.end_measure + 1)):
            conf  = conf_map.get(m, 1.0)
            badge = _conf_badge(conf)

            if not _show_measure(conf):
                cols[i].markdown(
                    f"<p style='text-align:center;color:#ccc;font-size:11px;"
                    f"margin:2px 0'>m{m}</p>",
                    unsafe_allow_html=True,
                )
                continue

            is_selected = (selected_m == m)
            label = f"{badge}{m}" + (" ◀" if is_selected else "")
            btn_type = "primary" if is_selected else "secondary"
            if cols[i].button(label, key=f"mbtn_{m}",
                              type=btn_type, use_container_width=True):
                if is_selected:
                    st.session_state.pop("selected_measure", None)
                else:
                    st.session_state["selected_measure"] = m
                st.rerun()

        st.markdown("")


if __name__ == "__main__":
    main()
