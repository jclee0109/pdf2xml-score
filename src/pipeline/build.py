"""MusicXML 빌더 — ValidatedChord + RawNote + ScoreLayout → MusicXML 4.0"""
from __future__ import annotations
import logging
from lxml import etree

from ..models.score import ScoreLayout, SystemInfo, RawNote
from ..models.chord import ChordSymbol, KEY_FIFTHS
from .pass3 import ValidatedChord

log = logging.getLogger(__name__)

DIVISIONS = 4   # ticks per quarter note

CLEF_MAP = {
    "treble": ("G", "2"),
    "bass":   ("F", "4"),
    "alto":   ("C", "3"),
    "tenor":  ("C", "4"),
}

DURATION_TICKS: dict[str, int] = {
    "whole": 16, "half": 8, "quarter": 4,
    "eighth": 2, "16th": 1, "32nd": 1,
}


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _get_system_at(measure: int, systems: list[SystemInfo]) -> SystemInfo | None:
    for s in systems:
        if s.start_measure <= measure <= s.end_measure:
            return s
    return None


def _measure_total_ticks(time_sig: str) -> int:
    """박자표 → 마디 전체 ticks. 6/8 → 12, 4/4 → 16."""
    beats, beat_type = time_sig.split("/")
    return DIVISIONS * int(beats) * 4 // int(beat_type)


def _note_ticks(duration: str, dots: int) -> int:
    base = DURATION_TICKS.get(duration, 4)
    total, addon = base, base
    for _ in range(dots):
        addon = addon // 2
        total += addon
    return total


def _parse_pitch(pitch_str: str) -> tuple[str, int, int]:
    """'G4' → ('G', 0, 4), 'F#5' → ('F', 1, 5), 'Bb3' → ('B', -1, 3)"""
    if len(pitch_str) >= 3 and pitch_str[1] in ("#", "b"):
        step  = pitch_str[0]
        alter = 1 if pitch_str[1] == "#" else -1
        octave = int(pitch_str[2:])
    else:
        step  = pitch_str[0]
        alter = 0
        octave = int(pitch_str[1:])
    return step, alter, octave


# ── 요소 빌더 ─────────────────────────────────────────────────────────────────

def _build_attributes(measure_num: int, part_clef: str, key: str, time_sig: str) -> etree._Element:
    attrs = etree.Element("attributes")
    etree.SubElement(attrs, "divisions").text = str(DIVISIONS)

    key_el = etree.SubElement(attrs, "key")
    etree.SubElement(key_el, "fifths").text = str(KEY_FIFTHS.get(key, 0))
    etree.SubElement(key_el, "mode").text = "minor" if "minor" in key else "major"

    beats, beat_type = time_sig.split("/")
    time_el = etree.SubElement(attrs, "time")
    etree.SubElement(time_el, "beats").text = beats
    etree.SubElement(time_el, "beat-type").text = beat_type

    sign, line = CLEF_MAP.get(part_clef, ("G", "2"))
    clef_el = etree.SubElement(attrs, "clef")
    etree.SubElement(clef_el, "sign").text = sign
    etree.SubElement(clef_el, "line").text = line

    return attrs


def _build_harmony(chord: ValidatedChord) -> etree._Element:
    sym: ChordSymbol = chord.normalized
    harmony = etree.Element("harmony")
    root = etree.SubElement(harmony, "root")
    etree.SubElement(root, "root-step").text = sym.root_step
    if sym.root_alter != 0:
        etree.SubElement(root, "root-alter").text = str(sym.root_alter)
    etree.SubElement(harmony, "kind").text = sym.kind
    if sym.bass_step:
        bass = etree.SubElement(harmony, "bass")
        etree.SubElement(bass, "bass-step").text = sym.bass_step
        if sym.bass_alter != 0:
            etree.SubElement(bass, "bass-alter").text = str(sym.bass_alter)
    return harmony


def _build_rest(time_sig: str) -> etree._Element:
    """전마디 쉼표. 박자표에 맞는 duration 계산."""
    note = etree.Element("note")
    etree.SubElement(note, "rest", attrib={"measure": "yes"})
    etree.SubElement(note, "duration").text = str(_measure_total_ticks(time_sig))
    etree.SubElement(note, "voice").text = "1"
    return note


def _build_note_element(n: RawNote, is_chord: bool) -> etree._Element:
    note_el = etree.Element("note")

    if is_chord:
        etree.SubElement(note_el, "chord")

    if n.pitch == "rest":
        etree.SubElement(note_el, "rest")
    else:
        try:
            step, alter, octave = _parse_pitch(n.pitch)
        except (ValueError, IndexError):
            log.warning(f"pitch 파싱 실패: {n.pitch!r}, 쉼표로 대체")
            etree.SubElement(note_el, "rest")
            step, alter, octave = "C", 0, 4  # fallback (rest는 pitch 무시)

        if n.pitch != "rest":
            pitch_el = etree.SubElement(note_el, "pitch")
            etree.SubElement(pitch_el, "step").text = step
            if alter != 0:
                etree.SubElement(pitch_el, "alter").text = str(alter)
            etree.SubElement(pitch_el, "octave").text = str(octave)

    ticks = _note_ticks(n.duration, n.dots)
    etree.SubElement(note_el, "duration").text = str(ticks)

    if n.tie_end:
        etree.SubElement(note_el, "tie", type="stop")
    if n.tie_start:
        etree.SubElement(note_el, "tie", type="start")

    etree.SubElement(note_el, "voice").text = str(n.voice)
    etree.SubElement(note_el, "type").text = n.duration
    for _ in range(n.dots):
        etree.SubElement(note_el, "dot")

    if n.tie_start or n.tie_end:
        notations = etree.SubElement(note_el, "notations")
        if n.tie_end:
            etree.SubElement(notations, "tied", type="stop")
        if n.tie_start:
            etree.SubElement(notations, "tied", type="start")

    return note_el


def _build_measure_notes(notes: list[RawNote], time_sig: str) -> list[etree._Element]:
    """마디 내 음표 목록 → MusicXML 요소 목록 (backup 포함 다성부 지원)."""
    # 전쉼표(whole rest)는 박자표 무관 "전마디 쉼표" 기호 → 박자표 기반 measure rest로 교체
    if all(n.pitch == "rest" for n in notes):
        return [_build_rest(time_sig)]

    elements: list[etree._Element] = []
    measure_ticks = _measure_total_ticks(time_sig)

    # voice별로 분리
    by_voice: dict[int, list[RawNote]] = {}
    for n in notes:
        by_voice.setdefault(n.voice, []).append(n)

    voices = sorted(by_voice.keys())
    for v_idx, voice in enumerate(voices):
        if v_idx > 0:
            # 이전 voice 끝 후 마디 처음으로 backup
            backup = etree.Element("backup")
            etree.SubElement(backup, "duration").text = str(measure_ticks)
            elements.append(backup)

        voice_notes = sorted(by_voice[voice], key=lambda n: (n.beat, n.pitch))
        prev_beat: float | None = None

        for note in voice_notes:
            is_chord = (prev_beat is not None and note.beat == prev_beat)
            elements.append(_build_note_element(note, is_chord))
            if not is_chord:
                prev_beat = note.beat

    return elements


# ── 메인 빌더 ─────────────────────────────────────────────────────────────────

def build_musicxml(
    layout: ScoreLayout,
    validated_chords: list[ValidatedChord],
    raw_notes: list[RawNote] | None = None,
) -> bytes:
    chord_by_measure: dict[int, ValidatedChord] = {c.measure: c for c in validated_chords}

    # 파트 × 마디 → 음표 목록
    notes_lookup: dict[tuple[str, int], list[RawNote]] = {}
    if raw_notes:
        for n in raw_notes:
            notes_lookup.setdefault((n.part_id, n.measure), []).append(n)

    parts_with_notes = {n.part_id for n in (raw_notes or [])}

    # 코드 심볼이 있는 파트 ID 집합 (measure 기준으로 판단)
    chord_part_ids: set[str] = set()
    if validated_chords:
        # raw_chords의 source_system을 쓸 수 없으므로 layout의 첫 treble 파트로 결정
        from ..pipeline.pass2a import CHORD_SYMBOL_TREBLE_NAMES
        for part in layout.parts:
            if part.name in CHORD_SYMBOL_TREBLE_NAMES:
                chord_part_ids.add(part.id)
        # fallback: 알려진 파트가 없으면 첫 treble 파트
        if not chord_part_ids:
            first_treble = next((p for p in layout.parts if p.clef == "treble"), None)
            if first_treble:
                chord_part_ids.add(first_treble.id)

    root_el = etree.Element("score-partwise", version="4.0")

    part_list = etree.SubElement(root_el, "part-list")
    for part in layout.parts:
        sp = etree.SubElement(part_list, "score-part", id=part.id)
        etree.SubElement(sp, "part-name").text = part.name

    for part in layout.parts:
        part_el = etree.SubElement(root_el, "part", id=part.id)
        is_chord_part = part.id in chord_part_ids
        has_notes = part.id in parts_with_notes

        prev_key = prev_time = None

        for m_num in range(1, layout.total_measures + 1):
            sys = _get_system_at(m_num, layout.systems)
            key      = sys.key            if sys else "C major"
            time_sig = sys.time_signature if sys else "4/4"

            measure_el = etree.SubElement(part_el, "measure", number=str(m_num))

            if m_num == 1 or key != prev_key or time_sig != prev_time:
                measure_el.append(_build_attributes(m_num, part.clef, key, time_sig))
            prev_key  = key
            prev_time = time_sig

            # harmony (코드 심볼 파트에만)
            if is_chord_part and m_num in chord_by_measure:
                chord = chord_by_measure[m_num]
                if chord.normalized is not None:
                    if chord.needs_review:
                        flags_str = ",".join(chord.flags)
                        measure_el.append(etree.Comment(
                            f" REVIEW: m{m_num} conf={chord.confidence:.2f} flags={flags_str} "
                        ))
                    measure_el.append(_build_harmony(chord))

            # 음표 또는 전마디 쉼표
            measure_notes = notes_lookup.get((part.id, m_num), [])
            if has_notes and measure_notes:
                for el in _build_measure_notes(measure_notes, time_sig):
                    measure_el.append(el)
            else:
                measure_el.append(_build_rest(time_sig))

    xml_bytes = etree.tostring(
        root_el,
        pretty_print=True,
        xml_declaration=True,
        encoding="UTF-8",
    )
    note_count = len(raw_notes) if raw_notes else 0
    log.info(
        f"MusicXML 생성 완료: {len(layout.parts)}파트, "
        f"{layout.total_measures}마디, {len(chord_by_measure)}코드, {note_count}음표"
    )
    return xml_bytes
