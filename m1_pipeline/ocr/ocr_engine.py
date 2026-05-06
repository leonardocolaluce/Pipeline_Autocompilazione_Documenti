import json
import subprocess
import sys
from pathlib import Path
from typing import List, Dict, Any

# Percorso assoluto all'interprete Python 3.11 con PaddleOCR
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PADDLE_PYTHON = _PROJECT_ROOT / "paddle311" / "Scripts" / "python.exe"
_WORKER_SCRIPT = Path(__file__).resolve().parent / "paddle_worker.py"


def run_ocr(pdf_path: str) -> List[Dict[str, Any]]:
    """
    Esegue OCR su un PDF scansionato tramite PaddleOCR (subprocess Python 3.11).

    Args:
        pdf_path: Percorso al file PDF foto da elaborare.

    Returns:
        Lista di blocchi nel formato standard:
        [{"text": "...", "bbox": [x, y, w, h], "page": 1, "confidence": 0.95, "source": "ocr"}, ...]

    Raises:
        RuntimeError: Se il subprocess fallisce o restituisce un errore.
        FileNotFoundError: Se paddle311 o il worker non vengono trovati.
    """
    if not _PADDLE_PYTHON.exists():
        raise FileNotFoundError(
            f"Interprete paddle311 non trovato: {_PADDLE_PYTHON}\n"
            "Assicurati che il venv paddle311 esista nella cartella del progetto."
        )

    if not _WORKER_SCRIPT.exists():
        raise FileNotFoundError(f"Worker script non trovato: {_WORKER_SCRIPT}")

    try:
        result = subprocess.run(
            [str(_PADDLE_PYTHON), str(_WORKER_SCRIPT), str(pdf_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,  # 5 minuti massimo
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("OCR timeout: elaborazione superato il limite di 5 minuti.")
    except Exception as e:
        raise RuntimeError(f"Errore avvio subprocess OCR: {e}")

    if not result.stdout.strip():
        stderr_preview = result.stderr.strip()[:500] if result.stderr else "(nessun output)"
        raise RuntimeError(f"Subprocess OCR non ha prodotto output.\nStderr: {stderr_preview}")

    # Cerca l'ultima riga che inizia con '[' o '{' (ignora i log di PaddleOCR su stdout)
    json_line = None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("[") or line.startswith("{"):
            json_line = line
            break

    if json_line is None:
        raise RuntimeError(
            f"Nessun JSON trovato nell'output del subprocess OCR.\n"
            f"Stdout: {result.stdout[:500]}"
        )

    try:
        data = json.loads(json_line)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON non valido dall'OCR worker: {e}\nRiga: {json_line[:200]}")

    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"Errore dal worker OCR: {data['error']}")

    if not isinstance(data, list):
        raise RuntimeError(f"Formato JSON inatteso dal worker OCR: {type(data)}")

    return data
