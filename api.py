import json
import uuid
import asyncio
import os
from pathlib import Path
from typing import Dict, Any
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
import shutil
import sys
import importlib.util

app = FastAPI(title="Pipeline Autocompilazione")

# === Job State Management ===
jobs: Dict[str, Dict[str, Any]] = {}

PROJECT_ROOT = Path(__file__).resolve().parent
M1_PATH = PROJECT_ROOT / "m1_pipeline" / "main.py"
M2_PATH = PROJECT_ROOT / "m2_pipeline" / "main.py"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Impossibile caricare modulo: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def run_pipeline_task(job_id: str, doc_path: str, data_json_path: str):
    """Esegue la pipeline in background."""
    try:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["progress"] = "Avviamento M1..."

        output_root = PROJECT_ROOT / "output" / job_id
        m1_out = output_root / "m1_output"
        m2_out = output_root / "m2_output"
        m1_out.mkdir(parents=True, exist_ok=True)
        m2_out.mkdir(parents=True, exist_ok=True)

        # --- M1 ---
        jobs[job_id]["progress"] = "M1: Estrazione struttura..."
        sys.path.insert(0, str(PROJECT_ROOT / "m1_pipeline"))
        m1_main = _load_module("m1_main", M1_PATH)
        
        docx_source = m1_out / Path(doc_path).name
        shutil.copy2(doc_path, docx_source)

        m1_main.process(doc_path, output_path=None, merge_nearby=False, output_dir=str(m1_out))

        jobs[job_id]["progress"] = "M1 completato. Avviamento M2..."

        # --- M2 ---
        jobs[job_id]["progress"] = "M2: Mappatura campi..."
        sys.path.insert(0, str(PROJECT_ROOT / "m2_pipeline"))
        os.environ["M2_EXTRA_DOCX_DIRS"] = str(m1_out)
        m2_main = _load_module("m2_main", M2_PATH)
        os.environ["M2_CLASSIFY_INPUT_DOC"] = str(doc_path)
        
        m2_result = m2_main.run_all(
            m1_dir=str(m1_out),
            output_dir=str(m2_out),
            data_json=data_json_path,
            bundle_name=None,
            venv_python=sys.executable,
        )

        jobs[job_id]["progress"] = "M2 completato. Finalizzazione..."
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["output_dir"] = str(m2_out)
        jobs[job_id]["progress"] = "Pipeline completata con successo"

    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["progress"] = f"Errore: {str(e)}"
        jobs[job_id]["error"] = str(e)


# === ENDPOINTS ===

@app.post("/upload")
async def upload(file: UploadFile = File(...), data_json: UploadFile = File(...), background_tasks: BackgroundTasks = BackgroundTasks()):
    """
    Carica documento e dati anagrafica, avvia pipeline.
    
    Returns:
        {"job_id": "uuid", "status": "queued"}
    """
    job_id = str(uuid.uuid4())
    
    # Salva file temporanei
    tmp_dir = PROJECT_ROOT / "tmp" / job_id
    tmp_dir.mkdir(parents=True, exist_ok=True)
    
    doc_path = tmp_dir / file.filename
    data_json_path = tmp_dir / "data.json"
    
    with open(doc_path, "wb") as f:
        f.write(await file.read())
    
    with open(data_json_path, "wb") as f:
        f.write(await data_json.read())
    
    # Crea job
    jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "progress": "In attesa di elaborazione",
        "doc_name": file.filename,
    }
    
    # Avvia elaborazione in background
    background_tasks.add_task(run_pipeline_task, job_id, str(doc_path), str(data_json_path))
    
    return {
        "job_id": job_id,
        "status": "queued",
        "message": "Upload ricevuto, elaborazione in corso..."
    }


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    """
    Ritorna lo stato di un job.
    
    Returns:
        {"job_id": "uuid", "status": "running|completed|failed", "progress": "..."}
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job non trovato")
    
    return {
        "job_id": job_id,
        "status": jobs[job_id]["status"],
        "progress": jobs[job_id].get("progress", ""),
        "error": jobs[job_id].get("error"),
    }


@app.get("/download/{job_id}")
async def download(job_id: str):
    """
    Scarica il DOCX compilato.
    
    Returns:
        File DOCX
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job non trovato")
    
    job = jobs[job_id]
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Job non completato: {job['status']}")
    
    output_dir = Path(job["output_dir"])
    
    # Cerca il DOCX finale (step11 writer_docx output)
    final_docx = output_dir / "documento_compilato_finale.docx"
    if not final_docx.exists():
        # Fallback: cerca qualsiasi .docx
        docx_files = list(output_dir.glob("*.docx"))
        if not docx_files:
            raise HTTPException(status_code=500, detail="DOCX compilato non trovato")
        final_docx = docx_files[0]
    
    return FileResponse(
        path=final_docx,
        filename=f"compilato_{job_id}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


@app.get("/health")
async def health():
    """Health check."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)
