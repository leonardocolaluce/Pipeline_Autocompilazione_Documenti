from __future__ import annotations

from pathlib import Path
import os
import subprocess


def convert_pdf_to_docx_adobe(pdf_in: str | Path, docx_out: str | Path) -> None:
    """
    Converte PDF -> DOCX usando Adobe API.

    Implementazione MINIMA: chiama uno script/CLI esterno (che metti tu) per non
    hardcodare qui i dettagli delle API.

    Richiede env var:
      - ADOBE_PDF2DOCX_CMD: comando da eseguire (es: python, node, o binario)
    Lo script esterno deve accettare:
      --input-pdf <path> --out-docx <path>
    """
    pdf_in = Path(pdf_in)
    docx_out = Path(docx_out)

    if not pdf_in.exists() or pdf_in.suffix.lower() != ".pdf":
        raise FileNotFoundError(f"PDF input non trovato (o non .pdf): {pdf_in}")

    cmd = os.getenv("ADOBE_PDF2DOCX_CMD", "").strip()
    if not cmd:
        raise RuntimeError("Env var ADOBE_PDF2DOCX_CMD non impostata (es: 'python3 adobe_pdf2docx.py').")

    docx_out.parent.mkdir(parents=True, exist_ok=True)

    # Esempio: ADOBE_PDF2DOCX_CMD="python3 /path/adobe_pdf2docx.py"
    # Splittiamo in argv in modo semplice (spazi). Se ti serve robustezza, usa una lista fissa.
    argv = cmd.split() + ["--input-pdf", str(pdf_in), "--out-docx", str(docx_out)]

    res = subprocess.run(argv, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            "Adobe PDF->DOCX fallita.\n"
            f"cmd={argv}\n"
            f"rc={res.returncode}\n"
            f"stdout={res.stdout[-2000:]}\n"
            f"stderr={res.stderr[-2000:]}"
        )

    if not docx_out.exists():
        raise FileNotFoundError(f"Adobe ha terminato senza creare DOCX atteso: {docx_out}")
