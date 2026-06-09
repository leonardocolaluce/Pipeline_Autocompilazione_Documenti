#!/usr/bin/env python3
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import cv2
    import fitz
    import numpy as np
except ImportError as exc:
    print(
        "Missing dependency. Install with: pip install PyMuPDF opencv-python-headless numpy",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


SUPPORTED = {".pdf", ".doc", ".docx"}
DEFAULT_DPI = 140
MIN_SIDE_PT = 4.0
MAX_SIDE_PT = 24.0
ASPECT_TOLERANCE = 0.38
MERGE_DISTANCE_PT = 3.0
CONTEXT_RADIUS_PT = 95.0


@dataclass
class BoxCandidate:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    source: str
    confidence: float

    @property
    def rect(self) -> fitz.Rect:
        return fitz.Rect(self.x0, self.y0, self.x1, self.y1)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)


def slugify(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return clean.strip("._") or "document"


def windows_path(path: Path) -> str:
    try:
        result = subprocess.run(
            ["wslpath", "-w", str(path.resolve())],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception:
        return str(path.resolve())


def convert_word_to_pdf(input_path: Path, converted_dir: Path) -> Path:
    converted_dir.mkdir(parents=True, exist_ok=True)
    target_pdf = converted_dir / f"{input_path.stem}.pdf"
    if target_pdf.exists() and target_pdf.stat().st_mtime >= input_path.stat().st_mtime:
        return target_pdf

    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".ps1",
        delete=False,
        encoding="utf-8",
        dir=converted_dir,
    ) as script:
        script_path = Path(script.name)
        script.write(
            """
$ErrorActionPreference = "Stop"
$inputPath = $args[0]
$outputDir = $args[1]
$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
try {
  $doc = $word.Documents.Open($inputPath, $false, $true)
  $base = [System.IO.Path]::GetFileNameWithoutExtension($inputPath)
  $outputPath = [System.IO.Path]::Combine($outputDir, $base + ".pdf")
  $doc.ExportAsFixedFormat($outputPath, 17)
  $doc.Close($false)
  Write-Output $outputPath
} finally {
  $word.Quit()
  [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null
}
"""
        )

    try:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            windows_path(script_path),
            windows_path(input_path),
            windows_path(converted_dir),
        ]
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    except Exception as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = f" stdout={exc.stdout.strip()} stderr={exc.stderr.strip()}"
        raise RuntimeError(f"Word to PDF conversion failed for {input_path.name}: {exc}{detail}") from exc
    finally:
        script_path.unlink(missing_ok=True)

    if not target_pdf.exists():
        raise RuntimeError(f"Word conversion did not create {target_pdf}")
    return target_pdf


def extract_docx_checkbox_hints(path: Path) -> list[str]:
    if path.suffix.lower() != ".docx":
        return []
    hints: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.startswith("word/") or not name.endswith(".xml"):
                    continue
                xml = archive.read(name).decode("utf-8", errors="ignore")
                if any(token in xml for token in ("<w:checkBox", "<w14:checkbox", "☐", "□", "☑", "☒")):
                    hints.append(name)
    except Exception:
        return []
    return hints


def detect_from_pdf_drawings(page: fitz.Page) -> list[BoxCandidate]:
    candidates: list[BoxCandidate] = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if not rect:
            continue
        width = rect.width
        height = rect.height
        if is_checkbox_size(width, height):
            candidates.append(
                BoxCandidate(
                    page=page.number + 1,
                    x0=rect.x0,
                    y0=rect.y0,
                    x1=rect.x1,
                    y1=rect.y1,
                    source="pdf_vector",
                    confidence=0.92,
                )
            )
    return candidates


def is_checkbox_size(width_pt: float, height_pt: float) -> bool:
    if width_pt < MIN_SIDE_PT or height_pt < MIN_SIDE_PT:
        return False
    if width_pt > MAX_SIDE_PT or height_pt > MAX_SIDE_PT:
        return False
    side = max(width_pt, height_pt)
    return abs(width_pt - height_pt) / side <= ASPECT_TOLERANCE


def detect_from_page_image(page: fitz.Page, dpi: int) -> list[BoxCandidate]:
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(blurred, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates: list[BoxCandidate] = []
    min_area = (MIN_SIDE_PT * scale * 0.75) ** 2
    max_area = (MAX_SIDE_PT * scale * 1.4) ** 2
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area or area > max_area:
            continue
        perimeter = cv2.arcLength(contour, True)
        if perimeter <= 0:
            continue
        approx = cv2.approxPolyDP(contour, 0.04 * perimeter, True)
        x, y, width, height = cv2.boundingRect(approx)
        width_pt = width / scale
        height_pt = height / scale
        if len(approx) < 4 or len(approx) > 8:
            continue
        if not is_checkbox_size(width_pt, height_pt):
            continue

        box_area = max(width * height, 1)
        rectangularity = area / box_area
        if rectangularity < 0.18:
            continue

        rect = fitz.Rect(x / scale, y / scale, (x + width) / scale, (y + height) / scale)
        if rect.is_empty or rect.width <= 0 or rect.height <= 0:
            continue
        candidates.append(
            BoxCandidate(
                page=page.number + 1,
                x0=rect.x0,
                y0=rect.y0,
                x1=rect.x1,
                y1=rect.y1,
                source="image_contour",
                confidence=0.72,
            )
        )
    return candidates


def detect_checkbox_glyphs(page: fitz.Page) -> list[BoxCandidate]:
    glyphs = {"☐", "☑", "☒", "□", "■", "❑", "❒", "\uf08a", "\uf0a3", "\uf0fe"}
    candidates: list[BoxCandidate] = []
    raw = page.get_text("rawdict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    if char.get("c") not in glyphs:
                        continue
                    rect = tighten_glyph_checkbox_rect(fitz.Rect(char["bbox"]))
                    if rect.width > 1 and rect.height > 1:
                        candidates.append(
                            BoxCandidate(
                                page=page.number + 1,
                                x0=rect.x0,
                                y0=rect.y0,
                                x1=rect.x1,
                                y1=rect.y1,
                                source="unicode_glyph",
                                confidence=0.95,
                            )
                        )
    return candidates


def tighten_glyph_checkbox_rect(rect: fitz.Rect) -> fitz.Rect:
    if rect.width <= 0 or rect.height <= 0:
        return rect
    side = min(rect.width, rect.height)
    center_x = (rect.x0 + rect.x1) / 2.0
    center_y = (rect.y0 + rect.y1) / 2.0
    return fitz.Rect(
        center_x - side / 2.0,
        center_y - side / 2.0,
        center_x + side / 2.0,
        center_y + side / 2.0,
    )


def detect_bracket_checkboxes(page: fitz.Page) -> list[BoxCandidate]:
    candidates: list[BoxCandidate] = []
    raw = page.get_text("rawdict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            chars = []
            for span in line.get("spans", []):
                chars.extend(span.get("chars", []))
            chars.sort(key=lambda char: char["bbox"][0])
            for index, char in enumerate(chars):
                if char.get("c") != "[":
                    continue
                rect = fitz.Rect(char["bbox"])
                content = []
                for next_char in chars[index + 1 : index + 8]:
                    next_rect = fitz.Rect(next_char["bbox"])
                    if next_rect.x0 - rect.x0 > MAX_SIDE_PT * 2.2:
                        break
                    rect |= next_rect
                    if next_char.get("c") == "]":
                        inside = "".join(content).strip()
                        if inside in {"", "_", "__", "___"} and is_checkbox_size(rect.width, rect.height):
                            candidates.append(
                                BoxCandidate(
                                    page=page.number + 1,
                                    x0=rect.x0,
                                    y0=rect.y0,
                                    x1=rect.x1,
                                    y1=rect.y1,
                                    source="text_bracket",
                                    confidence=0.88,
                                )
                            )
                        break
                    content.append(next_char.get("c", ""))
    return candidates


def merge_candidates(candidates: Iterable[BoxCandidate]) -> list[BoxCandidate]:
    merged: list[BoxCandidate] = []
    for candidate in sorted(candidates, key=lambda c: (c.page, c.y0, c.x0, -c.confidence)):
        duplicate_index = None
        for index, existing in enumerate(merged):
            if existing.page != candidate.page:
                continue
            if rects_close(existing.rect, candidate.rect):
                duplicate_index = index
                break
        if duplicate_index is None:
            merged.append(candidate)
            continue
        existing = merged[duplicate_index]
        best = existing if existing.confidence >= candidate.confidence else candidate
        union = existing.rect | candidate.rect
        merged[duplicate_index] = BoxCandidate(
            page=best.page,
            x0=union.x0,
            y0=union.y0,
            x1=union.x1,
            y1=union.y1,
            source=best.source if existing.source == candidate.source else f"{existing.source}+{candidate.source}",
            confidence=max(existing.confidence, candidate.confidence),
        )
    return merged


def rects_close(a: fitz.Rect, b: fitz.Rect) -> bool:
    if a.intersects(b):
        inter = a & b
        smaller = min(a.get_area(), b.get_area())
        if smaller > 0 and inter.get_area() / smaller > 0.35:
            return True
    acx, acy = ((a.x0 + a.x1) / 2.0, (a.y0 + a.y1) / 2.0)
    bcx, bcy = ((b.x0 + b.x1) / 2.0, (b.y0 + b.y1) / 2.0)
    return math.hypot(acx - bcx, acy - bcy) <= MERGE_DISTANCE_PT


def rect_to_json(rect: fitz.Rect) -> dict:
    return {
        "x0": round(rect.x0, 2),
        "y0": round(rect.y0, 2),
        "x1": round(rect.x1, 2),
        "y1": round(rect.y1, 2),
    }


def right_text_on_same_line(page: fitz.Page, box: BoxCandidate, page_boxes: list[BoxCandidate]) -> str:
    box_rect = box.rect
    box_center_y = (box_rect.y0 + box_rect.y1) / 2.0
    line_tolerance = max(5.0, box_rect.height * 0.85)
    right_limit = page.rect.x1

    for other in page_boxes:
        if other is box:
            continue
        other_rect = other.rect
        other_center_y = (other_rect.y0 + other_rect.y1) / 2.0
        if abs(other_center_y - box_center_y) <= line_tolerance and other_rect.x0 > box_rect.x1:
            right_limit = min(right_limit, other_rect.x0)

    words = []
    for raw_word in page.get_text("words"):
        word_rect = fitz.Rect(raw_word[:4])
        word_center_y = (word_rect.y0 + word_rect.y1) / 2.0
        if abs(word_center_y - box_center_y) > line_tolerance:
            continue
        if word_rect.x1 <= box_rect.x1 + 1.0:
            continue
        if word_rect.x0 >= right_limit - 1.0:
            continue
        words.append((word_rect.x0, raw_word[4]))

    words.sort(key=lambda item: item[0])
    return clean_right_text(" ".join(word for _, word in words))


def clean_right_text(text: str) -> str:
    return re.sub(r"^[\s☐☑☒□■❑❒\uf08a\uf0a3\uf0fe\[\]_]+", "", text).strip()


def analyze_pdf(
    pdf_path: Path,
    original_path: Path,
    output_dir: Path,
    docx_hints: list[str],
    raster_mode: str,
) -> dict:
    document = fitz.open(pdf_path)
    all_boxes: list[BoxCandidate] = []
    for page in document:
        page_boxes = []
        page_boxes.extend(detect_checkbox_glyphs(page))
        page_boxes.extend(detect_bracket_checkboxes(page))
        page_boxes.extend(detect_from_pdf_drawings(page))
        if raster_mode == "always" or (raster_mode == "auto" and not page_boxes):
            page_boxes.extend(detect_from_page_image(page, DEFAULT_DPI))
        all_boxes.extend(page_boxes)

    boxes = merge_candidates(all_boxes)
    boxes_by_page: dict[int, list[BoxCandidate]] = {}
    for box in boxes:
        boxes_by_page.setdefault(box.page, []).append(box)

    records = []
    for index, box in enumerate(boxes, start=1):
        page = document[box.page - 1]
        records.append(
            {
                "id": f"checkbox_{index:04d}",
                "page": box.page,
                "bbox": rect_to_json(box.rect),
                "text_right": right_text_on_same_line(page, box, boxes_by_page[box.page]),
            }
        )

    annotated_name = f"{slugify(original_path.stem)}_checkboxes.pdf"
    annotated_path = output_dir / "annotated_pdf" / annotated_name
    annotated_path.parent.mkdir(parents=True, exist_ok=True)
    draw_annotations(document, records)
    document.save(annotated_path, garbage=4, deflate=True)
    document.close()

    return {
        "input_file": str(original_path),
        "working_pdf": str(pdf_path),
        "annotated_pdf": str(annotated_path),
        "file_type": original_path.suffix.lower().lstrip("."),
        "checkbox_count": len(records),
        "docx_checkbox_xml_hints": docx_hints,
        "checkboxes": records,
    }

def _bbox_dict_to_list(bbox: dict) -> list[float] | None:
    try:
        return [
            float(bbox["x0"]),
            float(bbox["y0"]),
            float(bbox["x1"]),
            float(bbox["y1"]),
        ]
    except Exception:
        return None


def _record_to_m1_checkbox(record: dict) -> dict:
    bbox = _bbox_dict_to_list(record.get("bbox") or {})
    text = str(record.get("text_right") or "").strip()
    return {
        "label": text,
        "text": text,
        "lines": [text] if text else [],
        "page": record.get("page", 1),
        "bbox": bbox,
        "marker_bbox": bbox,
        "checkbox_bbox": bbox,
        "source": record.get("id", ""),
    }


def extract_checkboxes_from_pdf(
    pdf_path: str | Path,
    original_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    raster_mode: str = "off",
) -> list[dict]:
    pdf_path = Path(pdf_path)
    original = Path(original_path) if original_path else pdf_path
    out_dir = Path(output_dir) if output_dir else pdf_path.parent
    result = analyze_pdf(pdf_path, original, out_dir, [], raster_mode)
    return [_record_to_m1_checkbox(record) for record in result.get("checkboxes") or []]

def draw_annotations(document: fitz.Document, records: list[dict]) -> None:
    for record in records:
        page = document[record["page"] - 1]
        bbox = record["bbox"]
        rect = fitz.Rect(bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"])
        page.draw_rect(rect, color=(1, 0, 0), width=1.8, overlay=True)
        label_point = fitz.Point(rect.x1 + 2, max(8, rect.y0 - 1))
        page.insert_text(
            label_point,
            record["id"].replace("checkbox_", "#"),
            fontsize=6,
            color=(1, 0, 0),
            overlay=True,
        )


def process_file(input_path: Path, output_dir: Path, converted_dir: Path, raster_mode: str) -> dict:
    suffix = input_path.suffix.lower()
    docx_hints = extract_docx_checkbox_hints(input_path)
    if suffix == ".pdf":
        pdf_path = input_path
    elif suffix in {".doc", ".docx"}:
        pdf_path = convert_word_to_pdf(input_path, converted_dir)
    else:
        raise ValueError(f"Unsupported file type: {input_path}")
    return analyze_pdf(pdf_path, input_path, output_dir, docx_hints, raster_mode)


def iter_documents(input_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED and not path.name.startswith("~$")
    )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect checkbox-like squares in PDF/DOC/DOCX files and create JSON + red annotated PDFs."
    )
    parser.add_argument("input_dir", type=Path, help="Folder containing PDF, DOC, and DOCX files.")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("output"), help="Output folder.")
    parser.add_argument(
        "--raster",
        choices=("auto", "always", "off"),
        default="off",
        help="Image-based detection mode. Use always/auto for scanned PDFs; off is fastest for digital Word/PDF files.",
    )
    parser.add_argument("--fail-fast", action="store_true", help="Stop at first failed document.")
    args = parser.parse_args()

    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    converted_dir = output_dir / "converted_word_pdf"
    json_dir = output_dir / "json"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.exists() or not input_dir.is_dir():
        raise SystemExit(f"Input folder not found: {input_dir}")

    documents = iter_documents(input_dir)
    summary = {"input_dir": str(input_dir), "output_dir": str(output_dir), "documents": [], "errors": []}
    for document_path in documents:
        print(f"Processing: {document_path.name}", flush=True)
        try:
            result = process_file(document_path, output_dir, converted_dir, args.raster)
            json_path = json_dir / f"{slugify(document_path.stem)}.json"
            write_json(json_path, result["checkboxes"])
            summary["documents"].append(
                {
                    "input_file": str(document_path),
                    "json": str(json_path),
                    "annotated_pdf": result["annotated_pdf"],
                    "checkbox_count": result["checkbox_count"],
                }
            )
            print(f"  checkboxes: {result['checkbox_count']} -> {json_path.name}", flush=True)
        except Exception as exc:
            message = {"input_file": str(document_path), "error": str(exc)}
            summary["errors"].append(message)
            print(f"  ERROR: {exc}", file=sys.stderr, flush=True)
            if args.fail_fast:
                break

    write_json(output_dir / "summary.json", summary)
    print(f"Done. Summary: {output_dir / 'summary.json'}", flush=True)
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
