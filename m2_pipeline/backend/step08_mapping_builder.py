import json
from pathlib import Path
from typing import Any, Dict, List

from .step00_config import FIELD_MAPPING_FILENAME


def _coerce_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    except Exception:
        return None
    # normalize just in case
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def _bbox_union(a: list[float] | None, b: list[float] | None) -> list[float] | None:
    if a is None:
        return b
    if b is None:
        return a
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def _bbox_contains(outer: list[float], inner: list[float]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _format_table_context(
    table_index: int,
    row_index: int,
    col_index: int,
    headers: List[str],
    row_cells: List[Dict[str, Any]],
) -> Dict[str, Any]:
    header_value = headers[col_index] if col_index < len(headers) else ""
    non_fillable = [
        {
            "colonna": str(cell.get("colonna", "")).strip(),
            "testo": str(cell.get("testo", "")).strip(),
        }
        for cell in row_cells
        if not bool(cell.get("fillable"))
    ]
    context_parts = [
        f"tabella {table_index}",
        f"riga {row_index}",
        f"colonna {col_index}",
    ]
    if header_value:
        context_parts.append(f"header_colonna: {header_value}")
    if headers:
        context_parts.append(f"headers: {headers}")
    if non_fillable:
        context_parts.append(f"riga_labels: {non_fillable}")
    return {
        "table_index": table_index,
        "row_index": row_index,
        "col_index": col_index,
        "table_headers": headers,
        "row_cells": row_cells,
        "row_labels": non_fillable,
        "context_text": " | ".join(context_parts),
    }


def _build_items_for_bundle(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    fields_items: List[Dict[str, Any]] = []
    other_items: List[Dict[str, Any]] = []

    # Compute outer perimeter bbox for each table (page, table_index) using all available cell bboxes.
    table_bbox_by_key: dict[tuple[int, int], list[float]] = {}
    for table in bundle.get("tables") or []:
        table_index = int(table.get("table_index", 0) or 0)
        for row in table.get("rows") or []:
            for cell in row.get("cells") or []:
                bbox = _coerce_bbox(cell.get("bbox"))
                if bbox is None:
                    continue
                try:
                    page = int(cell.get("page") or 0)
                except Exception:
                    continue
                key = (page, table_index)
                table_bbox_by_key[key] = _bbox_union(table_bbox_by_key.get(key), bbox)  # type: ignore[arg-type]

    removed_fields_inside_tables = 0
    for idx, field in enumerate(bundle.get("fields") or []):
        # Drop fields whose bbox is fully contained within any table perimeter on the same page.
        try:
            page = int(field.get("page") or 0)
        except Exception:
            page = 0
        field_bbox = _coerce_bbox(field.get("bbox"))
        if field_bbox is not None:
            inside_any_table = False
            for (t_page, _t_index), t_bbox in table_bbox_by_key.items():
                if t_page != page:
                    continue
                if _bbox_contains(t_bbox, field_bbox):
                    inside_any_table = True
                    break
            if inside_any_table:
                removed_fields_inside_tables += 1
                continue
        fields_items.append(
            {
                "item_id": str(field.get("field_id") or f"field:{idx}"),
                "item_type": "field",
                "label": str(field.get("campo") or field.get("label") or "").strip(),
                "context": str(field.get("contesto", "")).strip(),
                "context_line": str(field.get("contesto_riga", "")).strip(),
                "context_above": str(field.get("contesto_sopra", "")).strip(),
                "placeholder": str(field.get("placeholder", "")).strip(),
                "page": field.get("page"),
                "bbox": field.get("bbox"),
            }
        )

    if removed_fields_inside_tables:
        print(f"[mapping] removed_fields_inside_tables={removed_fields_inside_tables}")

    for idx, checkbox in enumerate(bundle.get("checkboxes") or []):
        other_items.append(
            {
                "item_id": f"checkbox:{idx}",
                "item_type": "checkbox",
                "label": str(checkbox.get("label", "")).strip(),
                "context": str(checkbox.get("text", "")).strip(),
                "placeholder": "",
                "page": checkbox.get("page"),
                "bbox": checkbox.get("bbox"),
                "marker_bbox": checkbox.get("marker_bbox"),
                "checkbox_lines": checkbox.get("lines") or [],
                "checkbox_bbox": checkbox.get("bbox"),
            }
        )

    for table in bundle.get("tables") or []:
        table_index = int(table.get("table_index", 0) or 0)
        headers = [str(value).strip() for value in (table.get("headers") or [])]
        for row in table.get("rows") or []:
            row_index = int(row.get("row_index", 0) or 0)
            row_cells = list(row.get("cells") or [])
            for col_index, cell in enumerate(row_cells):
                if not bool(cell.get("fillable")):
                    continue
                cell_label = str(cell.get("colonna", "")).strip()
                if col_index < len(headers) and headers[col_index]:
                    cell_label = headers[col_index]
                table_ctx = _format_table_context(table_index, row_index, col_index, headers, row_cells)
                other_items.append(
                    {
                        "item_id": f"table:{table_index}:{row_index}:{col_index}",
                        "item_type": "table_cell",
                        "label": cell_label,
                        "context": table_ctx["context_text"],
                        "placeholder": "",
                        "page": cell.get("page"),
                        "bbox": cell.get("bbox"),
                        "table_index": table_ctx["table_index"],
                        "row_index": table_ctx["row_index"],
                        "col_index": table_ctx["col_index"],
                        "table_headers": table_ctx["table_headers"],
                        "row_cells": table_ctx["row_cells"],
                        "row_labels": table_ctx["row_labels"],
                    }
                )

    items.extend(fields_items)
    items.extend(other_items)
    return items



def _rows_for_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in items:
        row: Dict[str, Any] = {
            "item_id": item["item_id"],
            "item_type": item["item_type"],
            "label": item.get("label"),
            "context": item.get("context"),
            "placeholder": item.get("placeholder"),
            "page": item.get("page"),
            "bbox": item.get("bbox"),
            "answer": "",
            "confidence": None,
            "reason": "",
            "llm_enabled": False,
        }
        for key in (
            "context_line",
            "context_above",
            "table_index",
            "row_index",
            "col_index",
            "table_headers",
            "row_cells",
            "row_labels",
            "checkbox_lines",
            "checkbox_bbox",
            "marker_bbox",
        ):
            if key in item:
                row[key] = item[key]
        rows.append(row)
    return rows

def remove_fields_inside_tables_from_created_json(json_path: str | Path) -> Dict[str, Any]:
    def norm_bbox(bbox):
        if not bbox or len(bbox) != 4:
            return None

        x1, y1, a, b = map(float, bbox)

        if a > x1 and b > y1:
            x2, y2 = a, b
        else:
            x2, y2 = x1 + a, y1 + b

        return {
            "x1": min(x1, x2),
            "y1": min(y1, y2),
            "x2": max(x1, x2),
            "y2": max(y1, y2),
        }

    def bbox_center(box):
        return (
            (box["x1"] + box["x2"]) / 2,
            (box["y1"] + box["y2"]) / 2,
        )

    def point_inside_box(x, y, box, tolerance=2):
        return (
            box["x1"] - tolerance <= x <= box["x2"] + tolerance
            and box["y1"] - tolerance <= y <= box["y2"] + tolerance
        )

    json_file = Path(json_path).resolve()

    if not json_file.exists():
        raise FileNotFoundError(f"JSON mapping non trovato: {json_file}")

    payload = json.loads(json_file.read_text(encoding="utf-8"))
    rows: List[Dict[str, Any]] = list(payload.get("rows") or [])

    tables_by_key: dict[tuple[int, int], list[Dict[str, Any]]] = {}

    for item in rows:
        if item.get("item_type") != "table_cell":
            continue

        page = item.get("page")
        table_index = item.get("table_index")
        bbox = norm_bbox(item.get("bbox"))

        if page is None or table_index is None or bbox is None:
            continue

        key = (int(page), int(table_index))
        tables_by_key.setdefault(key, []).append({
            "item_id": item.get("item_id"),
            "row_index": item.get("row_index"),
            "col_index": item.get("col_index"),
            "bbox": bbox,
        })

    table_boxes = []

    for (page, table_index), cells in tables_by_key.items():
        x1 = min(cell["bbox"]["x1"] for cell in cells)
        y1 = min(cell["bbox"]["y1"] for cell in cells)
        x2 = max(cell["bbox"]["x2"] for cell in cells)
        y2 = max(cell["bbox"]["y2"] for cell in cells)

        table_boxes.append({
            "page": page,
            "table_index": table_index,
            "cells_count": len(cells),
            "bbox": {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
            },
        })

    cleaned_rows: List[Dict[str, Any]] = []
    removed_count = 0

    for item in rows:
        if item.get("item_type") != "field":
            cleaned_rows.append(item)
            continue

        page = item.get("page")
        bbox = norm_bbox(item.get("bbox"))

        if page is None or bbox is None:
            cleaned_rows.append(item)
            continue

        cx, cy = bbox_center(bbox)

        inside_any_table = False

        for table in table_boxes:
            if int(page) != int(table["page"]):
                continue

            if point_inside_box(cx, cy, table["bbox"], tolerance=2):
                inside_any_table = True
                break

        if inside_any_table:
            removed_count += 1
            continue

        cleaned_rows.append(item)

    payload["rows"] = cleaned_rows
    payload["item_count"] = len(cleaned_rows)

    json_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Eliminati {removed_count} field dentro tabelle, per tutte le pagine.")

    return {
        "json_path": str(json_file),
        "removed_count": removed_count,
        "item_count": len(cleaned_rows),
    }

def build_mapping_file(
    bundle: Dict[str, Any],
    xml_json_path: str | Path,
    output_dir: str | Path,
) -> Dict[str, Any]:
    xml_path = Path(xml_json_path).resolve()
    if not xml_path.exists():
        raise FileNotFoundError(f"JSON XML non trovato: {xml_path}")

    items = _build_items_for_bundle(bundle)
    rows = _rows_for_items(items)

    out_dir = Path(output_dir)
    out_path = out_dir / FIELD_MAPPING_FILENAME
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "base_name": bundle["base_name"],
                "source_document_path": bundle.get("source_document_path"),
                "source_document_type": bundle.get("source_document_type"),
                "xml_json_path": str(xml_path),
                "item_count": len(rows),
                "rows": rows,
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )

    remove_fields_inside_tables_from_created_json(out_path)

    return {
        "base_name": bundle["base_name"],
        "output_path": str(out_path),
        "item_count": len(rows),
    }
