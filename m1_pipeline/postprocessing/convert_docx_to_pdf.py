import os
import shutil
import subprocess
import tempfile
import zipfile
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--input-docx", required=True)
parser.add_argument("--out-pdf", required=True)
args = parser.parse_args()


def _resolve_docx_path(path: str) -> str:
    if os.path.exists(path):
        return path
    directory = os.path.dirname(path)
    name = os.path.basename(path)
    if os.path.isdir(directory) and name.startswith("~$"):
        suffix = name[2:]
        candidates = [
            os.path.join(directory, f)
            for f in os.listdir(directory)
            if f.lower().endswith(".docx") and (not f.startswith("~$")) and f.lower().endswith(suffix.lower())
        ]
        if len(candidates) == 1 and os.path.exists(candidates[0]):
            return candidates[0]
    raise FileNotFoundError(f"DOCX non trovato: {path}")

docx_path = os.path.abspath(_resolve_docx_path(args.input_docx))
pdf_path = os.path.abspath(args.out_pdf)
WINDOWS = (os.name == "nt")

def _try_unblock(path: str) -> None:
    # Se il file arriva da internet, Word può aprirlo in Protected View o rifiutarlo.
    try:
        escaped = path.replace("'", "''")
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Unblock-File -LiteralPath '{escaped}'",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass


def _find_soffice() -> str | None:
    candidates = []
    from_path = shutil.which("soffice") or shutil.which("soffice.com") or shutil.which("libreoffice")
    if from_path:
        candidates.append(from_path)
    candidates.extend(
        [
            r"C:\Program Files\LibreOffice\program\soffice.com",
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
    )
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


def _convert_with_libreoffice(docx: str, out_pdf: str) -> bool:
    soffice = _find_soffice()
    if not soffice:
        return False

    out_dir = os.path.dirname(out_pdf)
    os.makedirs(out_dir, exist_ok=True)

    # LibreOffice salva come <basename>.pdf nella outdir.
    expected_pdf = os.path.join(out_dir, os.path.splitext(os.path.basename(docx))[0] + ".pdf")
    cmd = [
        soffice,
        "--headless",
        "--nologo",
        "--nolockcheck",
        "--nodefault",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        out_dir,
        docx,
    ]
    # Evita di riusare un PDF vecchio rimasto da un run precedente.
    if os.path.exists(out_pdf):
        os.remove(out_pdf)

    if os.path.abspath(expected_pdf) != os.path.abspath(out_pdf) and os.path.exists(expected_pdf):
        os.remove(expected_pdf)

    print(f"[DOCX2PDF] Input DOCX: {docx}", flush=True)
    print(f"[DOCX2PDF] Output PDF target: {out_pdf}", flush=True)
    print(f"[DOCX2PDF] LibreOffice expected PDF: {expected_pdf}", flush=True)

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


    if os.path.exists(expected_pdf):
        if os.path.abspath(expected_pdf) != os.path.abspath(out_pdf):
            os.replace(expected_pdf, out_pdf)
        return True

    return os.path.exists(out_pdf)



def _repack_docx(src_docx: str) -> str:
    """
    Ricrea il .docx (zip) in modo "pulito".
    A volte Word via COM rifiuta docx validi perché lo zip è non standard/ha metadata strani.
    """
    temp_dir = tempfile.mkdtemp(prefix="docx_repack_")
    extracted_dir = os.path.join(temp_dir, "extracted")
    os.makedirs(extracted_dir, exist_ok=True)

    with zipfile.ZipFile(src_docx, "r") as zin:
        zin.extractall(extracted_dir)

    repacked = os.path.join(temp_dir, "repacked.docx")
    with zipfile.ZipFile(repacked, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for root, _, files in os.walk(extracted_dir):
            for filename in files:
                full_path = os.path.join(root, filename)
                arcname = os.path.relpath(full_path, extracted_dir).replace(os.sep, "/")
                zout.write(full_path, arcname)

    return repacked


word = None
doc = None
protected_view_window = None
temp_docx = None
repacked_docx = None

if not WINDOWS:
    # Su Linux (Render/HuggingFace) non esiste win32com/pywin32: usa direttamente LibreOffice.
    try:
        if _convert_with_libreoffice(docx_path, pdf_path):
            print(f"Convertito: {docx_path} -> {pdf_path}")
            raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit(f"Conversione LibreOffice fallita. Dettaglio: {e}")
    raise SystemExit("LibreOffice non trovato o conversione fallita (soffice).")

try:
    import win32com.client  # type: ignore
except Exception as e:
    raise SystemExit(
        "Errore: manca 'pywin32'. Installa con: pip install pywin32\n"
        f"Dettaglio: {e}"
    )

try:
    _try_unblock(docx_path)
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0

    def open_doc(path: str):
        last_err = None
        # Alcune combinazioni fanno fallire Word con "file danneggiato" anche su file validi.
        open_attempts = [
            dict(
                FileName=path,
                ConfirmConversions=False,
                ReadOnly=False,
                AddToRecentFiles=False,
                Revert=False,
                NoEncodingDialog=True,
                OpenAndRepair=True,
            ),
            dict(
                FileName=path,
                ConfirmConversions=False,
                ReadOnly=False,
                AddToRecentFiles=False,
                Revert=False,
                NoEncodingDialog=True,
            ),
        ]
        for kwargs in open_attempts:
            try:
                return word.Documents.Open(**kwargs)
            except Exception as e:
                last_err = e

        if hasattr(word.Documents, "OpenNoRepairDialog"):
            try:
                return word.Documents.OpenNoRepairDialog(path, False, False, False)
            except Exception as e:
                last_err = e

        raise last_err  # type: ignore[misc]

    try:
        doc = open_doc(docx_path)
    except Exception:
        # Fallback 1: prova con Protected View (tipico dei file "bloccati")
        try:
            protected_view_window = word.ProtectedViewWindows.Open(docx_path)
            doc = protected_view_window.Edit()
        except Exception:
            # Fallback 2: copia in una cartella temporanea (spesso rimuove flag/ADS)
            temp_dir = tempfile.mkdtemp(prefix="docx_to_pdf_")
            temp_docx = os.path.join(temp_dir, "input.docx")
            shutil.copy2(docx_path, temp_docx)
            _try_unblock(temp_docx)
            try:
                doc = open_doc(temp_docx)
            except Exception:
                # Fallback 3: repack dello zip docx e riprova
                repacked_docx = _repack_docx(temp_docx)
                _try_unblock(repacked_docx)
                doc = open_doc(repacked_docx)

    wdFormatPDF = 17
    try:
        doc.SaveAs(pdf_path, FileFormat=wdFormatPDF)
    except Exception:
        # In alcuni casi SaveAs fallisce ma ExportAsFixedFormat funziona.
        wdExportFormatPDF = 17
        doc.ExportAsFixedFormat(pdf_path, wdExportFormatPDF)
except Exception:
    # Ultimo fallback: LibreOffice headless (se installato).
    try:
        if _convert_with_libreoffice(docx_path, pdf_path):
            print(f"Convertito: {docx_path} -> {pdf_path}")
            raise SystemExit(0)
    except SystemExit:
        raise
    except Exception:
        pass
    raise
finally:
    if protected_view_window is not None:
        try:
            protected_view_window.Close()
        except Exception:
            pass
    if doc is not None:
        doc.Close(False)
    if word is not None:
        word.Quit()
    if temp_docx is not None:
        try:
            shutil.rmtree(os.path.dirname(temp_docx), ignore_errors=True)
        except Exception:
            pass
    if repacked_docx is not None:
        try:
            shutil.rmtree(os.path.dirname(repacked_docx), ignore_errors=True)
        except Exception:
            pass

print(f"Convertito: {docx_path} -> {pdf_path}")
