"""파이프라인 메인 러너 — Sprint 1: Pass 1 + Pass 2a"""
import logging
from pathlib import Path

from ..models.score import ScoreDocument, PipelineStatus
from ..utils.render import render_pdf, load_image
from .pass1 import run_pass1
from .pass2a import run_pass2a

log = logging.getLogger(__name__)


def run_sprint1(pdf_path: str | Path, output_dir: str | Path) -> ScoreDocument:
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)

    doc = ScoreDocument(
        id=pdf_path.stem,
        source_pdf=str(pdf_path),
        pages=0,
        status=PipelineStatus.RENDERING,
    )

    # 렌더링
    log.info(f"Rendering {pdf_path.name} @ 300dpi")
    page_paths = render_pdf(pdf_path, output_dir / "pages", dpi=300)
    doc.pages = len(page_paths)
    log.info(f"  → {doc.pages}페이지")

    page_images = [load_image(p) for p in page_paths]

    # Pass 1
    doc.status = PipelineStatus.PENDING
    doc.layout = run_pass1(page_images)
    doc.status = PipelineStatus.PASS1_DONE

    # Pass 2a
    doc.raw_chords = run_pass2a(page_images, doc.layout)
    doc.status = PipelineStatus.PASS2A_DONE

    return doc
