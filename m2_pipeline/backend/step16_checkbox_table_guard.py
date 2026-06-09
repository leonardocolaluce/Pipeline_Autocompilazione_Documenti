import json
from pathlib import Path
from typing import Any


def clear_tables_linked_to_unchecked_checkboxes(
    mapping_json_path: str | Path,
    *,
    max_vertical_gap_pt: float = 45.0,
    max_left_slack_pt: float = 60.0,
) -> dict[str, Any]:
    path = Path(mapping_json_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []

    def bbox(row):
        value = row.get("marker_bbox") or row.get("checkbox_bbox") or row.get("bbox")
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        x0, y0, a, b = [float(v) for v in value]
        if a <= x0 or b <= y0:
            return [x0, y0, x0 + a, y0 + b]
        return [min(x0, a), min(y0, b), max(x0, a), max(y0, b)]

    checkboxes = []
    tables = {}

    for row in rows:
        item_type = str(row.get("item_type") or "")
        bb = bbox(row)
        if not bb:
            continue

        if item_type == "checkbox":
            checkboxes.append(
                {
                    "page": int(row.get("page") or 1),
                    "bbox": bb,
                    "checked": str(row.get("answer") or "").strip().upper() == "X",
                    "label": str(row.get("label") or row.get("context") or ""),
                }
            )

        elif item_type == "table_cell":
            key = (int(row.get("page") or 1), row.get("table_index"))
            tables.setdefault(key, []).append(row)

    cleared_tables = 0
    cleared_cells = 0
    kept_tables = 0

    for (page, table_index), table_rows in tables.items():
        table_bboxes = [bbox(row) for row in table_rows]
        table_bboxes = [bb for bb in table_bboxes if bb]
        if not table_bboxes:
            continue

        tx0 = min(bb[0] for bb in table_bboxes)
        ty0 = min(bb[1] for bb in table_bboxes)
        tx1 = max(bb[2] for bb in table_bboxes)

        linked = []
        for checkbox in checkboxes:
            if checkbox["page"] != page:
                continue
            cb = checkbox["bbox"]
            cb_cx = (cb[0] + cb[2]) / 2.0
            vertical_gap = ty0 - cb[3]
            horizontally_near = (tx0 - max_left_slack_pt) <= cb_cx <= tx1
            vertically_near = 0 <= vertical_gap <= max_vertical_gap_pt
            if horizontally_near and vertically_near:
                linked.append(checkbox)

        if not linked:
            continue

        if any(item["checked"] for item in linked):
            kept_tables += 1
            print(f"[checkbox-table-guard] KEEP table={table_index} page={page} checked_checkbox=True")
            continue

        for row in table_rows:
            if str(row.get("answer") or "").strip() not in {"", "N/D"}:
                row["answer"] = "N/D"
                row["confidence"] = 0.0
                row["reason"] = f"{row.get('reason', '')}|cleared_unchecked_checkbox_table".strip("|")
                cleared_cells += 1

        cleared_tables += 1
        labels = " | ".join(item["label"][:80] for item in linked)
        print(f"[checkbox-table-guard] CLEAR table={table_index} page={page} cells={len(table_rows)} checkbox='{labels}'")

    print(f"\n========== TABELLE NON SCRITTE PER CHECKBOX: {cleared_tables} ==========\n", flush=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[checkbox-table-guard] done tables_cleared={cleared_tables} "
        f"tables_kept={kept_tables} cells_cleared={cleared_cells}"
    )
    return {
        "mapping_json_path": str(path),
        "tables_cleared": cleared_tables,
        "tables_kept": kept_tables,
        "cells_cleared": cleared_cells,
    }