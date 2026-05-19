from __future__ import annotations
import os
import json
import xml.etree.ElementTree as ET
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.metrics import analyze_bytes
from app.prompts import (
    single_target_interpretation,
    comparison_interpretation,
    focused_question,
)

try:
    from groq import Groq
    _groq_available = True
except ImportError:
    _groq_available = False


def _make_groq_client():
    if not _groq_available:
        raise HTTPException(
            status_code=503,
            detail="The 'groq' package is not installed. Run: pip install groq",
        )
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="GROQ_API_KEY environment variable is not set. "
                   "Set it in your .env file and restart the server.",
        )
    return Groq(api_key=api_key)


LLM_MODEL = "llama-3.3-70b-versatile"

app = FastAPI(title="JaCoCo Coverage Interpretation Model")

import pathlib
ROOT = pathlib.Path(__file__).parent.parent
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = ROOT / "templates" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


def _require_xml(filename: str | None) -> None:
    if not (filename or "").lower().endswith(".xml"):
        raise HTTPException(
            status_code=400,
            detail="Wrong file format: only .xml files are accepted.",
        )


def _parse_or_raise(content: bytes, label: str) -> dict:
    try:
        results = analyze_bytes(content)
    except ET.ParseError:
        raise HTTPException(
            status_code=400,
            detail=f"Wrong file format: '{label}' is not valid XML.",
        )
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Wrong structure: '{label}' is not a JaCoCo report.",
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse '{label}': {e}")
    if not results.get("project"):
        raise HTTPException(
            status_code=400,
            detail=f"Wrong structure: '{label}' does not contain JaCoCo coverage data.",
        )
    return results


@app.post("/api/analyze")
async def api_analyze(file: UploadFile = File(...)):
    #parse a single JaCoCo XML report and return all metrics as JSON.
    _require_xml(file.filename)
    content = await file.read()
    results = _parse_or_raise(content, file.filename)
    results["_filename"] = file.filename
    return JSONResponse(results)


@app.post("/api/compare")
async def api_compare(
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
):
    #parse two JaCoCo reports independently and return them under keys 'a' and 'b'.
    _require_xml(file_a.filename)
    _require_xml(file_b.filename)
    content_a = await file_a.read()
    content_b = await file_b.read()
    results_a = _parse_or_raise(content_a, file_a.filename)
    results_b = _parse_or_raise(content_b, file_b.filename)
    results_a["_filename"] = file_a.filename
    results_b["_filename"] = file_b.filename
    return JSONResponse({"a": results_a, "b": results_b})


class ExplainRequest(BaseModel):
    target: dict
    level: str
    show_prompt: bool = False


def _stream_llm(system: str, user: str, prompt_visible: bool):
    #generator that forwards LLM tokens to the client as Server-Sent Events.
    client = _make_groq_client()

    if prompt_visible:
        yield f"data: {json.dumps({'prompt': {'system': system, 'user': user}})}\n\n"

    try:
        stream = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=1024,
            stream=True,
        )
        for chunk in stream:
            text = chunk.choices[0].delta.content
            if text:
                yield f"data: {json.dumps({'text': text})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
        return

    yield f"data: {json.dumps({'done': True})}\n\n"


@app.post("/api/explain")
async def api_explain(req: ExplainRequest):
    system, user = single_target_interpretation(req.target, req.level)
    return StreamingResponse(
        _stream_llm(system, user, req.show_prompt),
        media_type="text/event-stream",
    )


class CompareExplainRequest(BaseModel):
    target_a: dict
    target_b: dict
    label_a: str
    label_b: str
    level: str
    show_prompt: bool = False


@app.post("/api/explain/compare")
async def api_explain_compare(req: CompareExplainRequest):
    system, user = comparison_interpretation(
        req.target_a, req.target_b, req.label_a, req.label_b, req.level
    )
    return StreamingResponse(
        _stream_llm(system, user, req.show_prompt),
        media_type="text/event-stream",
    )


class AskRequest(BaseModel):
    target: dict
    level: str
    question: str
    show_prompt: bool = False


@app.post("/api/ask")
async def api_ask(req: AskRequest):
    system, user = focused_question(req.target, req.level, req.question)
    return StreamingResponse(
        _stream_llm(system, user, req.show_prompt),
        media_type="text/event-stream",
    )



#health check (handy when wiring up the frontend)
@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "llm_configured": bool(os.environ.get("GROQ_API_KEY")),
        "llm_installed": _groq_available,
    }
