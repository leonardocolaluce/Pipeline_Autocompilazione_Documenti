from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple


_PAGE_RE = re.compile(r"page[_-]?0*(\d+)", re.I)
_TABLE_ID_RE = re.compile(r"t\s*(\d+)", re.I)


def _page_from_image(filename: str) -> int | None:
    match = _PAGE_RE.search(str(filename or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None




def _table_rank_from_id(table_id: str) -> int | None:
    m = _TABLE_ID_RE.fullmatch(str(table_id or "").strip())
    if not m:
        return None
    try:
        return int(m.group(1))
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


def _table_top_y_by_page(mapping_rows: List[Dict[str, Any]]) -> dict[int, dict[int, float]]:
    """
    Returns: {page: {table_index: top_y}} using cell bbox y (xywh -> y).
    """
    out: dict[int, dict[int, float]] = {}
    for row in mapping_rows:
        if str(row.get("item_type", "")).strip() != "table_cell":
            continue
        try:
            page = int(row.get("page") or 0)
            table_index = int(row.get("table_index"))
        except Exception:
            continue
        bbox = row.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4):
            continue
        try:
            y = float(bbox[1])
        except Exception:
            continue
        cur = (out.get(page) or {}).get(table_index)
        out.setdefault(page, {})[table_index] = y if cur is None else min(float(cur), y)
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


def _norm_text(s: str) -> str:
    s = str(s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _apply_kv_label_value(
    *,
    page: int,
    table_index: int,
    label: str,
    value_text: str,
    mapping_rows: List[Dict[str, Any]],
    cell_index: dict[_CellKey, Dict[str, Any]],
    only_if_empty: bool,
) -> bool:
    if not str(value_text or "").strip():
        return False
    nlabel = _norm_text(label)
    if not nlabel:
        return False

    for row in mapping_rows:
        if str(row.get("item_type", "")).strip() != "table_cell":
            continue
        try:
            if int(row.get("page") or 0) != int(page):
                continue
            if int(row.get("table_index")) != int(table_index):
                continue
            row_index = int(row.get("row_index"))
        except Exception:
            continue

        row_cells = row.get("row_cells") or []
        if not isinstance(row_cells, list):
            continue

        for cidx, cell in enumerate(row_cells):
            if not isinstance(cell, dict):
                continue
            testo = _norm_text(str(cell.get("testo", "") or ""))
            if testo and testo == nlabel:
                target_key = _CellKey(page=page, table_index=table_index, row_index=row_index, col_index=cidx + 1)
                target = cell_index.get(target_key)
                if target is None:
                    return False
                existing = str(target.get("answer", "") or "").strip()
                if only_if_empty and existing not in {"", "N/D"}:
                    return False
                target["answer"] = str(value_text)
                target["confidence"] = float(target.get("confidence", 0.0) or 0.0)
                target["reason"] = "vision_table_kv"
                return True

    return False


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


def _candidate_table_indices_on_page(
    *,
    page: int,
    shapes: dict[int, dict[int, dict[str, Any]]],
) -> list[int]:
    return sorted(int(ti) for ti in (shapes.get(page) or {}).keys())


def _select_table_index_by_cell_existence(
    *,
    page: int,
    llm_row: int,
    llm_col: int,
    table_rank: int | None,
    cell_index: dict[_CellKey, Dict[str, Any]],
    shapes: dict[int, dict[int, dict[str, Any]]],
    top_y: dict[int, dict[int, float]],
) -> int | None:
    """
    Fallback heuristic when header-based matching is impossible/ambiguous:
    pick the unique table_index on the same page where the target cell exists.
    """
    matches: list[int] = []
    for table_index in _candidate_table_indices_on_page(page=page, shapes=shapes):
        min_row = int((shapes.get(page) or {}).get(table_index, {}).get("min_row", 1))
        key = _CellKey(
            page=page,
            table_index=table_index,
            row_index=min_row + llm_row,
            col_index=llm_col,
        )
        if key in cell_index:
            matches.append(table_index)
    if len(matches) == 1:
        return matches[0]

    # Disambiguation: if we know the LLM table rank (t1,t2,...) pick table by vertical order.
    if table_rank is not None and table_rank >= 1:
        ordered = sorted(matches, key=lambda ti: float((top_y.get(page) or {}).get(ti, 1e9)))
        idx = table_rank - 1
        if 0 <= idx < len(ordered):
            return ordered[idx]

    return None


def merge_tables_filled_into_mapping(
    *,
    tables_filled_json_path: str | Path,
    mapping_json_path: str | Path,
    only_if_empty: bool = True,
) -> dict[str, Any]:
    """
    Writes table answers into `campo_valore_*.json` (mapping).

    Preferred input is `campi_tabelle.json` (matches with row_index/col_index). For backward
    compatibility, if `tables_filled_json_path` points to `tables_filled_output.json` but
    a sibling `campi_tabelle.json` exists, we will use that instead.

    Rigid rules:
    - Never cross pages: `page_002.png` -> page=2 only.
    - Never guess table_index if ambiguous: require a unique table_index on that page where the target cell exists.
    - Never guess row/col: require exact cell existence (table_index,row_index,col_index).
    - Row mapping is 0-based (LLM) -> mapping row_index = min_row_index_of_table + llm_row_index.
    """
    mapping_path = Path(mapping_json_path).resolve()
    tables_path = Path(tables_filled_json_path).resolve()
    if tables_path.name == "tables_filled_output.json":
        alt = tables_path.with_name("campi_tabelle.json")
        if alt.exists():
            tables_path = alt
    if not mapping_path.exists():
        raise FileNotFoundError(f"Mapping JSON non trovato: {mapping_path}")
    if not tables_path.exists():
        raise FileNotFoundError(f"Tables filled JSON non trovato: {tables_path}")

    mapping_payload = _load_json(mapping_path)
    rows = list(mapping_payload.get("rows") or [])
    cell_index = _index_table_cells(rows)
    shapes = _table_shapes_by_page(rows)
    top_y = _table_top_y_by_page(rows)

    tables_payload = _load_json(tables_path)

    # Two supported formats:
    # 1) campi_tabelle.json -> {"matches": [{"image_page": "...", "row_index": 0, "col_index": 1, "value": "..."}]}
    # 2) tables_filled_output.json -> {"images": [{"file": "...", "tables": [{"grid": {"cells": [...]}}]}]}
    matches = tables_payload.get("matches") if isinstance(tables_payload, dict) else None
    images = tables_payload.get("images") if isinstance(tables_payload, dict) else None
    if not isinstance(matches, list):
        matches = None
    if not isinstance(images, list):
        images = []

    applied = 0
    skipped = 0
    ambiguous_tables = 0
    missing_cells = 0

    def _apply_one(*, page: int, llm_row: int, llm_col: int, value_text: str, table_rank: int | None) -> None:
        nonlocal applied, skipped, ambiguous_tables, missing_cells
        table_index = _select_table_index_by_cell_existence(
            page=page,
            llm_row=llm_row,
            llm_col=llm_col,
            table_rank=table_rank,
            cell_index=cell_index,
            shapes=shapes,
            top_y=top_y,
        )
        if table_index is None:
            ambiguous_tables += 1
            return
        min_row = int((shapes.get(page) or {}).get(table_index, {}).get("min_row", 1))
        key = _CellKey(page=page, table_index=table_index, row_index=min_row + llm_row, col_index=llm_col)
        target = cell_index.get(key)
        if target is None:
            missing_cells += 1
            return
        existing = str(target.get("answer", "") or "").strip()
        if only_if_empty and existing not in {"", "N/D"}:
            skipped += 1
            return
        target["answer"] = value_text
        target["confidence"] = float(target.get("confidence", 0.0) or 0.0)
        target["reason"] = "vision_table"
        applied += 1

    if matches is not None:
        for m in matches:
            if not isinstance(m, dict):
                continue
            value_text = str(m.get("value") or "").strip()
            if not value_text:
                continue
            table_rank = _table_rank_from_id(str(m.get("table_id") or ""))
            page = _page_from_image(str(m.get("image_page") or ""))
            if page is None:
                skipped += 1
                continue

            table_type = str(m.get("table_type") or "").strip().lower()
            if table_type == "kv":
                if table_rank is None:
                    skipped += 1
                    continue
                ordered = sorted((top_y.get(page) or {}).items(), key=lambda kv: float(kv[1]))
                idx = table_rank - 1
                if not (0 <= idx < len(ordered)):
                    skipped += 1
                    continue
                table_index = int(ordered[idx][0])
                ok = _apply_kv_label_value(
                    page=page,
                    table_index=table_index,
                    label=str(m.get("label") or ""),
                    value_text=value_text,
                    mapping_rows=rows,
                    cell_index=cell_index,
                    only_if_empty=only_if_empty,
                )
                if ok:
                    applied += 1
                else:
                    skipped += 1
                continue

            try:
                llm_row = int(m.get("row_index"))
                llm_col = int(m.get("col_index"))
            except Exception:
                skipped += 1
                continue
            _apply_one(page=page, llm_row=llm_row, llm_col=llm_col, value_text=value_text, table_rank=table_rank)
    else:
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
                    value_text = str(cell.get("value") or "").strip()
                    if not value_text:
                        continue
                    _apply_one(page=page, llm_row=llm_row, llm_col=llm_col, value_text=value_text, table_rank=None)

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
