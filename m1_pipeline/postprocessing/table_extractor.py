import argparse
import json
from pathlib import Path
from typing import Any

import fitz


def bbox_to_dict(bbox: tuple[float, float, float, float] | None) -> dict[str, float] | None:
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    return {
        "x0": float(x0),
        "y0": float(y0),
        "x1": float(x1),
        "y1": float(y1),
        "width": float(x1 - x0),
        "height": float(y1 - y0),
    }


def _extract_tables_pdf_json(pdf_path: Path) -> dict[str, Any]:
    doc = fitz.open(pdf_path)
    pages_payload: list[dict[str, Any]] = []
    total_tables = 0

    for page_idx, page in enumerate(doc):
        finder = page.find_tables()
        page_tables: list[dict[str, Any]] = []
        print(f"Pagina {page_idx + 1}: {len(finder.tables)} tabelle")
        total_tables += len(finder.tables)

        for table_idx, table in enumerate(finder.tables):
            extracted = table.extract()
            rows_payload: list[dict[str, Any]] = []

            for row_idx, row in enumerate(table.rows):
                row_cells: list[dict[str, Any]] = []
                for col_idx, cell_bbox in enumerate(row.cells):
                    cell_text = None
                    if row_idx < len(extracted) and col_idx < len(extracted[row_idx]):
                        cell_text = extracted[row_idx][col_idx]
                    if isinstance(cell_text, str):
                        cell_text = cell_text.strip()

                    row_cells.append(
                        {
                            "row": row_idx,
                            "col": col_idx,
                            "bbox": bbox_to_dict(cell_bbox),
                            "text": cell_text,
                            "is_placeholder_for_span": cell_bbox is None,
                        }
                    )
                rows_payload.append({"row_index": row_idx, "cells": row_cells})

            table_payload = {
                "table_index_in_page": table_idx,
                "bbox": bbox_to_dict(table.bbox),
                "row_count": table.row_count,
                "col_count": table.col_count,
                "rows": rows_payload,
            }
            page_tables.append(table_payload)

        pages_payload.append(
            {
                "page_number": page_idx + 1,
                "table_count": len(page_tables),
                "tables": page_tables,
            }
        )

    return {
        "source_pdf": str(pdf_path),
        "total_pages": len(doc),
        "total_tables": total_tables,
        "pages": pages_payload,
    }


def _resolve_pdf_path(file_path: str | Path) -> Path:
    src = Path(file_path)
    if src.suffix.lower() == ".pdf" and src.exists():
        return src
    fallback = Path(__file__).with_name("input.pdf")
    if fallback.exists():
        return fallback
    raise FileNotFoundError(f"Nessun PDF disponibile per estrazione tabelle. Input: {src}")


def _bbox_dict_to_xywh_list(bbox: dict[str, float] | None) -> list[float] | None:
    if not bbox:
        return None
    return [
        round(float(bbox["x0"]), 2),
        round(float(bbox["y0"]), 2),
        round(float(bbox["width"]), 2),
        round(float(bbox["height"]), 2),
    ]


def _json_tables_to_m1_tables(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in data.get("pages", []):
        page_num = int(page.get("page_number", 1))
        for table in page.get("tables", []):
            rows = table.get("rows", [])
            if not rows:
                continue

            # Heuristic: some PDFs split a header cell across two consecutive rows (wrapped text),
            # while other columns keep the header on a single row. When the 2nd row looks like
            # a continuation (few non-empty cells and only in columns already non-empty in row0),
            # merge row1 text into row0 and drop row1 from the table structure.
            if len(rows) >= 2:
                header0 = rows[0].get("cells", []) or []
                header1 = rows[1].get("cells", []) or []
                if isinstance(header0, list) and isinstance(header1, list) and header0 and header1:
                    h0 = [str((c or {}).get("text") or "").strip() for c in header0]
                    h1 = [str((c or {}).get("text") or "").strip() for c in header1]
                    non0 = [i for i, t in enumerate(h0) if t]
                    non1 = [i for i, t in enumerate(h1) if t]
                    # Merge when row1 has some text, fewer filled columns than row0,
                    # and all row1 non-empty columns are also non-empty in row0.
                    if non1 and len(non1) < max(1, len(non0)) and all(i in non0 for i in non1):
                        for i in non1:
                            try:
	                                merged = f"{h0[i]} {h1[i]}".strip()
	                                header0[i]["text"] = merged
                            except Exception:
                                continue
                        rows = [rows[0]] + rows[2:]

            header_row = rows[0].get("cells", [])
            headers = [str(cell.get("text") or "").strip() for cell in header_row]
            header_cells = []
            for col_idx, cell in enumerate(header_row):
                header_cells.append(
                    {
                        "colonna": f"header_{col_idx}",
                        "testo": str(cell.get("text") or "").strip(),
                        "fillable": False,
                        "page": page_num,
                        "bbox": _bbox_dict_to_xywh_list(cell.get("bbox")),
                    }
                )

            data_rows = []
            for row_idx, row in enumerate(rows[1:], start=1):
                cells_out = []
                for col_idx, cell in enumerate(row.get("cells", [])):
                    text = str(cell.get("text") or "").strip()
                    cells_out.append(
                        {
                            "colonna": headers[col_idx] if col_idx < len(headers) and headers[col_idx] else f"col_{col_idx}",
                            "testo": text,
                            "fillable": text == "",
                            "page": page_num,
                            "bbox": _bbox_dict_to_xywh_list(cell.get("bbox")),
                        }
                    )
                data_rows.append({"row_index": row_idx, "cells": cells_out})

            out.append(
                {
                    "table_index": int(table.get("table_index_in_page", 0)),
                    "headers": headers,
                    "header_cells": header_cells,
                    "rows": data_rows,
                    "page": page_num,
                    "bbox": _bbox_dict_to_xywh_list(table.get("bbox")),
                }
            )
    return out


def extract_tables(file_path: str, blocks: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    # blocks è mantenuto per compatibilità con main.py, non usato in questa implementazione
    del blocks
    pdf_path = _resolve_pdf_path(file_path)
    data = _extract_tables_pdf_json(pdf_path)
    data = filter_narrow_columns(data, 10.0)
    return _json_tables_to_m1_tables(data)


def _union_bbox(bboxes: list[dict[str, float]]) -> dict[str, float] | None:
    if not bboxes:
        return None
    x0 = min(b["x0"] for b in bboxes)
    y0 = min(b["y0"] for b in bboxes)
    x1 = max(b["x1"] for b in bboxes)
    y1 = max(b["y1"] for b in bboxes)
    return {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "width": x1 - x0,
        "height": y1 - y0,
    }


def filter_narrow_columns(data: dict[str, Any], min_col_width: float) -> dict[str, Any]:
    for page in data.get("pages", []):
        for table in page.get("tables", []):
            col_count = table.get("col_count", 0)
            if col_count <= 0:
                continue

            # Enforce a stricter minimum column width based on the table width too.
            # This prevents spurious micro-columns that often come from border detection noise.
            bbox = table.get("bbox") or {}
            table_width = float(bbox.get("width") or 0.0) if isinstance(bbox, dict) else 0.0
            effective_min = float(min_col_width)
            if table_width > 0:
                effective_min = max(effective_min, table_width * 0.06)  # ~6% of table width

            col_widths: list[float] = []
            for col_idx in range(col_count):
                widths = []
                for row in table.get("rows", []):
                    cells = row.get("cells", [])
                    if col_idx >= len(cells):
                        continue
                    bbox = cells[col_idx].get("bbox")
                    if bbox:
                        widths.append(float(bbox["width"]))
                col_widths.append(max(widths) if widths else 0.0)

            keep_idxs = [idx for idx, w in enumerate(col_widths) if w >= effective_min]
            if not keep_idxs:
                keep_idxs = list(range(col_count))

            for row in table.get("rows", []):
                new_cells = []
                for new_col_idx, old_col_idx in enumerate(keep_idxs):
                    cell = row["cells"][old_col_idx]
                    cell["col"] = new_col_idx
                    new_cells.append(cell)
                row["cells"] = new_cells

            table["col_count"] = len(keep_idxs)
            table["kept_col_indices_from_original"] = keep_idxs
            table["removed_narrow_col_indices"] = [i for i in range(col_count) if i not in keep_idxs]

            remaining_bboxes = []
            for row in table.get("rows", []):
                for cell in row.get("cells", []):
                    if cell.get("bbox"):
                        remaining_bboxes.append(cell["bbox"])
            merged_bbox = _union_bbox(remaining_bboxes)
            if merged_bbox:
                table["bbox"] = merged_bbox

    return data


def render_pages_with_table_boxes(
    pdf_path: Path,
    tables_json: dict[str, Any],
    output_dir: Path,
    dpi: int = 180,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)

    for page_entry in tables_json.get("pages", []):
        page_number = page_entry["page_number"]
        page = doc[page_number - 1]

        for table in page_entry.get("tables", []):
            t_bbox = table.get("bbox")
            if t_bbox:
                page.draw_rect(
                    fitz.Rect(t_bbox["x0"], t_bbox["y0"], t_bbox["x1"], t_bbox["y1"]),
                    color=(1, 0, 0),
                    width=1.8,
                    overlay=True,
                )

            for row in table.get("rows", []):
                for cell in row.get("cells", []):
                    c_bbox = cell.get("bbox")
                    if not c_bbox:
                        continue
                    page.draw_rect(
                        fitz.Rect(c_bbox["x0"], c_bbox["y0"], c_bbox["x1"], c_bbox["y1"]),
                        color=(1, 0, 0),
                        width=1.2,
                        overlay=True,
                    )

        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        out_path = output_dir / f"pagina_{page_number:03d}.png"
        pix.save(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trova tabelle in un PDF ed esporta celle con coordinate precise."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).with_name("input.pdf"),
        help="Percorso del PDF di input (default: input.pdf nella stessa cartella).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("tabelle_rilevate.json"),
        help="Percorso JSON di output.",
    )
    parser.add_argument(
        "--img-dir",
        type=Path,
        default=Path(__file__).with_name("pagine_con_tabelle"),
        help="Cartella output immagini pagine con box tabelle/celle.",
    )
    parser.add_argument(
        "--min-col-width",
        type=float,
        default=10.0,
        help="Larghezza minima colonna (punti PDF) per considerarla colonna reale.",
    )
    args = parser.parse_args()

    if not args.input.exists():
        raise FileNotFoundError(f"PDF non trovato: {args.input}")

    result = _extract_tables_pdf_json(args.input)
    result = filter_narrow_columns(result, args.min_col_width)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    data_from_json = json.loads(args.output.read_text(encoding="utf-8"))
    render_pages_with_table_boxes(args.input, data_from_json, args.img_dir)
    print(f"Totale tabelle trovate: {result['total_tables']}")
    print(f"JSON salvato in: {args.output}")
    print(f"Immagini salvate in: {args.img_dir}")


if __name__ == "__main__":
    main()
