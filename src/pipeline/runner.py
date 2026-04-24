"""파이프라인 메인 러너"""
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from ..models.score import ScoreDocument, PipelineStatus
from ..utils.render import render_pdf, load_image
from .pass1 import run_pass1, layout_from_json
from .pass2a import run_pass2a, chords_from_json
from .pass2b import run_pass2b, notes_from_json, notes_to_json
from .pass2c import run_pass2c, lyrics_from_json, lyrics_to_json
from .pass3 import validate_chords, validate_notes
from .build import build_musicxml

log = logging.getLogger(__name__)


def validate_musicxml(xml_bytes: bytes) -> list[str]:
    """MusicXML 구조 기본 검증. 오류 목록 반환 (빈 리스트 = 정상)."""
    errors: list[str] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return [f"XML 파싱 오류: {e}"]

    if root.tag != "score-partwise":
        errors.append(f"루트 요소 오류: {root.tag} (score-partwise 필요)")

    part_list = root.find("part-list")
    if part_list is None:
        errors.append("part-list 요소 없음")
    else:
        n_score_parts = len(part_list.findall("score-part"))
        n_parts = len(root.findall("part"))
        if n_score_parts != n_parts:
            errors.append(f"part-list({n_score_parts})와 part({n_parts}) 수 불일치")

    for part_el in root.findall("part"):
        pid = part_el.get("id", "?")
        measures = part_el.findall("measure")
        if not measures:
            errors.append(f"파트 {pid}: 마디(measure) 없음")
            continue
        for m in measures:
            m_num = m.get("number", "?")
            notes  = m.findall("note")
            has_attrs = m.find("attributes") is not None
            if not notes and not has_attrs:
                errors.append(f"파트 {pid} 마디 {m_num}: 음표와 attributes 모두 없음")

    return errors


def run_sprint1(pdf_path: str | Path, output_dir: str | Path) -> ScoreDocument:
    """API 호출 모드 — ANTHROPIC_API_KEY 필요."""
    pdf_path   = Path(pdf_path)
    output_dir = Path(output_dir)

    doc = ScoreDocument(
        id=pdf_path.stem,
        source_pdf=str(pdf_path),
        pages=0,
        status=PipelineStatus.RENDERING,
    )

    log.info(f"Rendering {pdf_path.name} @ 300dpi")
    page_paths  = render_pdf(pdf_path, output_dir / "pages", dpi=300)
    doc.pages   = len(page_paths)
    page_images = [load_image(p) for p in page_paths]

    doc.layout = run_pass1(page_images)
    doc.status = PipelineStatus.PASS1_DONE

    doc.raw_chords = run_pass2a(page_images, doc.layout)
    doc.status     = PipelineStatus.PASS2A_DONE

    doc.raw_notes = run_pass2b(page_images, doc.layout)
    notes_to_json(doc.raw_notes, output_dir / "pass2b_notes.json")
    doc.status    = PipelineStatus.PASS2B_DONE

    doc.raw_lyrics = run_pass2c(page_images, doc.layout)
    lyrics_to_json(doc.raw_lyrics, output_dir / "pass2c_lyrics.json")
    doc.status     = PipelineStatus.PASS2C_DONE

    return _finish(doc, output_dir)


def run_sprint1_from_files(output_dir: str | Path) -> ScoreDocument:
    """파일 기반 모드 — API 키 불필요.

    필수:
      output_dir/pass1_layout.json
      output_dir/pass2a_chords.json
    선택:
      output_dir/pass2b_notes.json   (있으면 음표 포함, 없으면 쉼표만)
    """
    output_dir = Path(output_dir)

    layout_path = output_dir / "pass1_layout.json"
    chords_path = output_dir / "pass2a_chords.json"
    notes_path  = output_dir / "pass2b_notes.json"

    if not layout_path.exists():
        raise FileNotFoundError(f"Pass 1 결과 없음: {layout_path}")
    if not chords_path.exists():
        raise FileNotFoundError(f"Pass 2a 결과 없음: {chords_path}")

    lyrics_path = output_dir / "pass2c_lyrics.json"

    layout = layout_from_json(layout_path)
    chords = chords_from_json(chords_path)
    notes  = notes_from_json(notes_path)   if notes_path.exists()   else []
    lyrics = lyrics_from_json(lyrics_path) if lyrics_path.exists()  else []

    note_info  = f", {len(notes)}음표"   if notes  else ""
    lyric_info = f", {len(lyrics)}음절"  if lyrics else ""
    log.info(
        f"파일 로드 완료: {len(layout.parts)}파트, "
        f"{len(layout.systems)}시스템, {len(chords)}코드{note_info}{lyric_info}"
    )

    doc = ScoreDocument(
        id=output_dir.name,
        source_pdf="",
        pages=max((s.page for s in layout.systems), default=0),
        status=PipelineStatus.PASS2C_DONE if lyrics else (
            PipelineStatus.PASS2B_DONE if notes else PipelineStatus.PASS2A_DONE
        ),
        layout=layout,
        raw_chords=chords,
        raw_notes=notes,
        raw_lyrics=lyrics,
    )

    return _finish(doc, output_dir)


def _finish(doc: ScoreDocument, output_dir: Path) -> ScoreDocument:
    """Pass 3 → Build → 검증 → 저장 공통 경로."""
    validated_chords = validate_chords(doc.raw_chords, doc.layout)
    validated_notes  = validate_notes(doc.raw_notes, doc.layout)

    doc.status       = PipelineStatus.PASS3_DONE
    doc.review_count = sum(1 for v in validated_chords if v.needs_review)

    doc.status = PipelineStatus.BUILDING
    xml_bytes  = build_musicxml(
        doc.layout,
        validated_chords,
        validated_notes,
        doc.raw_lyrics or None,
    )

    # MusicXML 구조 검증
    errors = validate_musicxml(xml_bytes)
    if errors:
        for e in errors:
            log.warning(f"MusicXML 검증 오류: {e}")
    else:
        log.info("MusicXML 구조 검증 통과")

    out_path = output_dir / "output.musicxml"
    out_path.write_bytes(xml_bytes)
    doc.musicxml_draft = str(out_path)
    doc.status         = PipelineStatus.AWAITING_REVIEW

    log.info(f"저장: {out_path}")
    return doc
