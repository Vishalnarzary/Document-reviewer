from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .checklists import (
    checklist_definitions,
    remove_checklist,
    save_checklist,
    supported_categories,
    update_checklist,
)
from .config import OUTPUT_DIR, STATIC_DIR
from .models import ChatRequest, ChecklistInput
from .storage import store
from .workflow import workflow


ANALYSIS_PIPELINE_VERSION = "dynamic-checklists-vision-v26"


app = FastAPI(
    title="Pre-Approval Website Verification Tool",
    description="Evidence-gathering assistant for human pre-approval reviewers.",
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/artifacts", StaticFiles(directory=OUTPUT_DIR), name="artifacts")


@app.get("/", include_in_schema=False)
async def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "groq_configured": workflow.groq.enabled,
        "groq_model": workflow.groq.model,
        "groq_vision_model": workflow.groq.vision_model,
        "analysis_pipeline": ANALYSIS_PIPELINE_VERSION,
    }


@app.get("/api/categories")
async def categories() -> list[dict[str, str]]:
    return supported_categories()


@app.get("/api/checklists")
async def list_checklists() -> list[dict]:
    return checklist_definitions()


@app.post("/api/checklists", status_code=201)
async def create_checklist(payload: ChecklistInput) -> dict:
    try:
        return save_checklist(payload)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/checklists/{category}")
async def edit_checklist(category: str, payload: ChecklistInput) -> dict:
    try:
        return update_checklist(category, payload)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/checklists/{category}", status_code=204)
async def delete_checklist(category: str) -> None:
    try:
        remove_checklist(category)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/reviews")
async def recent_reviews() -> list[dict]:
    return [state.model_dump(mode="json") for state in store.recent()]


async def _review_progress_stream(upload_path: Path, filename: str):
    updates: asyncio.Queue[dict] = asyncio.Queue()

    def publish(progress: int, stage: str) -> None:
        updates.put_nowait({"type": "progress", "progress": progress, "stage": stage})

    task = asyncio.create_task(workflow.create(upload_path, filename, publish))
    try:
        while not task.done() or not updates.empty():
            try:
                update = await asyncio.wait_for(updates.get(), timeout=0.5)
            except TimeoutError:
                continue
            yield json.dumps(update, ensure_ascii=False) + "\n"
        state = await task
        yield json.dumps({"type": "complete", "progress": 100, "stage": "Review complete", "review_id": state.id}) + "\n"
    except asyncio.CancelledError:
        task.cancel()
        raise
    except Exception as exc:
        yield json.dumps({"type": "error", "message": f"The application could not be reviewed: {exc}"}) + "\n"
    finally:
        upload_path.unlink(missing_ok=True)


@app.post("/api/reviews")
async def create_review(file: UploadFile = File(...)) -> StreamingResponse:
    filename = file.filename or "application.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF application form.")
    with tempfile.NamedTemporaryFile(prefix="preapproval-", suffix=".pdf", delete=False) as handle:
        temp_path = Path(handle.name)
        shutil.copyfileobj(file.file, handle)
    await file.close()
    if temp_path.stat().st_size > 20 * 1024 * 1024:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail="The PDF must be smaller than 20 MB.")
    return StreamingResponse(
        _review_progress_stream(temp_path, filename),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/reviews/{review_id}")
async def get_review(review_id: str) -> dict:
    try:
        return store.load(review_id).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Review not found.") from exc


@app.post("/api/reviews/{review_id}/messages")
async def send_message(review_id: str, request: ChatRequest) -> dict:
    try:
        state = store.load(review_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Review not found.") from exc
    state = await workflow.handle_message(state, request.message)
    return state.model_dump(mode="json")


@app.get("/api/reviews/{review_id}/download")
async def download_review(review_id: str) -> FileResponse:
    try:
        state = store.load(review_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Review not found.") from exc
    if not state.package_zip:
        raise HTTPException(status_code=404, detail="The report package has not been generated yet.")
    package = Path(__file__).resolve().parent.parent / state.package_zip
    return FileResponse(package, filename=package.name, media_type="application/zip")
