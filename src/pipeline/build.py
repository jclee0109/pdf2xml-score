"""MusicXML 빌더 — ValidatedChord + ScoreLayout → MusicXML 4.0"""
from __future__ import annotations
import logging
from lxml import etree

from ..models.score import ScoreLayout, SystemInfo
from ..models.chord import ChordSymbol, KEY_FIFTHS
from .pass3 import ValidatedChord

log = logging.getLogger(__name__)

DIVISIONS = 4   # ticks per quarter note → whole = 16 ticks

CLEF_MAP = {
    "treble": ("G", "2"),
    "bass":   ("F", "4"),
    "alto":   ("C", "3"),
    "tenor":  ("C", "4"),
}


def _get_system_at(measure: int, systems: list[SystemInfo]) -> SystemInfo | None:
    for s in systems:
        if s.start_measure <= measure <= s.end_measure:
            return s
    return None


def _key_changes_at(measure: int, systems: list[SystemInfo]) -> bool:
    """이 마디에서 조표가 바뀌는가."""
    curr = _get_system_at(measure, systems)
    if curr is None or curr.start_measure != measure:
        return False
    # 이전 마디의 키와 비교
    prev = _get_system_at(measure - 1, systems)
    return prev is None or prev.key != curr.key


def _build_attributes(
    measure_num: int,
    part_clef: str,
    key: str,
    time_sig: str,
) -> etree._Element:
    attrs = etree.Element("attributes")
    etree.SubElement(attrs, "divisions").text = str(DIVISIONS)

    # key
    key_el = etree.SubElement(attrs, "key")
    fifths = KEY_FIFTHS.get(key, 0)
    etree.SubElement(key_el, "fifths").text = str(fifths)
    mode = "minor" if "minor" in key else "major"
    etree.SubElement(key_el, "mode").text = mode

    # time
    beats, beat_type = time_sig.split("/")
    time_el = etree.SubElement(attrs, "time")
    etree.SubElement(time_el, "beats").text = beats
    etree.SubElement(time_el, "beat-type").text = beat_type

    # clef
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


def _build_rest(beats: int = 4, beat_type: int = 4) -> etree._Element:
    """박자표 기준 전마디 쉼표."""
    note = etree.Element("note")
    etree.SubElement(note, "rest", attrib={"measure": "yes"})
    duration = DIVISIONS * beats
    etree.SubElement(note, "duration").text = str(duration)
    etree.SubElement(note, "voice").text = "1"
    etree.SubElement(note, "type").text = "whole"
    return note


def build_musicxml(
    layout: ScoreLayout,
    validated_chords: list[ValidatedChord],
) -> bytes:
    chord_by_measure: dict[int, ValidatedChord] = {c.measure: c for c in validated_chords}

    root = etree.Element("score-partwise", version="4.0")

    # part-list
    part_list = etree.SubElement(root, "part-list")
    for part in layout.parts:
        sp = etree.SubElement(part_list, "score-part", id=part.id)
        etree.SubElement(sp, "part-name").text = part.name

    # parts
    for part in layout.parts:
        part_el = etree.SubElement(root, "part", id=part.id)
        is_piano_treble = (part.name == "Piano treble")

        prev_key = None
        prev_time = None

        for m_num in range(1, layout.total_measures + 1):
            sys = _get_system_at(m_num, layout.systems)
            key = sys.key if sys else "C major"
            time_sig = sys.time_signature if sys else "4/4"
            beats_str, beat_type_str = time_sig.split("/")

            measure_el = etree.SubElement(part_el, "measure", number=str(m_num))

            # attributes: 첫 마디 또는 key/time 변화 시
            if m_num == 1 or key != prev_key or time_sig != prev_time:
                measure_el.append(_build_attributes(m_num, part.clef, key, time_sig))
            prev_key = key
            prev_time = time_sig

            # harmony (Piano treble에만)
            if is_piano_treble and m_num in chord_by_measure:
                chord = chord_by_measure[m_num]
                if chord.normalized is not None:
                    if chord.needs_review:
                        flags_str = ",".join(chord.flags)
                        measure_el.append(etree.Comment(
                            f" REVIEW: m{m_num} conf={chord.confidence:.2f} flags={flags_str} "
                        ))
                    measure_el.append(_build_harmony(chord))

            # 전마디 쉼표
            measure_el.append(_build_rest(int(beats_str), int(beat_type_str)))

    xml_bytes = etree.tostring(
        root,
        pretty_print=True,
        xml_declaration=True,
        encoding="UTF-8",
    )
    log.info(f"MusicXML 생성 완료: {len(layout.parts)}파트, "
             f"{layout.total_measures}마디, {len(chord_by_measure)}코드")
    return xml_bytes
