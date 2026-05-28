import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-pdf", required=True)
    parser.add_argument("--out-docx", required=True)
    args = parser.parse_args()

    input_pdf = Path(args.input_pdf).resolve()
    out_docx = Path(args.out_docx).resolve()
    out_docx.parent.mkdir(parents=True, exist_ok=True)

    creds_path = os.getenv("ADOBE_CREDENTIALS_JSON_PATH", "").strip()
    if not creds_path:
        raise SystemExit("ADOBE_CREDENTIALS_JSON_PATH non impostata (deve puntare a pdfservices-api-credentials.json).")
    creds_path = str(Path(creds_path).resolve())
    if not Path(creds_path).exists():
        raise SystemExit(f"File credenziali non trovato: {creds_path}")

    if not input_pdf.exists() or input_pdf.suffix.lower() != ".pdf":
        raise SystemExit(f"PDF input non valido: {input_pdf}")

    # Adobe PDF Services SDK (pip: pdfservices-sdk)
    try:
        from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
        from adobe.pdfservices.operation.pdf_services import PDFServices
        from adobe.pdfservices.operation.io.cloud_asset import CloudAsset
        from adobe.pdfservices.operation.io.stream_asset import StreamAsset
        from adobe.pdfservices.operation.pdfjobs.jobs.export_pdf_job import ExportPDFJob
        from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_params import ExportPDFParams
        from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_target_format import ExportPDFTargetFormat
    except Exception as e:
        raise SystemExit(f"Import SDK Adobe fallito: {e}")

    # Read client_id/client_secret from credentials JSON
    creds = json.loads(Path(creds_path).read_text(encoding="utf-8"))
    cc = creds.get("client_credentials") or {}
    client_id = cc.get("client_id")
    client_secret = cc.get("client_secret")
    if not client_id or not client_secret:
        raise SystemExit(f"Credenziali non valide nel JSON: manca client_id/client_secret in {creds_path}")

    credentials = ServicePrincipalCredentials(client_id=client_id, client_secret=client_secret)
    pdf_services = PDFServices(credentials=credentials)

    # Upload PDF
    with open(input_pdf, "rb") as f:
        input_stream = f.read()
    input_asset: CloudAsset = pdf_services.upload(input_stream=input_stream, mime_type="application/pdf")

    # Export PDF -> DOCX
    export_params = ExportPDFParams(target_format=ExportPDFTargetFormat.DOCX)
    job = ExportPDFJob(input_asset=input_asset, export_pdf_params=export_params)
    location = pdf_services.submit(job)
    response = pdf_services.get_job_result(location)

    # Download result
    result_asset = response.get_result().get_asset()
    stream_asset: StreamAsset = pdf_services.get_content(result_asset)

    with open(out_docx, "wb") as f:
        f.write(stream_asset.get_input_stream())

    if not out_docx.exists() or out_docx.stat().st_size == 0:
        raise SystemExit(f"Conversione completata ma DOCX vuoto/non creato: {out_docx}")

    print(f"OK: {input_pdf} -> {out_docx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
