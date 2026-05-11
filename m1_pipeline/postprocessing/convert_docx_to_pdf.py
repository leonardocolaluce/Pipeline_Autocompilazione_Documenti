import os
import shutil
import subprocess
import tempfile
import zipfile
import argparse
import time
import uuid
import requests
import msal

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

def _convert_with_microsoft_graph(docx: str, out_pdf: str) -> bool:
    client_id = "d9bd369d-1fb5-43d0-a480-e90640c226b6"
    client_secret = "QQj8Q~kZjM4rIYYr1RVXZ6~7XD_drYrPNWXvUbhh"
    refresh_token = "1.AUsAU6FlNwZJVUSbjJ9j-2RrpZ02vdm1H9BDpIDpBkDCJrYAAPJLAA.BQABAwIAAAADAOz_BQD0_0V2b1N0c0FydGlmYWN0cwIAAAAAAM1bJGe4a8yJY0M9vOnotv3eK-ITe_7xDdtbUIuQH0y14tx5ysfxcTpstPfmsoYzt98fgr8QNFfFALMwghbXBHrdUFWUCt3fD1OZvuvvbHjYXml0u1b-9EdOX5R9pUnmGMkfm_plWDCOfS6xBQsj40ZhZznhNF2NAD7k0TcxfUyH9M9YRcyNvHqQ1-W7xlYMNmy3GuJ7r54Uw5M6-ksdbgYr3WBLrPfaj_uaEOuKC0eWk1v-BBXaweO9dWw6qOo7sVLwk0yq0ASN-E2fs_es1Wlj5oSi8Iy6M53vn1PUC1VSOEILnOP-pD90UfB-m3W5RlELGhj2xNLmKgLDS7UwmZsZb0MdZS9gwxkjZ1YD3VvnPmnsoaow050DH2FltfL582MtAb4zKnpt9f6lMZUw68aAq3TINDlxJgC2OfWNnEKBBiOYvc-oZ7i2gJXdY0atXij1gUozgkxYUfCrHZWDrD6znJ6NCDnSIeq6Zfb8TVidX5j63PnrOu0sQDRmwTQmt_PCLIIW5C87vBowBIZL0b-AKyIveORDR9hoqPBbMx7Krnke3YTBO-OaRjWHNQdZvvANTOITON-BQn5OPz-UfrdLBSCFpQrQGFEWB6vNxx0xXFYjeaUEOT6lnZCh0zA9N6anl3aHOyajzGPj0Vh79THOY6h2l7stI6z2jmwDFlcM-hKIX5B6-RNr5Ut1dblUTwIa61KBFwC_pFUGNDJEfVyuC9DzCSdTmNNMkTQ6eaR7nBhdZiLXlKn3PS5klRK8ebgjKhHvOir3d3zXrHNZMqg0XdVXVUWDec4zm_MZzdDhjYDridp4UmBZbP3gr3_OJPLfKjNrufq7uzMKG10o2swfK1iFNd5xOyJN9dS3FiF4fXiJfq1YbFXWtTaLLunIEu2CYNOShp0tKH1I3mM7pLhPBIxGX5lgvvG6eM1NdM0djechVGVBAmOI02hLIpksKx88gTwzCP26yEC_7lpg6n5Vtzv6Qxw7wNQuVkkLeLkb45BXua1wxoEyNVyA6oPuUzooxt-hfPtokln0HCDmH3XxGumP4Kfv7n9o6jLd1806Ma172vCB6ENpJRfTkiz-G7YRaXSAjw6WkmVB-qbGh_zbv0Ep2ZWciGtNj_CSEzDq_rflXrGhDOD-BYrwkvU74BuquRUU6AV5Nduc6xnZaN61PxBqrtXc1OF3ynQbG3oO5AGnryfcv2YSff4U0OQt2VzeL5nqTyDtHA744Hh4Vg4W-45rlmSGDTdC1UqOEWK8WU5He5ICA3gQyjvwFEWwOGtQFzbCJVroxh2C415uhxUtzYE0Q09H1YitWikpMQrzU3oU2uOkY_BjTO9g-7jhl2hkQpkP7T4DaoUk4kKsNkY-bnxE0ATfxuXENsbtFdrfjNeNse5k12NNCNIGjf_TfAJBIma3Tk2d4rSZu6pI6d08_ftNhRMnawEkAm-Q5o939zUxbcPQi1CTCY42RYK6y6tm8mMQfCFlJx0djT19jc4ue86Ab9fFkH-a1KFa1tDD39TasTKwZxmveCJK0QA-_VdMVZzFUHmkhCB1IhVDITbCmBDI3aHl0dMqrPPY7_bFrNiyEOyCjdW6HjqjUkGji6BIHbJ9bwWl-U7wx0iOZY7fUDorWbTpFoHNJRSveAw-dt0neBjiHjaVDQg5Wy0zWQ5dexr_y6z2VW766LSpV21wSx5Issr1Ow-vcnEyCpPeVz3vBbvpWSXOgWs4AnsfHODE5sXD8NUOrIUfIXk4peHprkKFT89riaRsqrcnopDRo3Bjn8mj10grcI-4FtnbmbjtFKLfHSkxZ5MBaFYy2-MY1RS1HPpTTwRshVGJV9Tp2g5L9h6fN2WCm6AeIjurkowIfGs"
    tenant_id = "3765a153-4906-4455-9b8c-9f63fb646ba5"
    
    authority = f"https://login.microsoftonline.com/{tenant_id}"

    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=authority,
    )

    token_result = app.acquire_token_by_refresh_token(
        refresh_token,
        scopes=["Files.ReadWrite"]
    )

    if "access_token" not in token_result:
        raise RuntimeError(f"Microsoft Graph auth failed: {token_result}")

    access_token = token_result["access_token"]

    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    filename = f"temp_convert_{uuid.uuid4().hex}.docx"

    upload_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{filename}:/content"

    with open(docx, "rb") as f:
        upload_response = requests.put(upload_url, headers=headers, data=f)

    if upload_response.status_code not in (200, 201):
        raise RuntimeError(f"Upload DOCX failed: {upload_response.status_code} {upload_response.text}")

    item = upload_response.json()
    item_id = item["id"]

    try:
        pdf_url = f"https://graph.microsoft.com/v1.0/me/drive/items/{item_id}/content?format=pdf"

        pdf_response = requests.get(pdf_url, headers=headers, allow_redirects=True)

        if pdf_response.status_code != 200:
            raise RuntimeError(f"PDF conversion failed: {pdf_response.status_code} {pdf_response.text}")

        os.makedirs(os.path.dirname(out_pdf), exist_ok=True)

        with open(out_pdf, "wb") as f:
            f.write(pdf_response.content)

        return os.path.exists(out_pdf) and os.path.getsize(out_pdf) > 0

    finally:
        delete_url = f"https://graph.microsoft.com/v1.0/me/drive/items/{item_id}"
        requests.delete(delete_url, headers=headers)

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
    try:
        if _convert_with_microsoft_graph(docx_path, pdf_path):
            print(f"Convertito con Microsoft Graph: {docx_path} -> {pdf_path}")
            raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[DOCX2PDF] Microsoft Graph fallito: {e}", flush=True)

    try:
        if _convert_with_libreoffice(docx_path, pdf_path):
            print(f"Convertito con LibreOffice fallback: {docx_path} -> {pdf_path}")
            raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit(f"Conversione fallita sia con Microsoft Graph sia con LibreOffice. Dettaglio: {e}")

    raise SystemExit("Conversione fallita.")

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
