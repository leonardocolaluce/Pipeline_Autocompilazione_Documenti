import json
from pathlib import Path
from typing import Any, Dict, List

from .step00_config import FIELD_MAPPING_FILENAME


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

    for idx, field in enumerate(bundle.get("fields") or []):
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

    return {
        "base_name": bundle["base_name"],
        "output_path": str(out_path),
        "item_count": len(rows),
    }
