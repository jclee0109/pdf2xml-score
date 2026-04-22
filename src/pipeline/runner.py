"""파이프라인 메인 러너 — Sprint 1: Pass 1 + Pass 2a"""
import logging
from pathlib import Path

from ..models.score import ScoreDocument, PipelineStatus
from ..utils.render import render_pdf, load_image
from .pass1 import run_pass1, layout_from_json
from .pass2a import run_pass2a, chords_from_json

log = logging.getLogger(__name__)


def run_sprint1(pdf_path: str | Path, output_dir: str | Path) -> ScoreDocument:
    """API 호출 모드 — ANTHROPIC_API_KEY 필요."""
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)

    doc = ScoreDocument(
        id=pdf_path.stem,
        source_pdf=str(pdf_path),
        pages=0,
        status=PipelineStatus.RENDERING,
    )

    log.info(f"Rendering {pdf_path.name} @ 300dpi")
    page_paths = render_pdf(pdf_path, output_dir / "pages", dpi=300)
    doc.pages = len(page_paths)
    page_images = [load_image(p) for p in page_paths]

    doc.layout = run_pass1(page_images)
    doc.status = PipelineStatus.PASS1_DONE

    doc.raw_chords = run_pass2a(page_images, doc.layout)
    doc.status = PipelineStatus.PASS2A_DONE

    return doc


def run_sprint1_from_files(output_dir: str | Path) -> ScoreDocument:
    """파일 기반 모드 — API 키 불필요. output_dir에 JSON 파일이 있어야 함.

    필요 파일:
      output_dir/pass1_layout.json   — ScoreLayout
      output_dir/pass2a_chords.json  — list[RawChord]
    """
    output_dir = Path(output_dir)

    layout_path = output_dir / "pass1_layout.json"
    chords_path = output_dir / "pass2a_chords.json"

    if not layout_path.exists():
        raise FileNotFoundError(f"Pass 1 결과 없음: {layout_path}")
    if not chords_path.exists():
        raise FileNotFoundError(f"Pass 2a 결과 없음: {chords_path}")

    layout = layout_from_json(layout_path)
    chords = chords_from_json(chords_path)

    doc = ScoreDocument(
        id=output_dir.name,
        source_pdf="",
        pages=max((s.page for s in layout.systems), default=0),
        status=PipelineStatus.PASS2A_DONE,
        layout=layout,
        raw_chords=chords,
    )
    log.info(f"파일 로드 완료: {len(layout.parts)}파트, "
             f"{len(layout.systems)}시스템, {len(chords)}코드")
    return doc
