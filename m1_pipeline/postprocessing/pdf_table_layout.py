from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import fitz


@dataclass
class DetectedPdfTable:
    page: int
    bbox: Tuple[float, float, float, float]
    row_count: int
    col_count: int
    texts: List[List[str]]
    cell_bboxes: List[List[Optional[Tuple[float, float, float, float]]]]


def enrich_tables_with_pdf_layout(pdf_path: str | Path, tables: List[Dict[str, Any]]) -> None:
    if not tables:
        return

    detected = _detect_pdf_tables(pdf_path)
    if not detected:
        return

    used: set[int] = set()
    for table in tables:
        match_index, header_offset = _match_detected_table(table, detected, used)
        if match_index is None:
            continue
        used.add(match_index)
        _apply_detected_layout(table, detected[match_index], header_offset)


def _detect_pdf_tables(pdf_path: str | Path) -> List[DetectedPdfTable]:
    pdf_file = Path(pdf_path).resolve()
    out: List[DetectedPdfTable] = []
    with fitz.open(str(pdf_file)) as doc:
        for page_num, page in enumerate(doc, start=1):
            try:
                finder = page.find_tables()
            except Exception:
                continue
            tables = getattr(finder, "tables", None) or []
            for table in tables:
                detected = _normalize_detected_table(page_num, table)
                if detected:
                    out.append(detected)
    return out


def _normalize_detected_table(page_num: int, table: Any) -> Optional[DetectedPdfTable]:
    texts = _normalize_text_matrix(_safe_extract_table_text(table))
    row_count = len(texts)
    col_count = max((len(row) for row in texts), default=0)
    if row_count == 0 or col_count == 0:
        row_count = int(getattr(table, "row_count", 0) or 0)
        col_count = int(getattr(table, "col_count", 0) or 0)
    if row_count == 0 or col_count == 0:
        return None

    cell_bboxes = _extract_cell_bboxes(table, row_count, col_count)
    bbox = _rect_tuple(getattr(table, "bbox", None))
    if bbox is None:
        bbox = _derive_bbox_from_cells(cell_bboxes)
    if bbox is None:
        return None
    cell_bboxes = _fill_missing_cell_bboxes(cell_bboxes, bbox, row_count, col_count)

    return DetectedPdfTable(
        page=page_num,
        bbox=bbox,
        row_count=row_count,
        col_count=col_count,
        texts=texts,
        cell_bboxes=cell_bboxes,
    )


def _safe_extract_table_text(table: Any) -> List[List[str]]:
    try:
        extracted = table.extract()
    except Exception:
        return []
    if not isinstance(extracted, list):
        return []
    rows: List[List[str]] = []
    for row in extracted:
        if isinstance(row, list):
            rows.append([str(cell or "").strip() for cell in row])
    return rows


def _normalize_text_matrix(rows: List[List[str]]) -> List[List[str]]:
    width = max((len(row) for row in rows), default=0)
    return [row + [""] * (width - len(row)) for row in rows]


def _extract_cell_bboxes(table: Any, row_count: int, col_count: int) -> List[List[Optional[Tuple[float, float, float, float]]]]:
    row_objs = getattr(table, "rows", None)
    if row_objs:
        result: List[List[Optional[Tuple[float, float, float, float]]]] = []
        for row in row_objs[:row_count]:
            cells = list(getattr(row, "cells", None) or [])
            normalized = [_rect_tuple(cell) for cell in cells[:col_count]]
            normalized += [None] * (col_count - len(normalized))
            result.append(normalized)
        result += [[None] * col_count for _ in range(row_count - len(result))]
        return result

    flat_cells = list(getattr(table, "cells", None) or [])
    normalized_flat = [_rect_tuple(cell) for cell in flat_cells]
    result = [[None] * col_count for _ in range(row_count)]
    needed = row_count * col_count
    for index, rect in enumerate(normalized_flat[:needed]):
        row_idx = index // col_count
        col_idx = index % col_count
        result[row_idx][col_idx] = rect
    return result


def _rect_tuple(value: Any) -> Optional[Tuple[float, float, float, float]]:
    if value is None:
        return None
    if isinstance(value, fitz.Rect):
        x0, y0, x1, y1 = float(value.x0), float(value.y0), float(value.x1), float(value.y1)
    elif isinstance(value, Sequence) and len(value) == 4:
        x0, y0, x1, y1 = [float(item) for item in value]
    else:
        return None
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


def _derive_bbox_from_cells(cells: List[List[Optional[Tuple[float, float, float, float]]]]) -> Optional[Tuple[float, float, float, float]]:
    coords: List[Tuple[float, float, float, float]] = [
        rect for row in cells for rect in row if rect is not None
    ]
    if not coords:
        return None
    x0 = min(rect[0] for rect in coords)
    y0 = min(rect[1] for rect in coords)
    x1 = max(rect[0] + rect[2] for rect in coords)
    y1 = max(rect[1] + rect[3] for rect in coords)
    return (x0, y0, x1 - x0, y1 - y0)


def _match_detected_table(
    expected_table: Dict[str, Any],
    detected_tables: List[DetectedPdfTable],
    used: set[int],
) -> Tuple[Optional[int], int]:
    expected_headers = [str(value or "").strip() for value in expected_table.get("headers", [])]
    expected_cols = len(expected_headers)
    expected_rows = len(expected_table.get("rows", []))
    expected_row_labels = _collect_expected_row_labels(expected_table)

    best_index: Optional[int] = None
    best_offset = 0
    best_score: Optional[Tuple[int, int, int]] = None

    for idx, detected in enumerate(detected_tables):
        if idx in used:
            continue
        col_penalty = abs(detected.col_count - expected_cols)
        if col_penalty > max(1, expected_cols):
            continue

        header_row = detected.texts[0] if detected.texts else []
        header_match_score = _header_match_score(expected_headers, header_row)
        header_offset = 1 if header_match_score > 0 else 0
        row_penalty = abs((detected.row_count - header_offset) - expected_rows)
        detected_row_labels = _collect_detected_row_labels(detected, header_offset)
        row_label_score = _row_label_match_score(expected_row_labels, detected_row_labels)
        score = (col_penalty, row_penalty, -header_match_score, -row_label_score)

        if best_score is None or score < best_score:
            best_index = idx
            best_offset = header_offset
            best_score = score

    return best_index, best_offset


def _header_match_score(expected_headers: List[str], detected_headers: List[str]) -> int:
    score = 0
    normalized_detected = [_normalize_text(text) for text in detected_headers]
    for header in expected_headers:
        normalized_header = _normalize_text(header)
        if not normalized_header:
            continue
        for detected in normalized_detected:
            if not detected:
                continue
            if normalized_header == detected or normalized_header in detected or detected in normalized_header:
                score += 1
                break
    return score


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _apply_detected_layout(table: Dict[str, Any], detected: DetectedPdfTable, header_offset: int) -> None:
    table["page"] = detected.page
    table["bbox"] = list(detected.bbox)
    header_cells = table.get("header_cells") or []
    if detected.cell_bboxes:
        header_row_bboxes = detected.cell_bboxes[0]
        for col_idx, cell in enumerate(header_cells):
            if col_idx >= len(header_row_bboxes):
                continue
            rect = header_row_bboxes[col_idx]
            if rect is None:
                continue
            cell["page"] = detected.page
            cell["bbox"] = [round(value, 2) for value in rect]

    rows = table.get("rows", [])
    for row_idx, row in enumerate(rows):
        detected_row_index = row_idx + header_offset
        if detected_row_index >= len(detected.cell_bboxes):
            continue
        detected_cells = detected.cell_bboxes[detected_row_index]
        for col_idx, cell in enumerate(row.get("cells", [])):
            if col_idx >= len(detected_cells):
                continue
            rect = detected_cells[col_idx]
            if rect is None:
                continue
            cell["page"] = detected.page
            cell["bbox"] = [round(value, 2) for value in rect]


def _fill_missing_cell_bboxes(
    cells: List[List[Optional[Tuple[float, float, float, float]]]],
    table_bbox: Tuple[float, float, float, float],
    row_count: int,
    col_count: int,
) -> List[List[Optional[Tuple[float, float, float, float]]]]:
    x_edges = _build_axis_edges(cells, axis="x", total=col_count, start=table_bbox[0], size=table_bbox[2])
    y_edges = _build_axis_edges(cells, axis="y", total=row_count, start=table_bbox[1], size=table_bbox[3])

    out: List[List[Optional[Tuple[float, float, float, float]]]] = []
    for row_idx in range(row_count):
        row_out: List[Optional[Tuple[float, float, float, float]]] = []
        for col_idx in range(col_count):
            existing = cells[row_idx][col_idx] if row_idx < len(cells) and col_idx < len(cells[row_idx]) else None
            if existing is not None:
                row_out.append(existing)
                continue
            x0, x1 = x_edges[col_idx], x_edges[col_idx + 1]
            y0, y1 = y_edges[row_idx], y_edges[row_idx + 1]
            row_out.append((x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0)))
        out.append(row_out)
    return out


def _build_axis_edges(
    cells: List[List[Optional[Tuple[float, float, float, float]]]],
    *,
    axis: str,
    total: int,
    start: float,
    size: float,
) -> List[float]:
    edges: List[float] = [start, start + size]
    if axis == "x":
        for row in cells:
            for rect in row:
                if rect is None:
                    continue
                edges.append(rect[0])
                edges.append(rect[0] + rect[2])
    else:
        for row in cells:
            for rect in row:
                if rect is None:
                    continue
                edges.append(rect[1])
                edges.append(rect[1] + rect[3])

    rounded = sorted({round(value, 2) for value in edges})
    cleaned = _compress_edges(rounded)
    if len(cleaned) >= total + 1:
        return cleaned[: total + 1]

    step = size / total if total else size
    return [start + (step * index) for index in range(total)] + [start + size]


def _compress_edges(values: List[float], tolerance: float = 2.0) -> List[float]:
    if not values:
        return []
    out = [values[0]]
    for value in values[1:]:
        if abs(value - out[-1]) > tolerance:
            out.append(value)
    return out


def _collect_expected_row_labels(table: Dict[str, Any]) -> List[str]:
    labels: List[str] = []
    for row in table.get("rows", []):
        first_text = ""
        for cell in row.get("cells", []):
            first_text = str(cell.get("testo") or "").strip()
            if first_text:
                break
        if first_text:
            labels.append(_normalize_text(first_text))
    return labels


def _collect_detected_row_labels(table: DetectedPdfTable, header_offset: int) -> List[str]:
    labels: List[str] = []
    for row in table.texts[header_offset:]:
        if row:
            first_text = _normalize_text(row[0])
            if first_text:
                labels.append(first_text)
    return labels


def _row_label_match_score(expected: List[str], detected: List[str]) -> int:
    score = 0
    for value in expected:
        if not value:
            continue
        for candidate in detected:
            if not candidate:
                continue
            if value == candidate or value in candidate or candidate in value:
                score += 1
                break
    return score
