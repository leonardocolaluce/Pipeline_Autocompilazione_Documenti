from __future__ import annotations

from pathlib import Path
import os
import subprocess


# === ADOBE CREDS (HARDCODED) ===
# Incolla qui le credenziali Adobe (NON consigliato per repo/git, ma richiesto da te).
ADOBE_CREDENTIALS_JSON_PATH = str(Path(__file__).resolve().parent / "pdfservices-api-credentials.json")

# Se la tua integrazione Adobe richiede altri campi, incollali qui e verranno passati allo script.
ADOBE_ORG_ID = "INCOLLA_QUI_ORG_ID"              # opzionale
ADOBE_ACCOUNT_ID = "INCOLLA_QUI_ACCOUNT_ID"      # opzionale
ADOBE_PRIVATE_KEY = "INCOLLA_QUI_PRIVATE_KEY"    # opzionale (se usi JWT/PKI)
# ===============================


def convert_pdf_to_docx_adobe(pdf_in: str | Path, docx_out: str | Path) -> None:
    """
    Converte PDF -> DOCX usando Adobe API tramite uno script/CLI esterno.

    Requisito:
      - Env var ADOBE_PDF2DOCX_CMD deve contenere il comando da eseguire, es:
          ADOBE_PDF2DOCX_CMD="python3 /opt/adobe/adobe_pdf2docx.py"
        Lo script deve accettare:
          --input-pdf <path> --out-docx <path>

    Le credenziali hardcodate sopra vengono passate allo script via env:
      ADOBE_CLIENT_ID, ADOBE_CLIENT_SECRET, ADOBE_ORG_ID, ADOBE_ACCOUNT_ID, ADOBE_PRIVATE_KEY
    """
    pdf_in = Path(pdf_in)
    docx_out = Path(docx_out)

    if not pdf_in.exists() or pdf_in.suffix.lower() != ".pdf":
        raise FileNotFoundError(f"PDF input non trovato (o non .pdf): {pdf_in}")

    cmd = os.getenv("ADOBE_PDF2DOCX_CMD", "").strip()
    if not cmd:
        raise RuntimeError(
            "Env var ADOBE_PDF2DOCX_CMD non impostata.\n"
            "Esempio: ADOBE_PDF2DOCX_CMD=\"python3 /percorso/adobe_pdf2docx.py\""
        )

    docx_out.parent.mkdir(parents=True, exist_ok=True)

    child_env = os.environ.copy()
    child_env["ADOBE_CREDENTIALS_JSON_PATH"] = ADOBE_CREDENTIALS_JSON_PATH
    if ADOBE_ORG_ID:
        child_env["ADOBE_ORG_ID"] = ADOBE_ORG_ID
    if ADOBE_ACCOUNT_ID:
        child_env["ADOBE_ACCOUNT_ID"] = ADOBE_ACCOUNT_ID
    if ADOBE_PRIVATE_KEY:
        child_env["ADOBE_PRIVATE_KEY"] = ADOBE_PRIVATE_KEY

    argv = cmd.split() + ["--input-pdf", str(pdf_in), "--out-docx", str(docx_out)]
    res = subprocess.run(argv, capture_output=True, text=True, env=child_env)

    if res.returncode != 0:
        raise RuntimeError(
            "Adobe PDF->DOCX fallita.\n"
            f"cmd={argv}\n"
            f"rc={res.returncode}\n"
            f"stdout={((res.stdout or '')[-2000:]).strip()}\n"
            f"stderr={((res.stderr or '')[-2000:]).strip()}"
        )

    if not docx_out.exists():
        raise FileNotFoundError(f"Adobe ha terminato senza creare DOCX atteso: {docx_out}")
