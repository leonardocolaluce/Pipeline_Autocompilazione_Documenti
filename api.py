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
import re

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

def _safe_download_stem(job: Dict[str, Any]) -> str:
    name = str(job.get("doc_name") or "documento")
    stem = Path(name).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return (stem[:120] or "documento")


def _count_fields_and_tables(mapping_path: Path) -> tuple[int, int]:
    try:
        payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    except Exception:
        return 0, 0

    rows = payload.get("rows") or []
    total = 0
    filled = 0

    for row in rows:
        item_type = str(row.get("item_type", "")).strip()
        if item_type not in {"field", "table_cell"}:
            continue
        total += 1
        ans = str(row.get("answer", "") or "").strip()
        if ans and ans != "N/D":
            filled += 1

    return total, filled

def run_pipeline_task(job_id: str, doc_path: str, data_json_path: str):
    """Esegue la pipeline in background."""
    try:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["progress"] = "Avviamento M1..."

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
        
        src = Path(doc_path)

        if src.suffix.lower() == ".doc":
            from m1_pipeline.loaders.word_loader import convert_doc_to_docx
            tmp_docx = Path(convert_doc_to_docx(str(src)))          # crea .docx in /tmp/...
            docx_source = m1_out / (src.stem + ".docx")             # path finale in m1_out
            shutil.copy2(tmp_docx, docx_source)
            shutil.rmtree(tmp_docx.parent, ignore_errors=True)      # pulizia opzionale
        else:
            docx_source = m1_out / src.name
            shutil.copy2(src, docx_source)


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

        if isinstance(m2_result, dict) and m2_result.get("status") == "skipped":
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["output_dir"] = str(m2_out)
            jobs[job_id]["progress"] = "File non compilabile"
            print(f"[JOB] File non compilabile: {m2_result}", flush=True)
            return

        print(f"[M2_RESULT] {m2_result}", flush=True)
        print(f"[M2_OUTPUT_FILES] {list(m2_out.glob('*'))}", flush=True)

        # Log campi/tabelle (escludi checkbox): usa mapping provvisorio se presente
        prov_map = m2_out / "campo_valore_provvisorio.json"
        fallback_map = m2_out / "campo_valore.json"
        mapping_path = prov_map if prov_map.exists() else fallback_map

        fields_msg = None
        if mapping_path.exists():
            total, filled = _count_fields_and_tables(mapping_path)
            fields_msg = f"Campi totali: {total} | Campi compilati: {filled}"
            print(f"[FIELDS] {fields_msg} mapping={mapping_path}", flush=True)
        else:
            print("[FIELDS] mapping non trovato (nessun conteggio campi)", flush=True)

        jobs[job_id]["progress"] = "M2 completato. Finalizzazione..."
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["output_dir"] = str(m2_out)

        final_msg = "Pipeline completata con successo"
        if fields_msg:
            final_msg = f"{final_msg} | {fields_msg}"
        jobs[job_id]["progress"] = final_msg



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
    # Prefer in-memory status when available (single instance/worker).
    if job_id in jobs:
        return {
            "job_id": job_id,
            "status": jobs[job_id]["status"],
            "progress": jobs[job_id].get("progress", ""),
            "error": jobs[job_id].get("error"),
        }

    # Fallback stateless: if outputs exist on disk, treat as completed.
    output_dir = PROJECT_ROOT / "output" / job_id / "m2_output"
    if output_dir.exists():
        if (output_dir / "documento_compilato_preview.docx").exists() or (output_dir / "documento_compilato_finale.docx").exists():
            return {"job_id": job_id, "status": "completed", "progress": "Completato (filesystem)", "error": None}
        if (output_dir / "documento_compilato_preview.pdf").exists() or (output_dir / "documento_compilato_finale.pdf").exists():
            return {"job_id": job_id, "status": "completed", "progress": "Completato (filesystem)", "error": None}

    raise HTTPException(status_code=404, detail="Job non trovato")


@app.get("/download/{job_id}")
async def download(
    job_id: str,
    variant: str = Query(default="final", pattern="^(final|preview|any)$"),
    format: str = Query(default="auto", pattern="^(auto|docx|pdf)$"),
):
    """
    Scarica il DOCX compilato.

    Query params:
      - variant=final   -> documento_compilato_finale.docx (default)
      - variant=preview -> documento_compilato_preview.docx (se presente)
      - variant=any     -> preview se c'è, altrimenti final, altrimenti primo .docx
    
    Returns:
        File DOCX
    """
    output_dir = PROJECT_ROOT / "output" / job_id / "m2_output"
    if not output_dir.exists():
        raise HTTPException(status_code=404, detail="Output job non trovato (job non finito)")

    job = jobs.get(job_id)  # può essere None in multi-worker / multi-instance
    try:
        out_files = sorted([p.name for p in output_dir.glob("*") if p.is_file()])
    except Exception:
        out_files = []

    # --- DEBUG ---
    print(
        f"[DOWNLOAD] job_id={job_id} variant={variant} format={format} output_dir={output_dir} job_in_mem={job is not None}",
        flush=True,
    )
    if out_files:
        print(f"[DOWNLOAD] m2_output_files={out_files}", flush=True)
    else:
        print("[DOWNLOAD] m2_output_files=<empty>", flush=True)

    # Decide "input type":
    # - se abbiamo il job in RAM usiamo l'estensione del file caricato
    # - altrimenti inferiamo dal fatto che esista almeno un PDF in m2_output
    input_is_pdf = False
    if job is not None:
        doc_name = str(job.get("doc_name") or "")
        input_is_pdf = doc_name.lower().endswith(".pdf")
    else:
        input_is_pdf = any(name.lower().endswith(".pdf") for name in out_files)

    print(f"[DOWNLOAD] inferred_input_is_pdf={input_is_pdf}", flush=True)

    # In modalità auto: PDF input -> PDF; Word input -> DOCX
    effective_format = format
    if format == "auto":
        effective_format = "pdf" if input_is_pdf else "docx"
    print(f"[DOWNLOAD] effective_format={effective_format}", flush=True)

    preview_pdf = output_dir / "documento_compilato_preview.pdf"
    final_pdf = output_dir / "documento_compilato_finale.pdf"
    preview_docx = output_dir / "documento_compilato_preview.docx"
    final_docx = output_dir / "documento_compilato_finale.docx"

    if effective_format == "pdf":
        # PDF branch (serve PDF only)
        selected_pdf: Path | None = None
        if variant == "preview":
            selected_pdf = preview_pdf if preview_pdf.exists() else None
            if selected_pdf is None:
                print(f"[DOWNLOAD][PDF] preview missing at {preview_pdf}", flush=True)
                raise HTTPException(status_code=404, detail="PDF preview non trovato")
        elif variant == "final":
            selected_pdf = final_pdf if final_pdf.exists() else None
            if selected_pdf is None:
                print(f"[DOWNLOAD][PDF] final missing at {final_pdf}", flush=True)
                raise HTTPException(status_code=404, detail="PDF finale non trovato")
        else:  # any
            if preview_pdf.exists():
                selected_pdf = preview_pdf
            elif final_pdf.exists():
                selected_pdf = final_pdf
            else:
                pdf_files = sorted(output_dir.glob("*.pdf"))
                selected_pdf = pdf_files[0] if pdf_files else None

        if selected_pdf is None or not selected_pdf.exists():
            print("[DOWNLOAD][PDF] no pdf candidate found", flush=True)
            raise HTTPException(status_code=404, detail="PDF compilato non trovato")

        stem = _safe_download_stem(job) if job else job_id
        print(f"[DOWNLOAD][PDF] serving={selected_pdf.name}", flush=True)
        return FileResponse(
            path=selected_pdf,
            filename=f"{variant}_{stem}.pdf",
            media_type="application/pdf",
        )

    # DOCX branch (serve DOCX only)
    selected_docx: Path | None = None
    if variant == "preview":
        selected_docx = preview_docx if preview_docx.exists() else None
        if selected_docx is None:
            print(f"[DOWNLOAD][DOCX] preview missing at {preview_docx}", flush=True)
            raise HTTPException(status_code=404, detail="DOCX preview non trovato")
    elif variant == "final":
        selected_docx = final_docx if final_docx.exists() else None
        if selected_docx is None:
            print(f"[DOWNLOAD][DOCX] final missing at {final_docx}", flush=True)
            raise HTTPException(status_code=404, detail="DOCX finale non trovato")
    else:  # any
        if preview_docx.exists():
            selected_docx = preview_docx
        elif final_docx.exists():
            selected_docx = final_docx
        else:
            docx_files = sorted(output_dir.glob("*.docx"))
            selected_docx = docx_files[0] if docx_files else None

    if selected_docx is None or not selected_docx.exists():
        print("[DOWNLOAD][DOCX] no docx candidate found", flush=True)
        raise HTTPException(status_code=404, detail="DOCX compilato non trovato")

    stem = _safe_download_stem(job) if job else job_id
    print(f"[DOWNLOAD][DOCX] serving={selected_docx.name}", flush=True)
    return FileResponse(
        path=selected_docx,
        filename=f"{variant}_{stem}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )



@app.get("/download-mapping/{job_id}")
async def download_mapping(job_id: str, variant: str = Query(default="provvisorio", pattern="^(provvisorio|finale)$")):
    """
    Scarica il JSON di mapping del job.

    Query params:
      - variant=provvisorio -> campo_valore_provvisorio.json (default)
      - variant=finale      -> campo_valore_finale.json
    """
    output_dir = PROJECT_ROOT / "output" / job_id / "m2_output"
    if not output_dir.exists():
        raise HTTPException(status_code=404, detail="Output job non trovato (job non finito)")

    mapping_path = output_dir / ("campo_valore_provvisorio.json" if variant == "provvisorio" else "campo_valore_finale.json")
    if not mapping_path.exists():
        raise HTTPException(status_code=404, detail=f"Mapping {variant} non trovato")

    job = jobs.get(job_id)
    stem = _safe_download_stem(job) if job else job_id
    return FileResponse(
        path=mapping_path,
        filename=f"mapping_{variant}_{stem}.json",
        media_type="application/json",
    )


@app.get("/download-m1/{job_id}")
async def download_m1_output(job_id: str):
    """
    Scarica tutta la cartella m1_output del job in ZIP.
    """
    m1_dir = PROJECT_ROOT / "output" / job_id / "m1_output"

    if not m1_dir.exists():
        raise HTTPException(status_code=404, detail="Cartella m1_output non trovata")

    zip_path = m1_dir.parent / "m1_output_debug.zip"

    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for path in m1_dir.rglob("*"):
            if path.is_file():
                z.write(path, path.relative_to(m1_dir))

    job = jobs.get(job_id)
    stem = _safe_download_stem(job) if job else job_id
    return FileResponse(
        path=zip_path,
        filename=f"m1_output_{stem}.zip",
        media_type="application/zip",
    )


@app.get("/preview-pages/{job_id}")
async def preview_pages(job_id: str):
    output_dir = PROJECT_ROOT / "output" / job_id / "m2_output"
    if not output_dir.exists():
        raise HTTPException(status_code=404, detail="Output job non trovato (job non finito)")

    # --- NEW: preview da PDF se presente (caso input PDF) ---
    preview_pdf = output_dir / "documento_compilato_preview.pdf"
    final_pdf   = output_dir / "documento_compilato_finale.pdf"
    pdf_path = preview_pdf if preview_pdf.exists() else final_pdf

    if pdf_path.exists():
        print(f"[PREVIEW] job_id={job_id} output_dir={output_dir}", flush=True)
        print(f"[PREVIEW] pdf_path={pdf_path} size={pdf_path.stat().st_size}", flush=True)

        pages_b64: list[str] = []
        doc = fitz.open(str(pdf_path))
        for pg in doc:
            pix = pg.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
            pages_b64.append(base64.b64encode(pix.tobytes("png")).decode("ascii"))
        doc.close()

        return {"pages": pages_b64, "total": len(pages_b64)}
    
    preview_docx = output_dir / "documento_compilato_preview.docx"
    final_docx   = output_dir / "documento_compilato_finale.docx"
    docx_path = preview_docx if preview_docx.exists() else final_docx
    if not docx_path.exists():
        raise HTTPException(status_code=404, detail="DOCX compilato non trovato")

    print(f"[PREVIEW] job_id={job_id} output_dir={output_dir}", flush=True)
    print(f"[PREVIEW] docx_path={docx_path} size={docx_path.stat().st_size}", flush=True)

    pages_b64: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_p = Path(tmpdir)
        pdf_file = tmpdir_p / "preview.pdf"
        convert_script = PROJECT_ROOT / "m1_pipeline" / "postprocessing" / "convert_docx_to_pdf.py"

        print(f"[PREVIEW] tmpdir={tmpdir}", flush=True)
        print(f"[PREVIEW] convert_script={convert_script} exists={convert_script.exists()}", flush=True)

        # 1) Tenta Microsoft Graph -> PDF
        try:
            cmd = [
                sys.executable, str(convert_script),
                "--input-docx", str(docx_path),
                "--out-pdf", str(pdf_file),
                #"--graph-only",
            ]
            print(f"[PREVIEW][GRAPH] start cmd={cmd}", flush=True)

            res = subprocess.run(
                cmd,
                check=True,
                timeout=120,
                capture_output=True,
                text=True,
            )
            if res.stdout:
                print(f"[PREVIEW][GRAPH] stdout(last4k)=\n{res.stdout[-4000:]}", flush=True)
            if res.stderr:
                print(f"[PREVIEW][GRAPH] stderr(last4k)=\n{res.stderr[-4000:]}", flush=True)

            print(f"[PREVIEW][GRAPH] ok pdf={pdf_file} exists={pdf_file.exists()}", flush=True)
            if not pdf_file.exists():
                raise RuntimeError("Graph ha terminato senza generare il PDF atteso")

            doc = fitz.open(str(pdf_file))
            for pg in doc:
                pix = pg.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                pages_b64.append(base64.b64encode(pix.tobytes("png")).decode("ascii"))
            doc.close()

            print(f"[PREVIEW][GRAPH] rendered_pages={len(pages_b64)}", flush=True)
            return {"pages": pages_b64, "total": len(pages_b64)}

        except subprocess.CalledProcessError as e:
            out = (e.stdout or "")[-4000:]
            err = (e.stderr or "")[-4000:]
            print(f"[PREVIEW][GRAPH] FAILED rc={e.returncode}", flush=True)
            if out:
                print(f"[PREVIEW][GRAPH] stdout(last4k)=\n{out}", flush=True)
            if err:
                print(f"[PREVIEW][GRAPH] stderr(last4k)=\n{err}", flush=True)

        except Exception as e:
            print(f"[PREVIEW][GRAPH] FAILED exc={type(e).__name__}: {e}", flush=True)

        # 2) Fallback: “foto” pagine (LibreOffice -> PNG)
        try:
            lo_cmd = [
                "libreoffice",
                "--headless",
                "--convert-to",
                "png",
                "--outdir",
                tmpdir,
                str(docx_path),
            ]
            print(f"[PREVIEW][LO->PNG] start cmd={lo_cmd}", flush=True)

            res = subprocess.run(
                lo_cmd,
                check=True,
                timeout=180,
                capture_output=True,
                text=True,
            )
            if res.stdout:
                print(f"[PREVIEW][LO->PNG] stdout(last4k)=\n{res.stdout[-4000:]}", flush=True)
            if res.stderr:
                print(f"[PREVIEW][LO->PNG] stderr(last4k)=\n{res.stderr[-4000:]}", flush=True)

            png_files = sorted(tmpdir_p.glob("*.png"))
            print(f"[PREVIEW][LO->PNG] png_count={len(png_files)}", flush=True)
            if not png_files:
                raise RuntimeError("LibreOffice non ha generato PNG")

            pages_b64 = [base64.b64encode(p.read_bytes()).decode("ascii") for p in png_files]
            print(f"[PREVIEW][LO->PNG] ok pages={len(pages_b64)}", flush=True)
            return {"pages": pages_b64, "total": len(pages_b64)}

        except Exception as e:
            print(f"[PREVIEW][LO->PNG] FAILED exc={type(e).__name__}: {e}", flush=True)
            raise HTTPException(status_code=500, detail="Preview fallita: Graph KO e LibreOffice KO")



@app.get("/health")
async def health():
    """Health check."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)
