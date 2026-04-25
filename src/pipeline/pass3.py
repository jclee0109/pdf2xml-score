"""Pass 3: 음악 이론 검증 — Rule 1~6"""
from __future__ import annotations
import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from ..models.score import RawChord, RawNote, ScoreLayout, SystemInfo
from ..models.chord import ChordSymbol, parse_chord_text, diatonic_pcs

log = logging.getLogger(__name__)


@dataclass
class ValidatedChord:
    measure: int
    beat: float
    chord_text: str
    normalized: ChordSymbol | None
    confidence: float
    flags: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    needs_review: bool = False


def _get_key_at(measure: int, systems: list[SystemInfo]) -> str:
    """마디번호에 맞는 키 반환. 시스템 내 key_changes 지원."""
    key = "C major"
    for sys in systems:
        if sys.start_measure <= measure <= sys.end_measure:
            key = sys.key
            # 시스템 내 키 변화 적용
            for kc in sorted(sys.key_changes, key=lambda k: k.measure):
                if measure >= kc.measure:
                    key = kc.key
            return key
        if sys.start_measure <= measure:
            key = sys.key
    return key


def validate_chords(
    raw_chords: list[RawChord],
    layout: ScoreLayout,
) -> list[ValidatedChord]:
    validated: list[ValidatedChord] = []

    prev_pc: int | None = None

    for raw in sorted(raw_chords, key=lambda c: (c.measure, c.beat)):
        key = _get_key_at(raw.measure, layout.systems)
        parsed = parse_chord_text(raw.chord_text)
        conf = raw.confidence
        flags: list[str] = []

        if parsed is None:
            flags.append("parse_failed")
            validated.append(ValidatedChord(
                measure=raw.measure, beat=raw.beat,
                chord_text=raw.chord_text, normalized=None,
                confidence=0.0, flags=flags, needs_review=True,
            ))
            continue

        # Rule 1: 다이아토닉 분류
        dpcs = diatonic_pcs(key)
        root_pc = parsed.semitone
        if root_pc in dpcs:
            classification = "diatonic"
        else:
            # secondary dominant check: V of any diatonic degree?
            is_secondary = False
            for tonic_pc in dpcs:
                # V of tonic = (tonic - 7) % 12 = (tonic + 5) % 12
                if root_pc == (tonic_pc + 7) % 12:
                    is_secondary = True
                    break
            if is_secondary:
                classification = "secondary_dominant"
            else:
                classification = "chromatic"
                conf = min(conf, 0.65)
                flags.append("chromatic")
                log.debug(f"m{raw.measure}: {raw.chord_text} chromatic in {key}")

        # Rule 2: 근음 도약 검사
        if prev_pc is not None:
            leap = min((root_pc - prev_pc) % 12, (prev_pc - root_pc) % 12)
            if leap >= 8:
                flags.append("large_leap")
                conf = min(conf, 0.70)
                log.debug(f"m{raw.measure}: {raw.chord_text} large leap {leap}st from prev")
            elif leap >= 6:
                conf = min(conf, 0.85)

        prev_pc = root_pc

        # Rule 3: 저신뢰도 → needs_review (API 재평가는 생략)
        if conf < 0.70 or flags:
            flags.append("low_confidence") if conf < 0.70 and "low_confidence" not in flags else None
            needs_review = True
        else:
            needs_review = False

        validated.append(ValidatedChord(
            measure=raw.measure, beat=raw.beat,
            chord_text=raw.chord_text, normalized=parsed,
            confidence=conf, flags=flags, needs_review=needs_review,
        ))

    review_count = sum(1 for v in validated if v.needs_review)
    total = len(validated)
    log.info(f"Pass 3 완료: {total}개 코드, 검수 필요 {review_count}개 "
             f"({review_count/total*100:.0f}%)" if total else "Pass 3: 코드 없음")
    return validated


# ── Rule 4: 음표 duration 합산 검증 ────────────────────────────────────────────

DURATION_QUARTERS: dict[str, float] = {
    "whole": 4.0, "half": 2.0, "quarter": 1.0,
    "eighth": 0.5, "16th": 0.25, "32nd": 0.125,
}


def _time_sig_quarters(time_sig: str) -> float:
    """박자표 → 마디당 4분음표 수. 6/8 → 3.0, 4/4 → 4.0"""
    beats, beat_type = time_sig.split("/")
    return int(beats) * 4.0 / int(beat_type)


def validate_notes(
    raw_notes: list[RawNote],
    layout: ScoreLayout,
) -> list[RawNote]:
    """Rule 4: 마디 내 duration 합이 박자표와 맞지 않으면 경고 로그.
    음표 자체는 수정하지 않고 그대로 반환. 검수 플래그는 confidence < 0.7로 표시."""

    # (part_id, measure) → notes
    by_pm: dict[tuple[str, int], list[RawNote]] = {}
    for n in raw_notes:
        by_pm.setdefault((n.part_id, n.measure), []).append(n)

    flagged = 0
    for (part_id, measure), notes in by_pm.items():
        sys = next(
            (s for s in layout.systems if s.start_measure <= measure <= s.end_measure),
            None,
        )
        if sys is None:
            continue
        expected = _time_sig_quarters(sys.time_signature)

        # voice별 합산 (각 voice는 독립)
        by_voice: dict[int, list[RawNote]] = {}
        for n in notes:
            by_voice.setdefault(n.voice, []).append(n)

        for voice, v_notes in by_voice.items():
            # 전쉼표(whole rest)만 있는 마디 = 전마디 쉼표 기호, 박자표와 무관하게 정상
            if all(n.pitch == "rest" for n in v_notes):
                continue

            # chord 음표(같은 beat 중복)는 한 번만 합산
            seen_beats: dict[float, float] = {}
            for n in v_notes:
                dur = DURATION_QUARTERS.get(n.duration, 1.0) * (1 + sum(0.5**i for i in range(1, n.dots + 1)))
                if n.beat not in seen_beats or dur > seen_beats[n.beat]:
                    seen_beats[n.beat] = dur
            total = sum(seen_beats.values())

            if abs(total - expected) > 0.01:
                log.warning(
                    f"Rule 4: m{measure} {part_id} v{voice} — "
                    f"duration 합 {total:.3f} ≠ 박자 {expected:.3f} ({sys.time_signature})"
                )
                flagged += 1

    if flagged:
        log.info(f"Rule 4: {flagged}개 마디/성부에서 duration 불일치")
    else:
        log.info("Rule 4: 모든 마디 duration 정상")

    return raw_notes


# ── Rule 5~6: 음표 이상 탐지 ──────────────────────────────────────────────────

_PITCH_CLASS: dict[str, int] = {
    "C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11,
}

# 악기 음역 — written pitch (악보에 표기된 그대로) 기준 MIDI
# 이조 악기는 written pitch로 비교 (pipeline이 written pitch 저장)
_RANGE_MAP: list[tuple[tuple[str, ...], int, int]] = [
    (("piccolo",),                  62, 102),  # D4–F#7  (written, sounds 8va alta)
    (("flute",),                    60, 100),  # C4–E7
    (("oboe",),                     58,  93),  # Bb3–A6
    (("clarinet",),                 50,  94),  # D3–Bb6  (written, sounds major 2nd lower)
    (("bassoon",),                  34,  75),  # Bb1–Eb5
    (("horn",),                     35,  84),  # B1–C6   (written, sounds perfect 5th lower)
    (("trumpet",),                  52,  86),  # E3–D6   (written, sounds major 2nd lower)
    (("trombone", "tuba"),          28,  67),  # E1–G4
    (("timpani",),                  41,  65),  # F2–F4
    (("violin",),                   55, 100),  # G3–E7
    (("viola",),                    48,  91),  # C3–G6
    (("violoncello", "cello"),      36,  84),  # C2–C6
    (("contrabass", "bass"),        28,  72),  # E1–C5   (written, sounds 8va bassa)
    (("piano",),                    21, 108),  # A0–C8
]

_LEAP_SEMITONES = 13   # 단9도 이상이면 의심 (옥타브+반음)
_COUNT_HIGH_K   = 3.0  # 파트 중앙값 × 이 배수 이상이면 과다
_COUNT_LOW_K    = 0.25 # 파트 중앙값 × 이 배수 이하면 과소 (0은 제외)


def _to_midi(pitch: str) -> int | None:
    """'G4' → 67, 'F#5' → 78, 'Bb3' → 46, 'rest' → None."""
    if pitch == "rest" or not pitch:
        return None
    try:
        if len(pitch) >= 3 and pitch[1] in ("#", "b"):
            step, alter, octave = pitch[0], (1 if pitch[1] == "#" else -1), int(pitch[2:])
        else:
            step, alter, octave = pitch[0], 0, int(pitch[1:])
        return (octave + 1) * 12 + _PITCH_CLASS[step.upper()] + alter
    except (KeyError, ValueError):
        return None


def _part_range(part_name: str) -> tuple[int, int] | None:
    name_lower = part_name.lower()
    for keywords, lo, hi in _RANGE_MAP:
        if any(k in name_lower for k in keywords):
            return lo, hi
    return None


def check_note_anomalies(
    raw_notes: list[RawNote],
    layout: ScoreLayout,
) -> dict[tuple[str, int], list[str]]:
    """Rule 5~6: 음표 이상 탐지.

    Returns:
        {(part_id, measure): [anomaly_description, ...]}

    Rule 5 — 음역 도약 이상:
        같은 voice 연속 음표 사이 도약이 _LEAP_SEMITONES 반음 이상이면 플래그.
        단, 옥타브 유니즌(정확히 12반음 도약)은 의도적 옥타브 이동이 많으므로 제외.

    Rule 6 — 마디별 음표 수 이상치:
        파트별 마디당 음표 수의 중앙값을 구하고, 그 중앙값의 _COUNT_HIGH_K배 이상이거나
        _COUNT_LOW_K배 이하(단 0 제외)인 마디를 플래그.

    Rule 7 — 악기 음역 이탈:
        알려진 악기 음역(_RANGE_MAP)을 벗어난 음표가 있는 마디를 플래그.
    """
    anomalies: dict[tuple[str, int], list[str]] = defaultdict(list)

    # 파트 이름 조회
    part_name_map = {p.id: p.name for p in layout.parts}

    # (part_id, voice) → measure 순 음표 목록
    by_pv: dict[tuple[str, int], list[RawNote]] = defaultdict(list)
    for n in raw_notes:
        if n.pitch != "rest":
            by_pv[(n.part_id, n.voice)].append(n)

    for pv, notes in by_pv.items():
        pid, _ = pv
        notes_sorted = sorted(notes, key=lambda n: (n.measure, n.beat))

        # ── Rule 5: 도약 이상 ──────────────────────────────────────────────
        prev_midi: int | None = None
        prev_measure: int | None = None
        for n in notes_sorted:
            midi = _to_midi(n.pitch)
            if midi is None:
                continue
            if prev_midi is not None:
                leap = abs(midi - prev_midi)
                # 정확히 12반음(옥타브)은 의도적 이동으로 허용
                if leap >= _LEAP_SEMITONES and leap != 12:
                    key = (pid, n.measure)
                    msg = f"Rule 5: 도약 {leap}반음 ({notes_sorted[notes_sorted.index(n)-1].pitch}→{n.pitch})"
                    if msg not in anomalies[key]:
                        anomalies[key].append(msg)
                        log.debug(f"  {pid} m{n.measure}: {msg}")
            prev_midi = midi
            prev_measure = n.measure

    # ── Rule 6: 마디별 음표 수 이상치 ─────────────────────────────────────
    # 파트별 (measure → 음표 수) 집계 (쉼표 제외, chord 중복 포함)
    by_part_m: dict[str, dict[int, int]] = defaultdict(dict)
    for n in raw_notes:
        if n.pitch != "rest":
            by_part_m[n.part_id][n.measure] = \
                by_part_m[n.part_id].get(n.measure, 0) + 1

    for pid, m_counts in by_part_m.items():
        counts = list(m_counts.values())
        if len(counts) < 3:
            continue
        med = statistics.median(counts)
        if med == 0:
            continue
        for m, cnt in m_counts.items():
            if cnt > med * _COUNT_HIGH_K:
                msg = f"Rule 6: 음표 수 과다 ({cnt}개, 중앙값 {med:.0f})"
                anomalies[(pid, m)].append(msg)
                log.debug(f"  {pid} m{m}: {msg}")
            elif 0 < cnt < med * _COUNT_LOW_K:
                msg = f"Rule 6: 음표 수 과소 ({cnt}개, 중앙값 {med:.0f})"
                anomalies[(pid, m)].append(msg)
                log.debug(f"  {pid} m{m}: {msg}")

    # ── Rule 7: 악기 음역 이탈 ────────────────────────────────────────────
    for n in raw_notes:
        if n.pitch == "rest":
            continue
        midi = _to_midi(n.pitch)
        if midi is None:
            continue
        rng = _part_range(part_name_map.get(n.part_id, ""))
        if rng is None:
            continue
        lo, hi = rng
        if not (lo <= midi <= hi):
            msg = f"Rule 7: 음역 이탈 {n.pitch} (허용 {_to_pitch(lo)}–{_to_pitch(hi)})"
            key = (n.part_id, n.measure)
            if msg not in anomalies[key]:
                anomalies[key].append(msg)
                log.debug(f"  {n.part_id} m{n.measure}: {msg}")

    total = sum(len(v) for v in anomalies.values())
    log.info(f"Rule 5~7: {len(anomalies)}개 (part, measure)에서 {total}개 이상 감지")
    return dict(anomalies)


def _to_pitch(midi: int) -> str:
    """67 → 'G4'  (Rule 7 로그용)."""
    names = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
    octave = midi // 12 - 1
    name   = names[midi % 12]
    return f"{name}{octave}"
