from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def _answer(row: dict[str, Any]) -> str:
    value = str(row.get("answer") or "").strip()
    return "" if value in {"", "N/D"} else value


def _page(row: dict[str, Any]) -> int:
    try:
        return max(1, int(row.get("page") or 1))
    except Exception:
        return 1


def _bbox(row: dict[str, Any]) -> list[float] | None:
    if str(row.get("item_type") or "") == "checkbox":
        value = row.get("marker_bbox") or row.get("checkbox_bbox") or row.get("bbox")
    else:
        value = row.get("bbox") or row.get("marker_bbox") or row.get("checkbox_bbox")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        a, b, c, d = (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    except Exception:
        return None

    # We support both [x0,y0,x1,y1] and [x,y,w,h].
    # If it looks like xywh (w/h are small positive), convert to x1/y1.
    if c <= a or d <= b:
        x0, y0 = a, b
        x1, y1 = a + c, b + d
    else:
        x0, y0, x1, y1 = a, b, c, d

    # Normalize
    x0n, x1n = (x0, x1) if x0 <= x1 else (x1, x0)
    y0n, y1n = (y0, y1) if y0 <= y1 else (y1, y0)
    if x1n - x0n < 1 or y1n - y0n < 1:
        return None
    return [x0n, y0n, x1n, y1n]


def _filled_coordinate_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    filled: list[dict[str, Any]] = []
    for row in rows:
        if not _answer(row):
            continue
        if _bbox(row) is None:
            continue
        filled.append(row)
    return sorted(
        filled,
        key=lambda r: (
            _page(r),
            float((_bbox(r) or [0, 0, 0, 0])[1]),
            float((_bbox(r) or [0, 0, 0, 0])[0]),
            str(r.get("item_id") or ""),
        ),
    )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_pdf_from_answers_json(
    source_pdf: str | Path,
    answers_json: str | Path,
    out_pdf: str | Path,
    *,
    color_rgb: tuple[float, float, float] = (0, 0, 0),  # black
    add_white_bg: bool = False,
    min_box_height: float = 12.0,
    min_box_width: float = 80.0,
    pad_right: float = 10.0,
) -> dict[str, Any]:
    """
    Overlays answers onto an existing PDF using bbox/page coordinates.

    Expects `answers_json` in the same schema as `campo_valore*.json`:
      {"rows":[{"page":int,"bbox":[x0,y0,x1,y1],"answer":"..."}]}
    """
    try:
        import fitz  # PyMuPDF
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("PyMuPDF (fitz) non disponibile; installa PyMuPDF.") from exc

    source_pdf = Path(source_pdf)
    answers_json = Path(answers_json)
    out_pdf = Path(out_pdf)

    if not source_pdf.exists() or source_pdf.suffix.lower() != ".pdf":
        raise FileNotFoundError(f"PDF sorgente non trovato (o non .pdf): {source_pdf}")
    if not answers_json.exists():
        raise FileNotFoundError(f"JSON risposte non trovato: {answers_json}")

    payload = _read_json(answers_json)
    rows = list(payload.get("rows") or [])
    filled_rows = _filled_coordinate_rows(rows)

    doc = fitz.open(str(source_pdf))
    written = 0
    skipped = 0
    overflow = 0
    adjusted = 0

    # Force black text for PDF overlays (ignore caller color overrides).
    color_rgb = (0, 0, 0)

    for row in filled_rows:
        page_no = _page(row)
        bbox = _bbox(row)
        text = _answer(row)
        if bbox is None or not text:
            skipped += 1
            continue
        if page_no < 1 or page_no > doc.page_count:
            skipped += 1
            continue

        page = doc.load_page(page_no - 1)
        rect = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])
        if str(row.get("item_type") or "") == "checkbox":
            rc = page.insert_textbox(
                rect,
                "X",
                fontsize=10,
                fontname="helv",
                color=(0, 0, 0),
                align=1,
                overlay=True,
            )
            if rc < 0:
                overflow += 1
            written += 1
            continue

        # Many "field" bboxes coming from line markers are extremely thin (≈1pt height),
        # which makes insert_textbox fail. Expand to a reasonable rectangle while keeping
        # the same anchor (x0) and staying inside the page.
        page_rect = page.rect
        orig_rect = rect

        target_height = max(float(min_box_height), float(rect.height))
        target_width = max(float(min_box_width), float(rect.width))

        # Try to approximate the width needed for the text (empirical average char width).
        # Clamp to the printable area.
        est_width = max(target_width, min(page_rect.width * 0.92, max(0.0, len(text)) * 5.5))
        target_width = max(target_width, est_width)

        x0 = float(rect.x0)
        x1 = min(float(page_rect.x1) - 2.0, x0 + float(target_width) + float(pad_right))
        if x1 <= x0 + 2.0:
            x1 = min(float(page_rect.x1) - 2.0, x0 + 2.0)

        # Treat the original bbox as an underline: place the box above it.
        y1 = min(float(page_rect.y1) - 2.0, float(rect.y1) + 2.0)
        y0 = max(float(page_rect.y0) + 2.0, y1 - float(target_height))
        if y1 <= y0 + 2.0:
            y0 = max(float(page_rect.y0) + 2.0, float(rect.y0) - float(target_height))
            y1 = min(float(page_rect.y1) - 2.0, y0 + float(target_height))

        rect = fitz.Rect(x0, y0, x1, y1)
        if rect != orig_rect:
            adjusted += 1

        if add_white_bg:
            page.draw_rect(rect, color=None, fill=(1, 1, 1), overlay=True)

        # Try to fit text by decreasing font size.
        rc = 1
        font_size = 11
        while font_size >= 6:
            rc = page.insert_textbox(
                rect,
                text,
                fontsize=font_size,
                fontname="helv",
                color=color_rgb,
                align=0,  # left
                overlay=True,
            )
            if rc >= 0:
                break
            font_size -= 1

        if rc < 0:
            overflow += 1
            # Fallback: draw a single line of text; may overflow horizontally but it becomes visible.
            try:
                page.insert_text(
                    (rect.x0, rect.y1 - 2.0),
                    text,
                    fontsize=6,
                    fontname="helv",
                    color=color_rgb,
                    overlay=True,
                )
            except Exception:
                pass
        written += 1

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_pdf))
    doc.close()

    return {
        "status": "ok",
        "source_pdf": str(source_pdf),
        "answers_json": str(answers_json),
        "output_pdf": str(out_pdf),
        "written": written,
        "skipped": skipped,
        "overflow": overflow,
        "adjusted_boxes": adjusted,
    }
