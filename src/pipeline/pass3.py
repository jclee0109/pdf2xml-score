"""Pass 3: 음악 이론 검증 — Rule 1~4"""
from __future__ import annotations
import logging
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
