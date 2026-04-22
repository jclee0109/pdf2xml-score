"""Pass 3: 음악 이론 검증 — Rule 1 (다이아토닉), Rule 2 (근음 도약)"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field

from ..models.score import RawChord, ScoreLayout, SystemInfo
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
    key = "C major"
    for sys in systems:
        if sys.start_measure <= measure <= sys.end_measure:
            return sys.key
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
