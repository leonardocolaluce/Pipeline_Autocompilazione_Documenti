from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple


_PAGE_RE = re.compile(r"page[_-]?0*(\\d+)", re.I)


def _page_from_image(filename: str) -> int | None:
    match = _PAGE_RE.search(str(filename or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


@dataclass(frozen=True)
class _CellKey:
    page: int
    table_index: int
    row_index: int
    col_index: int


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _dump_json(path: str | Path, payload: Any) -> None:
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _table_shapes_by_page(mapping_rows: List[Dict[str, Any]]) -> dict[int, dict[int, dict[str, Any]]]:
    """
    Returns:
      {page: {table_index: {"min_row": int, "max_col": int}}}
    """
    out: dict[int, dict[int, dict[str, Any]]] = {}
    for row in mapping_rows:
        if str(row.get("item_type", "")).strip() != "table_cell":
            continue
        try:
            page = int(row.get("page") or 0)
            table_index = int(row.get("table_index"))
            row_index = int(row.get("row_index"))
            col_index = int(row.get("col_index"))
        except Exception:
            continue
        bucket = out.setdefault(page, {}).setdefault(table_index, {"min_row": row_index, "max_col": col_index})
        bucket["min_row"] = min(int(bucket["min_row"]), row_index)
        bucket["max_col"] = max(int(bucket["max_col"]), col_index)
    return out


def _index_table_cells(mapping_rows: List[Dict[str, Any]]) -> dict[_CellKey, Dict[str, Any]]:
    index: dict[_CellKey, Dict[str, Any]] = {}
    for row in mapping_rows:
        if str(row.get("item_type", "")).strip() != "table_cell":
            continue
        try:
            key = _CellKey(
                page=int(row.get("page") or 0),
                table_index=int(row.get("table_index")),
                row_index=int(row.get("row_index")),
                col_index=int(row.get("col_index")),
            )
        except Exception:
            continue
        index[key] = row
    return index


def _select_table_index_for_llm_table(
    *,
    page: int,
    llm_headers_len: int,
    shapes: dict[int, dict[int, dict[str, Any]]],
) -> int | None:
    """
    Strict mapping from LLM table_id to M2 table_index based on column count on the same page.
    - We consider a mapping table_index a match if (max_col+1) == llm_headers_len.
    - If exactly one match exists, we return it. Otherwise return None (ambiguous).
    """
    candidates: list[int] = []
    for table_index, meta in (shapes.get(page) or {}).items():
        max_col = int(meta.get("max_col", -1))
        cols = max_col + 1
        if cols == llm_headers_len:
            candidates.append(int(table_index))
    if len(candidates) == 1:
        return candidates[0]
    return None


def merge_tables_filled_into_mapping(
    *,
    tables_filled_json_path: str | Path,
    mapping_json_path: str | Path,
    only_if_empty: bool = True,
) -> dict[str, Any]:
    """
    Reads `tables_filled_output.json` (LLM) and writes answers into `campo_valore_*.json` (mapping).

    Rigid rules:
    - Never cross pages: `page_002.png` -> page=2 only.
    - Never guess table_index if ambiguous: require a unique table_index on that page with same column count.
    - Never guess row/col: require exact cell existence (table_index,row_index,col_index).
    - Row mapping is 0-based (LLM) -> mapping row_index = min_row_index_of_table + llm_row_index.
    """
    mapping_path = Path(mapping_json_path).resolve()
    tables_path = Path(tables_filled_json_path).resolve()
    if not mapping_path.exists():
        raise FileNotFoundError(f"Mapping JSON non trovato: {mapping_path}")
    if not tables_path.exists():
        raise FileNotFoundError(f"Tables filled JSON non trovato: {tables_path}")

    mapping_payload = _load_json(mapping_path)
    rows = list(mapping_payload.get("rows") or [])
    cell_index = _index_table_cells(rows)
    shapes = _table_shapes_by_page(rows)

    tables_payload = _load_json(tables_path)
    images = tables_payload.get("images") or []
    if not isinstance(images, list):
        images = []

    applied = 0
    skipped = 0
    ambiguous_tables = 0
    missing_cells = 0

    for img in images:
        if not isinstance(img, dict):
            continue
        filename = str(img.get("file") or "")
        page = _page_from_image(filename)
        if page is None:
            continue
        tables = img.get("tables") or []
        if not isinstance(tables, list):
            continue

        for t in tables:
            if not isinstance(t, dict):
                continue
            grid = t.get("grid") or {}
            if not isinstance(grid, dict):
                continue
            headers = grid.get("headers") or t.get("headers") or []
            if not isinstance(headers, list):
                headers = []
            llm_headers_len = len([h for h in headers if str(h).strip()]) or len(headers)
            if llm_headers_len <= 0:
                skipped += 1
                continue

            table_index = _select_table_index_for_llm_table(page=page, llm_headers_len=llm_headers_len, shapes=shapes)
            if table_index is None:
                ambiguous_tables += 1
                continue

            min_row = int((shapes.get(page) or {}).get(table_index, {}).get("min_row", 1))
            cells = grid.get("cells") or []
            if not isinstance(cells, list):
                continue

            for cell in cells:
                if not isinstance(cell, dict):
                    continue
                try:
                    llm_row = int(cell.get("row_index"))
                    llm_col = int(cell.get("col_index"))
                except Exception:
                    skipped += 1
                    continue
                value = cell.get("value")
                value_text = "" if value is None else str(value).strip()
                if not value_text:
                    continue

                target_row = min_row + llm_row
                target_col = llm_col
                key = _CellKey(page=page, table_index=table_index, row_index=target_row, col_index=target_col)
                target = cell_index.get(key)
                if target is None:
                    missing_cells += 1
                    continue

                existing = str(target.get("answer", "") or "").strip()
                if only_if_empty and existing not in {"", "N/D"}:
                    skipped += 1
                    continue

                target["answer"] = value_text
                target["confidence"] = float(target.get("confidence", 0.0) or 0.0)
                target["reason"] = "vision_table"
                applied += 1

    mapping_payload["rows"] = rows
    _dump_json(mapping_path, mapping_payload)
    return {
        "mapping_path": str(mapping_path),
        "tables_filled_json_path": str(tables_path),
        "applied": applied,
        "skipped": skipped,
        "ambiguous_tables": ambiguous_tables,
        "missing_cells": missing_cells,
    }

