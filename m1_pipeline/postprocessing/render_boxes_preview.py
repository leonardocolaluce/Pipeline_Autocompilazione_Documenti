import json
import os
from collections import defaultdict
from typing import Any, Dict, List, Tuple

import argparse

_default_pdf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input.pdf")
_default_json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "campi_pdf.json")

parser = argparse.ArgumentParser()
parser.add_argument("--input-pdf", default=_default_pdf_path)
parser.add_argument("--fields-json", default=_default_json_path)
parser.add_argument("--out-dir", required=True)
parser.add_argument(
    "--tables-json",
    default="",
    help="Path JSON tabelle (se fornito, disegna le tabelle in verde sulle stesse immagini).",
)
args = parser.parse_args()
pdf_path = args.input_pdf
json_path = args.fields_json
out_dir = args.out_dir
tables_json_path = args.tables_json

def _try_import_fitz():
    try:
        import fitz  # type: ignore

        return fitz
    except Exception as e:
        raise SystemExit(
            "Errore: manca PyMuPDF.\n"
            "Installa con: pip install PyMuPDF\n"
            f"Dettaglio: {e}"
        )


def _load_fields() -> Dict[int, List[Tuple[float, float, float, float]]]:
    if not os.path.exists(json_path):
        raise SystemExit(f"JSON non trovato: {json_path}")
    data = json.loads(open(json_path, "r", encoding="utf-8").read())
    by_page: Dict[int, List[Tuple[float, float, float, float]]] = defaultdict(list)

    for item in data:
        page = int(item["pagina"])
        cc = item["cc"]
        by_page[page].append((float(cc["x0"]), float(cc["y0"]), float(cc["x1"]), float(cc["y1"])))

    return by_page


def _is_bbox_xywh(bbox: Any) -> bool:
    return isinstance(bbox, (list, tuple)) and len(bbox) == 4 and all(isinstance(x, (int, float)) for x in bbox)


def _bbox_to_rect(bbox: Any) -> Tuple[float, float, float, float] | None:
    """
    Supporta due formati bbox:
      - [x, y, w, h]  (xywh)
      - [x0, y0, x1, y1] (corners)
    Ritorna sempre (x0, y0, x1, y1).
    """
    if not _is_bbox_xywh(bbox):
        return None
    x0, y0, a, b = [float(v) for v in bbox]
    # Heuristic: if (a,b) look like bottom-right corners, keep them.
    if a > x0 and b > y0:
        return (x0, y0, a, b)
    # Otherwise treat as width/height.
    w, h = a, b
    if w <= 0 or h <= 0:
        return None
    return (x0, y0, x0 + w, y0 + h)


def _load_tables_boxes() -> Dict[int, List[Tuple[float, float, float, float]]]:
    """
    Carica un JSON tabelle e ritorna bbox per pagina nel formato (x0,y0,x1,y1).
    Supporta:
      - root = lista di tabelle
      - root = dict con chiave 'tables' (lista)
    Atteso per ciascuna cella: {page: int, bbox: [x, y, w, h]} (xywh).
    """
    by_page: Dict[int, List[Tuple[float, float, float, float]]] = defaultdict(list)
    if not isinstance(tables_json_path, str) or not tables_json_path.strip():
        return by_page
    if not os.path.exists(tables_json_path):
        raise SystemExit(f"JSON tabelle non trovato: {tables_json_path}")

    raw = json.loads(open(tables_json_path, "r", encoding="utf-8").read())
    if isinstance(raw, dict) and isinstance(raw.get("tables"), list):
        tables = raw.get("tables") or []
    elif isinstance(raw, list):
        tables = raw
    else:
        tables = []

    def _push_bbox(page: Any, bbox: Any) -> None:
        if page in (None, ""):
            return
        rect = _bbox_to_rect(bbox)
        if rect is None:
            return
        page_num = int(page)
        by_page[page_num].append(rect)

    def _push_cell(cell: Any, *, fallback_page: Any = None) -> None:
        if not isinstance(cell, dict):
            return
        page = cell.get("page")
        if page in (None, ""):
            page = fallback_page
        _push_bbox(page, cell.get("bbox"))

    for t in tables:
        if not isinstance(t, dict):
            continue
        table_page = t.get("page")
        _push_bbox(table_page, t.get("bbox"))

        for cell in (t.get("header_cells") or []):
            _push_cell(cell, fallback_page=table_page)
        for row in (t.get("rows") or []):
            if not isinstance(row, dict):
                continue
            for cell in (row.get("cells") or []):
                _push_cell(cell, fallback_page=table_page)

    return by_page


def main() -> None:
    fitz = _try_import_fitz()

    if not os.path.exists(pdf_path):
        raise SystemExit(f"PDF non trovato: {pdf_path}")

    os.makedirs(out_dir, exist_ok=True)
    fields_by_page = _load_fields()
    tables_by_page = _load_tables_boxes()

    doc = fitz.open(pdf_path)

    # Disegna box rossi (senza salvare il PDF): poi renderizza in PNG.
    red = (1, 0, 0)
    green = (0, 1, 0)
    for page_index in range(doc.page_count):
        page_num = page_index + 1
        page = doc.load_page(page_index)

        for (x0, y0, x1, y1) in fields_by_page.get(page_num, []):
            rect = fitz.Rect(x0, y0, x1, y1)
            # bordo rosso visibile
            page.draw_rect(rect, color=red, width=1.2)

        for (x0, y0, x1, y1) in tables_by_page.get(page_num, []):
            rect = fitz.Rect(x0, y0, x1, y1)
            # bordo verde per tabelle
            page.draw_rect(rect, color=green, width=1.2)

        pix = page.get_pixmap(dpi=200, alpha=False)
        out_path = os.path.join(out_dir, f"page_{page_num:03d}.png")
        pix.save(out_path)

    print(f"OK: salvate {doc.page_count} immagini in: {out_dir}")


if __name__ == "__main__":
    main()
