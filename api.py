import json
import uuid
import asyncio
import os
from pathlib import Path
from typing import Dict, Any
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse
import shutil
import sys
import importlib.util
import zipfile
import subprocess
import tempfile
import base64
import fitz 

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

        old_output = PROJECT_ROOT / "output"
        print(f"[CLEANUP] Cancello output precedente: {old_output} exists={old_output.exists()}", flush=True)
        shutil.rmtree(old_output, ignore_errors=True)
        print(f"[CLEANUP] Output cancellato: exists={old_output.exists()}", flush=True)

        output_root = PROJECT_ROOT / "output" / job_id
        print(f"[JOB] Nuovo output_root: {output_root}", flush=True)


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
        os.environ["M2_FORCE_SOURCE_DOCX"] = str(docx_source)
        print(f"[SOURCE] Forzo DOCX sorgente M2: {docx_source}", flush=True)

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

    old_tmp = PROJECT_ROOT / "tmp"
    print(f"[CLEANUP] Cancello tmp precedente: {old_tmp} exists={old_tmp.exists()}", flush=True)
    shutil.rmtree(old_tmp, ignore_errors=True)
    print(f"[CLEANUP] Tmp cancellato: exists={old_tmp.exists()}", flush=True)
    
    # Salva file temporanei
    tmp_dir = PROJECT_ROOT / "tmp" / job_id
    print(f"[UPLOAD] Nuova tmp_dir: {tmp_dir}", flush=True)


    tmp_dir.mkdir(parents=True, exist_ok=True)
    
    doc_path = tmp_dir / file.filename
    data_json_path = tmp_dir / "data.json"
    
    with open(doc_path, "wb") as f:
        f.write(await file.read())
    
    with open(data_json_path, "wb") as f:
        f.write(await data_json.read())

    print(f"[UPLOAD] Documento salvato: {doc_path} exists={doc_path.exists()} size={doc_path.stat().st_size}", flush=True)
    print(f"[UPLOAD] JSON salvato: {data_json_path} exists={data_json_path.exists()} size={data_json_path.stat().st_size}", flush=True)

    
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
async def download(job_id: str, variant: str = Query(default="final", pattern="^(final|preview|any)$")):
    """
    Scarica il DOCX compilato.

    Query params:
      - variant=final   -> documento_compilato_finale.docx (default)
      - variant=preview -> documento_compilato_preview.docx (se presente)
      - variant=any     -> preview se c'è, altrimenti final, altrimenti primo .docx
    
    Returns:
        File DOCX
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job non trovato")
    
    job = jobs[job_id]
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Job non completato: {job['status']}")
    
    output_dir = Path(job["output_dir"])
    
    final_docx = output_dir / "documento_compilato_finale.docx"
    preview_docx = output_dir / "documento_compilato_preview.docx"

    selected_docx: Path | None = None
    if variant == "preview":
        selected_docx = preview_docx if preview_docx.exists() else None
        if selected_docx is None:
            raise HTTPException(status_code=404, detail="DOCX preview non trovato")
    elif variant == "final":
        selected_docx = final_docx if final_docx.exists() else None
        if selected_docx is None:
            raise HTTPException(status_code=404, detail="DOCX finale non trovato")
    else:  # any
        if preview_docx.exists():
            selected_docx = preview_docx
        elif final_docx.exists():
            selected_docx = final_docx
        else:
            docx_files = list(output_dir.glob("*.docx"))
            selected_docx = docx_files[0] if docx_files else None

    if selected_docx is None or not selected_docx.exists():
        raise HTTPException(status_code=500, detail="DOCX compilato non trovato")
    
    return FileResponse(
        path=selected_docx,
        filename=f"{variant}_{job_id}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


@app.get("/download-mapping/{job_id}")
async def download_mapping(job_id: str, variant: str = Query(default="provvisorio", pattern="^(provvisorio|finale)$")):
    """
    Scarica il JSON di mapping del job.

    Query params:
      - variant=provvisorio -> campo_valore_provvisorio.json (default)
      - variant=finale      -> campo_valore_finale.json
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job non trovato")

    job = jobs[job_id]
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Job non completato: {job['status']}")

    output_dir = Path(job["output_dir"])
    mapping_path = output_dir / ("campo_valore_provvisorio.json" if variant == "provvisorio" else "campo_valore_finale.json")
    if not mapping_path.exists():
        raise HTTPException(status_code=404, detail=f"Mapping {variant} non trovato")

    return FileResponse(
        path=mapping_path,
        filename=f"mapping_{variant}_{job_id}.json",
        media_type="application/json",
    )

@app.get("/download-m1/{job_id}")
async def download_m1_output(job_id: str):
    """
    Scarica tutta la cartella m1_output del job in ZIP.
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job non trovato")

    job = jobs[job_id]
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Job non completato: {job['status']}")

    m2_dir = Path(job["output_dir"])
    m1_dir = m2_dir.parent / "m1_output"

    if not m1_dir.exists():
        raise HTTPException(status_code=404, detail="Cartella m1_output non trovata")

    zip_path = m2_dir.parent / "m1_output_debug.zip"

    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for path in m1_dir.rglob("*"):
            if path.is_file():
                z.write(path, path.relative_to(m1_dir))

    return FileResponse(
        path=zip_path,
        filename=f"m1_output_{job_id}.zip",
        media_type="application/zip",
    )

@app.get("/preview-pages/{job_id}")
async def preview_pages(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job non trovato")
    job = jobs[job_id]
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Job non completato: {job['status']}")

    output_dir = Path(job["output_dir"])
    preview_pdf = output_dir / "documento_compilato_preview.pdf"
    final_pdf = output_dir / "documento_compilato_finale.pdf"
    preview_docx = output_dir / "documento_compilato_preview.docx"
    final_docx   = output_dir / "documento_compilato_finale.docx"
    pages_b64 = []

    pdf_path = preview_pdf if preview_pdf.exists() else final_pdf
    if pdf_path.exists():
        doc = fitz.open(str(pdf_path))
        for pg in doc:
            pix = pg.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
            pages_b64.append(base64.b64encode(pix.tobytes("png")).decode())
        doc.close()
        return {"pages": pages_b64, "total": len(pages_b64)}
    
    docx_path = preview_docx if preview_docx.exists() else final_docx
    if not docx_path.exists():
        raise HTTPException(status_code=404, detail="Nessun file compilato trovato")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_file = Path(tmpdir) / "preview.pdf"
        convert_script = PROJECT_ROOT / "m1_pipeline" / "postprocessing" / "convert_docx_to_pdf.py"
        subprocess.run([
            sys.executable, str(convert_script),
            "--input-docx", str(docx_path),
            "--out-pdf", str(pdf_file)
        ], check=True, timeout=120)
        doc = fitz.open(str(pdf_file))
        for pg in doc:
            pix = pg.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
            pages_b64.append(base64.b64encode(pix.tobytes("png")).decode())
        doc.close()

    return {"pages": pages_b64, "total": len(pages_b64)}

@app.get("/health")
async def health():
    """Health check."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)
