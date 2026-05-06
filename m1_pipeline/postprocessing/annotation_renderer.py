from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import fitz


ANNOTATIONS_DIR_SUFFIX = "_ANNOTAZIONI"


def render_annotations_from_m1(
    source_pdf_path: str | Path,
    output_dir: str | Path,
    base_name: str,
    *,
    fields: List[Dict[str, Any]] | None = None,
    checkboxes: List[Dict[str, Any]] | None = None,
    tables: List[Dict[str, Any]] | None = None,
    zoom: float = 2.0,
) -> List[str]:
    pdf_path = Path(source_pdf_path).resolve()
    out_dir = Path(output_dir).resolve() / f"{base_name}{ANNOTATIONS_DIR_SUFFIX}"
    out_dir.mkdir(parents=True, exist_ok=True)

    annotations_red_by_page: Dict[int, List[Tuple[float, float, float, float]]] = {}
    annotations_green_by_page: Dict[int, List[Tuple[float, float, float, float]]] = {}

    # ROSSO: fields + checkboxes
    for item in fields or []:
        _push_bbox(annotations_red_by_page, item.get("page"), item.get("bbox"))

    for item in checkboxes or []:
        _push_bbox(annotations_red_by_page, item.get("page"), item.get("marker_bbox") or item.get("bbox"))

    # VERDE: tabelle
    for table in tables or []:
        for cell in table.get("header_cells", []) or []:
            _push_bbox(annotations_green_by_page, cell.get("page"), cell.get("bbox"))
        for row in table.get("rows", []):
            for cell in row.get("cells", []):
                _push_bbox(annotations_green_by_page, cell.get("page"), cell.get("bbox"))

    written: List[str] = []
    with fitz.open(str(pdf_path)) as doc:
        for page_index, page in enumerate(doc, start=1):
            # prima rosso
            for x, y, w, h in annotations_red_by_page.get(page_index, []):
                rect = fitz.Rect(float(x), float(y), float(x + w), float(y + h))
                page.draw_rect(rect, color=(1, 0, 0), width=1.5)

            # poi verde (tabelle)
            for x, y, w, h in annotations_green_by_page.get(page_index, []):
                rect = fitz.Rect(float(x), float(y), float(x + w), float(y + h))
                page.draw_rect(rect, color=(0, 1, 0), width=1.5)

            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            output_path = out_dir / f"{base_name}_page_{page_index:03d}.png"
            pixmap.save(str(output_path))
            written.append(str(output_path))

    return written


def _push_bbox(
    annotations_by_page: Dict[int, List[Tuple[float, float, float, float]]],
    page: Any,
    bbox: Any,
) -> None:
    if page in (None, "") or not _is_bbox(bbox):
        return
    page_num = int(page)
    x, y, w, h = [float(value) for value in bbox]
    if w <= 0 or h <= 0:
        return
    annotations_by_page.setdefault(page_num, []).append((x, y, w, h))


def _is_bbox(bbox: Any) -> bool:
    return isinstance(bbox, (list, tuple)) and len(bbox) == 4
