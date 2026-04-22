from __future__ import annotations
import re
from dataclasses import dataclass


# chord text quality → MusicXML kind (긴 패턴 먼저)
QUALITY_MAP: list[tuple[str, str]] = [
    ("maj13",  "major-13th"),
    ("maj11",  "major-11th"),
    ("maj9",   "major-ninth"),
    ("maj7",   "major-seventh"),
    ("maj",    "major"),
    ("m7b5",   "half-diminished"),
    ("m13",    "minor-13th"),
    ("m11",    "minor-11th"),
    ("m9",     "minor-ninth"),
    ("m7",     "minor-seventh"),
    ("m6",     "minor-sixth"),
    ("m",      "minor"),
    ("dim7",   "diminished-seventh"),
    ("dim",    "diminished"),
    ("aug",    "augmented"),
    ("sus4",   "suspended-fourth"),
    ("sus2",   "suspended-second"),
    ("add9",   "add-ninth"),
    ("13",     "dominant-13th"),
    ("11",     "dominant-11th"),
    ("9",      "dominant-ninth"),
    ("7",      "dominant"),
    ("6",      "major-sixth"),
    ("",       "major"),
]

ALTER_MAP = {"#": 1, "b": -1, "##": 2, "bb": -2, "": 0}
ROOT_PAT = re.compile(r"^([A-G])(##|bb|#|b)?")


@dataclass
class ChordSymbol:
    root_step: str          # "G", "F", "Bb" (step only)
    root_alter: int         # 1=sharp, -1=flat, 0=natural
    kind: str               # MusicXML kind value
    bass_step: str | None   # slash chord bass note step
    bass_alter: int         # bass alteration

    @property
    def root_name(self) -> str:
        alter = {1: "#", -1: "b", 2: "##", -2: "bb", 0: ""}.get(self.root_alter, "")
        return f"{self.root_step}{alter}"

    @property
    def semitone(self) -> int:
        """Root pitch class (0=C)."""
        base = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[self.root_step]
        return (base + self.root_alter) % 12


def parse_chord_text(text: str) -> ChordSymbol | None:
    text = text.strip()
    if not text:
        return None

    # slash chord 분리
    bass_step, bass_alter = None, 0
    if "/" in text:
        text, bass_raw = text.split("/", 1)
        m = ROOT_PAT.match(bass_raw)
        if m:
            bass_step = m.group(1)
            bass_alter = ALTER_MAP.get(m.group(2) or "", 0)

    # root 파싱
    m = ROOT_PAT.match(text)
    if not m:
        return None
    root_step = m.group(1)
    root_alter = ALTER_MAP.get(m.group(2) or "", 0)
    quality_str = text[m.end():]

    # quality 매칭 (긴 것 먼저)
    kind = "major"
    for q_key, q_val in QUALITY_MAP:
        if quality_str == q_key or quality_str.startswith(q_key):
            kind = q_val
            break

    return ChordSymbol(
        root_step=root_step,
        root_alter=root_alter,
        kind=kind,
        bass_step=bass_step,
        bass_alter=bass_alter,
    )


# 키 → 다이아토닉 피치클래스 집합 (0=C)
MAJOR_SCALE_INTERVALS = [0, 2, 4, 5, 7, 9, 11]
MINOR_SCALE_INTERVALS = [0, 2, 3, 5, 7, 8, 10]

NOTE_TO_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

KEY_FIFTHS: dict[str, int] = {
    "C major": 0,  "G major": 1,  "D major": 2,  "A major": 3,
    "E major": 4,  "B major": 5,  "F# major": 6, "C# major": 7,
    "F major": -1, "Bb major": -2,"Eb major": -3,"Ab major": -4,
    "Db major": -5,"Gb major": -6,"Cb major": -7,
    "A minor": 0,  "E minor": 1,  "B minor": 2,  "F# minor": 3,
    "D minor": -1, "G minor": -2, "C minor": -3, "F minor": -4,
    "Bb minor":-5, "Eb minor":-6, "Ab minor": -7,
}


def key_root_pc(key: str) -> int:
    """'G major' → 7 (semitone of root)"""
    parts = key.split()
    note = parts[0]
    # handle Bb, F#, Ab etc.
    if len(note) == 1:
        pc = NOTE_TO_PC.get(note, 0)
    else:
        pc = (NOTE_TO_PC.get(note[0], 0) + ALTER_MAP.get(note[1:], 0)) % 12
    return pc


def diatonic_pcs(key: str) -> set[int]:
    root = key_root_pc(key)
    intervals = MINOR_SCALE_INTERVALS if "minor" in key else MAJOR_SCALE_INTERVALS
    return {(root + i) % 12 for i in intervals}
