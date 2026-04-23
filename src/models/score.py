from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


def _transposition_semitones(name: str) -> int:
    """악기 이름(부분 일치)으로 이조 반음 수 반환. 파트 이름에 '1/2' 등 suffix가 붙어도 매칭."""
    name_lower = name.lower()
    if "piccolo" in name_lower:
        return 12
    if "clarinet in eb" in name_lower:
        return 3
    if "clarinet in bb" in name_lower or "trumpet in bb" in name_lower:
        return -2
    if "horn in f" in name_lower:
        return -7
    if "contrabass" in name_lower:
        return -12
    return 0


TRANSPOSITION_TABLE: dict[str, int] = {
    "Clarinet in Bb": -2,
    "Trumpet in Bb": -2,
    "Horn in F": -7,
    "Clarinet in Eb": 3,
    "Piccolo": 12,
    "Contrabass": -12,
}


@dataclass
class PartInfo:
    id: str
    name: str
    order: int
    clef: str                       # "treble" | "bass" | "alto" | "tenor"
    transposition_semitones: int    # written → concert pitch, 코드로 처리


@dataclass
class RehearsalMark:
    measure: int
    label: str


@dataclass
class RepeatBarline:
    measure: int
    type: str                       # "start" | "end" | "end-start"


@dataclass
class VoltaBracket:
    start_measure: int
    end_measure: int
    number: int


@dataclass
class KeyChange:
    measure: int
    key: str    # e.g. "Ab major"


@dataclass
class SystemInfo:
    page: int
    system_index: int
    start_measure: int
    end_measure: int                # 파이프라인에서 계산 (다음 시스템 start - 1)
    key: str                        # concert pitch 기준 e.g. "G major" (시스템 시작 키)
    time_signature: str             # e.g. "4/4"
    y_top_px: int
    y_bottom_px: int
    active_parts: list[str]         # part id 목록 (active_parts 내 인덱스로 crop)
    rehearsal_marks: list[RehearsalMark] = field(default_factory=list)
    repeat_barlines: list[RepeatBarline] = field(default_factory=list)
    volta_brackets: list[VoltaBracket] = field(default_factory=list)
    key_changes: list[KeyChange] = field(default_factory=list)  # 시스템 내 키 변화


@dataclass
class ScoreLayout:
    parts: list[PartInfo]
    systems: list[SystemInfo]
    total_measures: int
    name_to_id: dict[str, str] = field(default_factory=dict)  # 빌드 후 채워짐


@dataclass
class RawChord:
    measure: int
    beat: float
    chord_text: str
    confidence: float
    source_page: int
    source_system: int


@dataclass
class RawNote:
    measure: int
    beat: float
    pitch: str          # written pitch e.g. "G4", "F#3", "rest"
    duration: str       # "whole" | "half" | "quarter" | "eighth" | "16th" | "32nd"
    dots: int
    tie_start: bool
    tie_end: bool
    voice: int
    confidence: float
    part_id: str
    source_system: int


class PipelineStatus(Enum):
    PENDING         = "pending"
    RENDERING       = "rendering"
    PASS1_DONE      = "pass1_done"
    PASS2A_DONE     = "pass2a_done"
    PASS2B_DONE     = "pass2b_done"
    PASS3_DONE      = "pass3_done"
    BUILDING        = "building"
    AWAITING_REVIEW = "awaiting_review"
    DONE            = "done"


@dataclass
class ScoreDocument:
    id: str
    source_pdf: str
    pages: int
    status: PipelineStatus = PipelineStatus.PENDING
    layout: ScoreLayout | None = None
    raw_chords: list[RawChord] = field(default_factory=list)
    raw_notes: list[RawNote] = field(default_factory=list)
    musicxml_draft: str | None = None
    review_count: int = 0
