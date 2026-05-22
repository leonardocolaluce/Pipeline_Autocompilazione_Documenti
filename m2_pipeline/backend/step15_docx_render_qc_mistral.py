from __future__ import annotations
from PIL import Image
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib import request

import fitz  # PyMuPDF

from .step00_config import MISTRAL_API_KEY, MISTRAL_MODEL

def _crop_middle_area(
    *,
    input_png_path: Path,
    output_png_path: Path,
) -> None:
    img = Image.open(input_png_path)
    width, height = img.size

    y1 = int(height * 0.15)
    y2 = int(height * 0.75)

    cropped = img.crop((0, y1, width, y2))
    output_png_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(output_png_path)

def _encode_image_data_uri(image_path: Path) -> str:
    data = image_path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    mime = "image/png"
    if image_path.suffix.lower() in {".jpg", ".jpeg"}:
        mime = "image/jpeg"
    elif image_path.suffix.lower() == ".webp":
        mime = "image/webp"
    return f"data:{mime};base64,{b64}"


QC_SYSTEM_PROMPT  = """
Check only if values are written in the correct semantic row near their label.
Set good=false if a field contains the wrong type of information because values shifted into another row.
Examples of WRONG formatting: a birth date written inside the "Via" field, a full name written where a date should be, an email written in the phone field, a city written in the codice fiscale field, or a phone number written in the email field.
If fields are visually shifted and labels no longer match the correct value type, return good=false.

Examples:
"nato il" -> "Mario Rossi" = false
"Via" -> "22/01/1998" = false
"telefono" -> "test@gmail.com" = false
"Codice fiscale" -> "ROMA" = false
"e-mail" -> "3341983810" = false

Return ONLY valid JSON with this exact schema:
{"good": true|false, "confidence": 0.0-1.0}
"""


def _call_mistral_vision(*, api_key: str, model: str, image_path: Path, timeout_sec: int = 180) -> dict[str, Any]:
    api_url = os.getenv("MISTRAL_API_URL", "https://api.mistral.ai/v1/chat/completions").strip()
    body = {
        "model": model,
        "temperature": 0,
        "top_p": 1,
        "messages": [
            {"role": "system", "content": QC_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Valuta questa pagina e rispondi con il JSON richiesto."},
                    {"type": "image_url", "image_url": _encode_image_data_uri(image_path)},
                ],
            },
        ],
    }

    req = request.Request(
        api_url,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


def _coerce_qc_json(text: str) -> dict[str, Any]:
    """
    Accept either strict JSON-only responses or responses that contain JSON as a substring.
    """
    text = (text or "").strip()
    if not text:
        return {"good": True, "confidence": 0.0, "reason": "empty_response"}

    # First attempt: strict JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # Fallback: extract first JSON object by brace balancing.
    start = text.find("{")
    if start < 0:
        return {"good": True, "confidence": 0.0, "reason": "no_json_found"}

    chunk = text[start:]
    depth = 0
    in_string = False
    escaped = False
    end_idx = None
    for idx, ch in enumerate(chunk):
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = idx + 1
                break

    if end_idx is None:
        return {"good": True, "confidence": 0.0, "reason": "truncated_json"}

    candidate = chunk[:end_idx].strip()
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
    except Exception:
        import re

        good_match = re.search(r'"?good"?\s*[:=]\s*(true|false)', text, re.I)
        conf_match = re.search(r'"?confidence"?\s*[:=]\s*([01](?:\.\d+)?)', text, re.I)

        if good_match:
            good = good_match.group(1).lower() == "true"
            confidence = float(conf_match.group(1)) if conf_match else 0.0
            return {
                "good": good,
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": "recovered_from_invalid_json",
            }

        return {"good": True, "confidence": 0.0, "reason": "invalid_json"}

    return {"good": True, "confidence": 0.0, "reason": "unknown_parse_state"}


def _convert_docx_to_pdf(*, docx_path: Path, pdf_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    convert_script = project_root / "m1_pipeline" / "postprocessing" / "convert_docx_to_pdf.py"
    if not convert_script.exists():
        raise FileNotFoundError(f"convert_docx_to_pdf.py non trovato: {convert_script}")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, str(convert_script), "--input-docx", str(docx_path), "--out-pdf", str(pdf_path)],
        check=True,
        timeout=180,
    )
    if not pdf_path.exists() or pdf_path.stat().st_size <= 0:
        raise RuntimeError(f"Conversione DOCX->PDF fallita: pdf non generato: {pdf_path}")


def _render_first_page_png(*, pdf_path: Path, png_path: Path, zoom: float = 2.0) -> None:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    try:
        if doc.page_count <= 0:
            raise RuntimeError(f"PDF vuoto: {pdf_path}")
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(float(zoom), float(zoom)), alpha=False)
        png_path.write_bytes(pix.tobytes("png"))
    finally:
        doc.close()


def qc_docx_render_first_page(
    *,
    compiled_docx_path: str | Path,
    out_json_path: str | Path,
    work_dir: str | Path | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """
    Renders the first page of a compiled DOCX and asks Mistral Vision whether the overlay text
    is vertically shifted too low (i.e., falls into the next row).
    """
    docx_path = Path(compiled_docx_path).resolve()
    if not docx_path.exists() or docx_path.suffix.lower() != ".docx":
        raise FileNotFoundError(f"DOCX compilato non trovato o non .docx: {docx_path}")

    out_json = Path(out_json_path).resolve()
    base_dir = Path(work_dir).resolve() if work_dir else out_json.parent
    base_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = base_dir / "qc_preview_first_page.pdf"
    png_path = base_dir / "qc_preview_first_page.png"

    _convert_docx_to_pdf(docx_path=docx_path, pdf_path=pdf_path)
    _render_first_page_png(pdf_path=pdf_path, png_path=png_path, zoom=2.0)
    crop_png_path = base_dir / "qc_preview_first_page_middle.png"

    _crop_middle_area(
       input_png_path=png_path,
       output_png_path=crop_png_path,
    )

    effective_key = (api_key or os.getenv("MISTRAL_API_KEY") or MISTRAL_API_KEY or "").strip()
    if not effective_key:
        raise RuntimeError("MISTRAL_API_KEY mancante (env o step00_config).")
    effective_model = (model or os.getenv("MISTRAL_MODEL") or MISTRAL_MODEL or "").strip() or "mistral-medium-2508"

    raw = _call_mistral_vision(api_key=effective_key, model=effective_model, image_path=crop_png_path)
    text = _extract_text(raw)
    qc = _coerce_qc_json(text)

    good = bool(qc.get("good"))
    try:
        confidence = float(qc.get("confidence", 0.0) or 0.0)
    except Exception:
        confidence = 0.0
    reason = str(qc.get("reason", "") or "").strip() or "no_reason"

    payload = {
        "status": "ok",
        "good": good,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": reason,
        "model": effective_model,
        "compiled_docx": str(docx_path),
        "pdf_first_page": str(pdf_path),
        "png_first_page": str(png_path),
        "png_crop": str(crop_png_path),
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mistral QC: render first page and judge vertical misalignment.")
    ap.add_argument("--docx", required=True, help="Path DOCX compilato (provvisorio).")
    ap.add_argument("--out-json", required=True, help="Output JSON path.")
    ap.add_argument("--work-dir", default="", help="Work directory for intermediate PDF/PNG.")
    ap.add_argument("--model", default="", help="Override model (default: MISTRAL_MODEL).")
    args = ap.parse_args(argv)

    qc_docx_render_first_page(
        compiled_docx_path=args.docx,
        out_json_path=args.out_json,
        work_dir=(args.work_dir or None),
        model=(args.model or None),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

