"""PDF → MusicXML 변환 웹 앱"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

import sys
sys.path.insert(0, str(Path(__file__).parent))

app = FastAPI()

_JOBS: dict[str, dict] = {}  # job_id → {status, output_path, error}


@app.get("/", response_class=HTMLResponse)
async def index():
    return Path("templates/index.html").read_text()


@app.post("/convert")
async def convert(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    pdf_stem = Path(file.filename).stem  # 확장자 제외 파일명
    _JOBS[job_id] = {"status": "processing", "output_path": None, "error": None, "pdf_stem": pdf_stem}

    tmp_dir = Path(tempfile.mkdtemp())
    pdf_path = tmp_dir / file.filename
    pdf_path.write_bytes(await file.read())

    asyncio.create_task(_run_pipeline(job_id, pdf_path, tmp_dir))
    return {"job_id": job_id}


@app.get("/status/{job_id}")
async def status(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        return {"status": "not_found"}
    return {"status": job["status"], "error": job.get("error")}


@app.get("/download/{job_id}")
async def download(job_id: str):
    job = _JOBS.get(job_id)
    if not job or job["status"] != "done" or not job["output_path"]:
        return {"error": "not ready"}
    pdf_stem = job.get("pdf_stem", "output")
    return FileResponse(
        job["output_path"],
        media_type="application/xml",
        filename=f"{pdf_stem}.musicxml",
    )


async def _run_pipeline(job_id: str, pdf_path: Path, tmp_dir: Path):
    out_dir = tmp_dir / "output"
    out_dir.mkdir()
    try:
        import logging
        logging.disable(logging.CRITICAL)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _sync_pipeline, pdf_path, out_dir)

        mxl = out_dir / "output.musicxml"
        if mxl.exists():
            _JOBS[job_id]["output_path"] = str(mxl)
            _JOBS[job_id]["status"] = "done"
        else:
            _JOBS[job_id]["status"] = "error"
            _JOBS[job_id]["error"] = "MusicXML 생성 실패"
    except Exception as e:
        _JOBS[job_id]["status"] = "error"
        _JOBS[job_id]["error"] = str(e)


def _sync_pipeline(pdf_path: Path, out_dir: Path):
    from src.pipeline.runner import run_sprint1
    run_sprint1(str(pdf_path), str(out_dir))
