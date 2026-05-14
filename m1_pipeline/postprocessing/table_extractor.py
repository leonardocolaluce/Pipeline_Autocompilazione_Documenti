#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

import cv2  # type: ignore
import fitz  # PyMuPDF  # type: ignore
import numpy as np


Orientation = Literal["horizontal", "vertical"]


@dataclass(frozen=True)
class LineSeg:
    x1: float
    y1: float
    x2: float
    y2: float
    orientation: Orientation
    source: str  # "vector" | "raster"

    def as_json(self) -> dict[str, Any]:
        return {
            "x1": float(self.x1),
            "y1": float(self.y1),
            "x2": float(self.x2),
            "y2": float(self.y2),
            "orientation": self.orientation,
            "source": self.source,
        }


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _to_posix_path(user_path: str) -> Path:
    p = (user_path or "").strip().strip('"').strip("'")
    if not p:
        raise ValueError("empty path")
    if os.name != "nt" and len(p) >= 3 and p[1:3] == ":\\":
        drive = p[0].lower()
        rest = p[2:].lstrip("\\/").replace("\\", "/")
        return Path(f"/mnt/{drive}/{rest}").expanduser().resolve()
    return Path(p).expanduser().resolve()


def _render_page_rgb(page: fitz.Page, zoom: float) -> np.ndarray:
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)


def _adaptive_binarize(gray: np.ndarray, block_size: int, c: int) -> np.ndarray:
    return cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        block_size,
        c,
    )


def _extract_lines_hough(
    mask: np.ndarray,
    orientation: Orientation,
    hough_threshold: int,
    min_line_length: int,
    max_line_gap: int,
) -> list[LineSeg]:
    lines = cv2.HoughLinesP(
        mask,
        rho=1,
        theta=np.pi / 180,
        threshold=hough_threshold,
        minLineLength=min_line_length,
        maxLineGap=max_line_gap,
    )
    if lines is None:
        return []
    out: list[LineSeg] = []
    for (x1, y1, x2, y2) in lines.reshape(-1, 4):
        out.append(LineSeg(float(x1), float(y1), float(x2), float(y2), orientation, "raster"))
    return out


def detect_lines_raster(
    rgb: np.ndarray,
    *,
    bin_block_size: int = 15,
    bin_c: int = 10,
    kernel_frac: float = 0.03,
    morph_iterations: int = 1,
    hough_threshold: int = 120,
    min_line_length_frac: float = 0.15,
    max_line_gap: int = 10,
) -> tuple[list[LineSeg], np.ndarray, np.ndarray]:
    if bin_block_size % 2 == 0 or bin_block_size < 3:
        raise ValueError("bin_block_size must be odd and >= 3")

    h, w = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    bw = _adaptive_binarize(gray, bin_block_size, bin_c)

    kernel_w = max(10, int(w * kernel_frac))
    kernel_h = max(10, int(h * kernel_frac))
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 1))
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, kernel_h))

    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN, horiz_kernel, iterations=morph_iterations)
    vert = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vert_kernel, iterations=morph_iterations)

    min_line_length = max(30, int(min(w, h) * min_line_length_frac))
    lines_h = _extract_lines_hough(
        horiz,
        "horizontal",
        hough_threshold=hough_threshold,
        min_line_length=min_line_length,
        max_line_gap=max_line_gap,
    )
    lines_v = _extract_lines_hough(
        vert,
        "vertical",
        hough_threshold=hough_threshold,
        min_line_length=min_line_length,
        max_line_gap=max_line_gap,
    )
    return (lines_h + lines_v, horiz, vert)


def detect_lines_raster_multiscale(rgb: np.ndarray) -> list[LineSeg]:
    """
    Union of multiple raster detections to better catch both strong and very light table strokes.
    Returned coordinates are in image pixels.
    """
    h, w = rgb.shape[:2]
    # Local contrast boost for faint grey strokes
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray_boost = clahe.apply(gray)
    rgb_boost = cv2.cvtColor(gray_boost, cv2.COLOR_GRAY2RGB)

    configs = [
        # Default-ish
        dict(bin_block_size=15, bin_c=10, kernel_frac=0.03, morph_iterations=1, hough_threshold=120, min_line_length_frac=0.15, max_line_gap=10),
        # More sensitive to faint lines (lower C, lower threshold, longer kernels)
        dict(bin_block_size=21, bin_c=2, kernel_frac=0.02, morph_iterations=1, hough_threshold=80, min_line_length_frac=0.10, max_line_gap=20),
        # Very sensitive but requires longer min length (helps grid lines)
        dict(bin_block_size=25, bin_c=0, kernel_frac=0.015, morph_iterations=1, hough_threshold=70, min_line_length_frac=0.12, max_line_gap=25),
    ]

    out: list[LineSeg] = []
    for cfg in configs:
        lines, _, _ = detect_lines_raster(rgb, **cfg)
        out.extend(lines)
        lines2, _, _ = detect_lines_raster(rgb_boost, **cfg)
        out.extend(lines2)

    # De-duplicate roughly in image space
    # Bucket by quantized endpoints
    seen: set[tuple[int, int, int, int, str]] = set()
    uniq: list[LineSeg] = []
    q = max(2, int(min(h, w) * 0.002))  # ~0.2%
    for ln in out:
        x1, y1, x2, y2 = float(ln.x1), float(ln.y1), float(ln.x2), float(ln.y2)
        key = (int(round(x1 / q)), int(round(y1 / q)), int(round(x2 / q)), int(round(y2 / q)), ln.orientation)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(ln)
    return uniq


def detect_tables_by_morphology(
    rgb: np.ndarray,
    *,
    bin_block_size: int = 21,
    bin_c: int = 7,
    kernel_scale: float = 30.0,
    morph_iterations: int = 1,
    min_area_frac: float = 0.002,
    min_w_frac: float = 0.20,
    min_h_frac: float = 0.06,
) -> tuple[list[tuple[int, int, int, int]], np.ndarray, np.ndarray]:
    """
    Returns (bboxes_xywh, horiz_mask, vert_mask) in IMAGE coordinates.
    Bboxes are filtered candidates for table-like grids.
    """
    h, w = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    if bin_block_size % 2 == 0:
        bin_block_size += 1
    bw = _adaptive_binarize(gray, int(bin_block_size), int(bin_c))

    kx = max(25, int(w / float(kernel_scale)))
    ky = max(25, int(h / float(kernel_scale)))
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kx, 1))
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, ky))
    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN, horiz_kernel, iterations=int(morph_iterations))
    vert = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vert_kernel, iterations=int(morph_iterations))

    grid = cv2.bitwise_or(horiz, vert)
    # Connect broken segments a bit
    grid = cv2.dilate(grid, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=1)

    contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    bboxes: list[tuple[int, int, int, int]] = []
    min_area = float(w * h) * float(min_area_frac)
    for c in contours:
        x, y, ww, hh = cv2.boundingRect(c)
        area = float(ww * hh)
        if area < min_area:
            continue
        if ww < int(w * float(min_w_frac)) or hh < int(h * float(min_h_frac)):
            continue
        bboxes.append((int(x), int(y), int(ww), int(hh)))

    # Sort top-to-bottom then left-to-right
    bboxes.sort(key=lambda b: (b[1], b[0]))
    return bboxes, horiz, vert

def _vector_lines_from_drawings(page: fitz.Page, *, angle_tol_deg: float = 2.0, min_len: float = 2.0) -> list[LineSeg]:
    out: list[LineSeg] = []
    drawings = page.get_drawings()
    for d in drawings:
        # Only consider stroked shapes (table borders). Filled rectangles are often text backgrounds.
        stroke_color = d.get("color", None)
        if stroke_color is None:
            continue
        for it in d.get("items", []):
            if not it:
                continue
            kind = it[0]
            if kind == "l":  # line
                p1, p2 = it[1], it[2]
                x1, y1, x2, y2 = float(p1.x), float(p1.y), float(p2.x), float(p2.y)
                dx, dy = (x2 - x1), (y2 - y1)
                length = float((dx * dx + dy * dy) ** 0.5)
                if length < min_len:
                    continue
                ang = abs(np.degrees(np.arctan2(dy, dx)))
                # Normalize angle to [0, 90]
                if ang > 90:
                    ang = 180 - ang
                if ang <= angle_tol_deg:
                    out.append(LineSeg(x1, y1, x2, y2, "horizontal", "vector"))
                elif abs(90 - ang) <= angle_tol_deg:
                    out.append(LineSeg(x1, y1, x2, y2, "vertical", "vector"))
            elif kind == "re":  # rectangle
                r = it[1]
                x0, y0, x1, y1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
                if abs(x1 - x0) < min_len or abs(y1 - y0) < min_len:
                    continue
                out.extend(
                    [
                        LineSeg(x0, y0, x1, y0, "horizontal", "vector"),
                        LineSeg(x0, y1, x1, y1, "horizontal", "vector"),
                        LineSeg(x0, y0, x0, y1, "vertical", "vector"),
                        LineSeg(x1, y0, x1, y1, "vertical", "vector"),
                    ]
                )
    return out


def _normalize_line(seg: LineSeg) -> LineSeg:
    if seg.orientation == "horizontal":
        if seg.x2 < seg.x1:
            return LineSeg(seg.x2, seg.y2, seg.x1, seg.y1, seg.orientation, seg.source)
    else:
        if seg.y2 < seg.y1:
            return LineSeg(seg.x2, seg.y2, seg.x1, seg.y1, seg.orientation, seg.source)
    return seg


def _merge_collinear(lines: Sequence[LineSeg], *, pos_tol: float, gap_tol: float) -> list[LineSeg]:
    """
    Merge nearly-collinear segments (same orientation) that overlap/touch.
    pos_tol: tolerance on constant coordinate (y for horizontal, x for vertical).
    gap_tol: tolerance to bridge small gaps.
    """
    if not lines:
        return []
    lines_n = [_normalize_line(l) for l in lines]

    if lines_n[0].orientation == "horizontal":
        key = lambda l: l.y1
        span_a = lambda l: l.x1
        span_b = lambda l: l.x2
        make = lambda y, a, b, src: LineSeg(a, y, b, y, "horizontal", src)
    else:
        key = lambda l: l.x1
        span_a = lambda l: l.y1
        span_b = lambda l: l.y2
        make = lambda x, a, b, src: LineSeg(x, a, x, b, "vertical", src)

    lines_sorted = sorted(lines_n, key=lambda l: (round(float(key(l)) / pos_tol) if pos_tol > 0 else float(key(l)), span_a(l)))
    merged: list[LineSeg] = []
    cur = lines_sorted[0]
    cur_pos = float(key(cur))
    cur_a = float(span_a(cur))
    cur_b = float(span_b(cur))
    cur_src = cur.source

    def src_merge(a: str, b: str) -> str:
        if a == b:
            return a
        return "mixed"

    for ln in lines_sorted[1:]:
        pos = float(key(ln))
        a = float(span_a(ln))
        b = float(span_b(ln))
        if abs(pos - cur_pos) <= pos_tol and a <= (cur_b + gap_tol) and b >= (cur_a - gap_tol):
            cur_pos = (cur_pos + pos) / 2.0
            cur_a = min(cur_a, a)
            cur_b = max(cur_b, b)
            cur_src = src_merge(cur_src, ln.source)
        else:
            merged.append(make(cur_pos, cur_a, cur_b, cur_src))
            cur_pos, cur_a, cur_b, cur_src = pos, a, b, ln.source
    merged.append(make(cur_pos, cur_a, cur_b, cur_src))
    return merged


def _cluster_coords(values: Sequence[float], tol: float) -> list[float]:
    if not values:
        return []
    vals = sorted(float(v) for v in values)
    out: list[float] = []
    cur = [vals[0]]
    for v in vals[1:]:
        if abs(v - cur[-1]) <= tol:
            cur.append(v)
        else:
            out.append(float(sum(cur) / len(cur)))
            cur = [v]
    out.append(float(sum(cur) / len(cur)))
    return out


def _segment_covers_horizontal(lines: Sequence[LineSeg], y: float, x0: float, x1: float, *, pos_tol: float, cover_tol: float) -> bool:
    if x1 < x0:
        x0, x1 = x1, x0
    for ln in lines:
        if ln.orientation != "horizontal":
            continue
        if abs(ln.y1 - y) > pos_tol:
            continue
        a, b = (min(ln.x1, ln.x2), max(ln.x1, ln.x2))
        if a <= (x0 + cover_tol) and b >= (x1 - cover_tol):
            return True
    return False


def _segment_covers_vertical(lines: Sequence[LineSeg], x: float, y0: float, y1: float, *, pos_tol: float, cover_tol: float) -> bool:
    if y1 < y0:
        y0, y1 = y1, y0
    for ln in lines:
        if ln.orientation != "vertical":
            continue
        if abs(ln.x1 - x) > pos_tol:
            continue
        a, b = (min(ln.y1, ln.y2), max(ln.y1, ln.y2))
        if a <= (y0 + cover_tol) and b >= (y1 - cover_tol):
            return True
    return False


def _nearest_dist(values: Sequence[float], v: float) -> float:
    if not values:
        return float("inf")
    return float(min(abs(float(x) - float(v)) for x in values))


def _find_vertical_peaks(mask: np.ndarray, *, min_sep_px: int, rel_threshold: float) -> list[int]:
    """
    Find x positions of strong vertical strokes in a binary mask (white=foreground).
    Returns x indices in mask coordinates.
    """
    if mask.size == 0:
        return []
    # column density
    col = mask.astype(np.uint8).sum(axis=0).astype(np.float32)
    if col.size == 0:
        return []
    # smooth
    k = max(3, int(min_sep_px // 2) * 2 + 1)
    col_s = cv2.GaussianBlur(col.reshape(1, -1), (k, 1), 0).reshape(-1)
    m = float(col_s.max()) if col_s.size else 0.0
    if m <= 0:
        return []
    thr = m * float(rel_threshold)
    # candidate indices above threshold
    idx = np.where(col_s >= thr)[0].tolist()
    if not idx:
        return []
    # group contiguous runs, pick max per run
    peaks: list[int] = []
    run = [idx[0]]
    for x in idx[1:]:
        if x == run[-1] + 1:
            run.append(x)
        else:
            # pick best
            best = max(run, key=lambda i: float(col_s[i]))
            peaks.append(int(best))
            run = [x]
    best = max(run, key=lambda i: float(col_s[i]))
    peaks.append(int(best))
    # enforce minimum separation (keep strongest)
    peaks_sorted = sorted(peaks, key=lambda i: float(col_s[i]), reverse=True)
    kept: list[int] = []
    for x in peaks_sorted:
        if all(abs(x - kx) >= int(min_sep_px) for kx in kept):
            kept.append(int(x))
    return sorted(kept)


def _intersections(horiz: Sequence[LineSeg], vert: Sequence[LineSeg], *, pos_tol: float) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for h in horiz:
        y = float(h.y1)
        hx0, hx1 = (min(h.x1, h.x2), max(h.x1, h.x2))
        for v in vert:
            x = float(v.x1)
            vy0, vy1 = (min(v.y1, v.y2), max(v.y1, v.y2))
            if (hx0 - pos_tol) <= x <= (hx1 + pos_tol) and (vy0 - pos_tol) <= y <= (vy1 + pos_tol):
                pts.append((x, y))
    return pts


def _bbox_from_points(points: Sequence[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))

def _compact_grid_coords(
    coords: Sequence[float],
    *,
    min_gap: float = 3.0,
    mode: str = "mean",
) -> list[float]:
    """
    Compatta coordinate troppo vicine tra loro.
    Utile per eliminare micro righe o doppie linee raster.

    Esempio:
    [629.4, 631.3, 665.7, 667.2, 668.5, 689.0]
    diventa circa:
    [630.35, 667.13, 689.0]
    """
    if not coords:
        return []

    values = sorted(float(v) for v in coords)
    groups: list[list[float]] = [[values[0]]]

    for v in values[1:]:
        if abs(v - groups[-1][-1]) <= min_gap:
            groups[-1].append(v)
        else:
            groups.append([v])

    compacted: list[float] = []
    for g in groups:
        if mode == "first":
            compacted.append(g[0])
        elif mode == "last":
            compacted.append(g[-1])
        else:
            compacted.append(sum(g) / len(g))

    return compacted

def _cluster_points(points: Sequence[tuple[float, float]], *, eps: float, min_points: int) -> list[list[tuple[float, float]]]:
    """
    Simple grid-hash clustering: points within eps (L_inf neighborhood via buckets + exact check) belong to same cluster.
    """
    if not points:
        return []
    pts = [(float(x), float(y)) for (x, y) in points]
    cell = max(1e-6, float(eps))

    buckets: dict[tuple[int, int], list[int]] = {}
    for i, (x, y) in enumerate(pts):
        key = (int(x // cell), int(y // cell))
        buckets.setdefault(key, []).append(i)

    visited = [False] * len(pts)
    clusters: list[list[tuple[float, float]]] = []

    def neighbors(idx: int) -> Iterable[int]:
        x, y = pts[idx]
        bx, by = (int(x // cell), int(y // cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in buckets.get((bx + dx, by + dy), []):
                    if j == idx:
                        continue
                    x2, y2 = pts[j]
                    if abs(x2 - x) <= eps and abs(y2 - y) <= eps:
                        yield j

    for i in range(len(pts)):
        if visited[i]:
            continue
        stack = [i]
        visited[i] = True
        comp: list[int] = []
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for j in neighbors(cur):
                if not visited[j]:
                    visited[j] = True
                    stack.append(j)
        if len(comp) >= int(min_points):
            clusters.append([pts[k] for k in comp])
    return clusters


def _overlay_lines(rgb: np.ndarray, lines_img: Iterable[LineSeg], *, thickness: int = 2) -> np.ndarray:
    # Draw EVERYTHING in green as requested.
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    for seg in lines_img:
        cv2.line(
            bgr,
            (int(round(seg.x1)), int(round(seg.y1))),
            (int(round(seg.x2)), int(round(seg.y2))),
            (0, 255, 0),
            thickness=thickness,
            lineType=cv2.LINE_AA,
        )
    return bgr


def _convert_office_to_pdf_via_powershell(src: Path, dst_pdf: Path) -> None:
    """
    Best-effort conversion on Windows hosts (WSL) via Word COM automation.
    Requires: powershell.exe + Microsoft Word installed.
    """
    _ensure_dir(dst_pdf.parent)
    def to_win(p: Path) -> str:
        ps = str(p)
        if ps.startswith("/mnt/") and len(ps) >= 7 and ps[5].isalpha() and ps[6] == "/":
            drive = ps[5].upper()
            rest = ps[7:].replace("/", "\\")
            return f"{drive}:\\{rest}"
        return ps

    src_win = to_win(src)
    dst_win = to_win(dst_pdf)
    # Escape for PowerShell single-quoted strings
    src_win_ps = src_win.replace("'", "''")
    dst_win_ps = dst_win.replace("'", "''")
    ps = f"""
$ErrorActionPreference = 'Stop'
$src = '{src_win_ps}'
$dst = '{dst_win_ps}'
$word = New-Object -ComObject Word.Application
$word.Visible = $false
try {{
  $doc = $word.Documents.Open($src, $false, $true)
  try {{
    $wdFormatPDF = 17
    $doc.SaveAs([ref]$dst, [ref]$wdFormatPDF)
  }} finally {{
    $doc.Close($false) | Out-Null
  }}
}} finally {{
  $word.Quit() | Out-Null
}}
"""
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], check=True)


def _process_pdf(pdf_path: Path, out_dir: Path, *, zoom: float) -> dict[str, Any]:
    pdf_out = out_dir / pdf_path.stem
    _ensure_dir(pdf_out)
    pages_summary: list[dict[str, Any]] = []

    with fitz.open(pdf_path) as doc:
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            rgb = _render_page_rgb(page, zoom=zoom)

            # Vector lines in PDF coords
            vec = _vector_lines_from_drawings(page)
            tables: list[dict[str, Any]] = []
            # Detect table regions (IMAGE coords)
            table_bboxes_img, horiz_mask, vert_mask = detect_tables_by_morphology(rgb)

            # Build table lines by running Hough inside each bbox on the morph masks.
            table_lines_img: list[LineSeg] = []
            for (bx, by, bw, bh) in table_bboxes_img:
                hx = horiz_mask[by : by + bh, bx : bx + bw]
                vx = vert_mask[by : by + bh, bx : bx + bw]

                # Hough params tuned for grids (ignore text)
                min_len = max(40, int(min(bw, bh) * 0.35))
                h_lines = _extract_lines_hough(hx, "horizontal", hough_threshold=140, min_line_length=min_len, max_line_gap=10)
                v_lines = _extract_lines_hough(vx, "vertical", hough_threshold=140, min_line_length=min_len, max_line_gap=10)

                # Offset to full image coords
                for ln in (h_lines + v_lines):
                    table_lines_img.append(
                        LineSeg(ln.x1 + bx, ln.y1 + by, ln.x2 + bx, ln.y2 + by, ln.orientation, "raster")
                    )

            # Convert table lines to PDF coords (and union with vector strokes that are grid-like)
            table_lines_pdf = [
                LineSeg(l.x1 / zoom, l.y1 / zoom, l.x2 / zoom, l.y2 / zoom, l.orientation, l.source) for l in table_lines_img
            ]

            # Merge with vector (stroked) lines, but only keep those that look like table rules (long-ish).
            all_pdf = [_normalize_line(l) for l in (vec + table_lines_pdf)]
            horiz_pdf = [l for l in all_pdf if l.orientation == "horizontal"]
            vert_pdf = [l for l in all_pdf if l.orientation == "vertical"]
            horiz_m = _merge_collinear(horiz_pdf, pos_tol=0.8, gap_tol=1.5)
            vert_m = _merge_collinear(vert_pdf, pos_tol=0.8, gap_tol=1.5)

            # For each morphology table bbox, build a grid and cells based only on lines within bbox.
            used_grid_lines_pdf: list[LineSeg] = []
            for tid, (bx, by, bw, bh) in enumerate(table_bboxes_img, start=1):
                x0, y0, x1, y1 = (bx / zoom, by / zoom, (bx + bw) / zoom, (by + bh) / zoom)
                pad = 1.5
                x0, y0, x1, y1 = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)

                h_tbl = [
                    l
                    for l in horiz_m
                    if (y0 <= l.y1 <= y1) and not (max(l.x1, l.x2) < x0 or min(l.x1, l.x2) > x1)
                ]
                v_tbl = [
                    l
                    for l in vert_m
                    if (x0 <= l.x1 <= x1) and not (max(l.y1, l.y2) < y0 or min(l.y1, l.y2) > y1)
                ]

                # Keep only long rules (ignore short internal strokes)
                bw_pdf = max(1.0, x1 - x0)
                bh_pdf = max(1.0, y1 - y0)
                h_tbl = [l for l in h_tbl if abs(l.x2 - l.x1) >= 0.6 * bw_pdf]
                v_tbl = [l for l in v_tbl if abs(l.y2 - l.y1) >= 0.6 * bh_pdf]

                # Horizontal grid coords are reliable: derive ys only from horizontal rules.
                ys = sorted(_cluster_coords([l.y1 for l in h_tbl], 1.2))
                ys = _compact_grid_coords_px(ys, min_gap_px=20.0, zoom=zoom)

                # Vertical grid coords are sometimes broken/missing in the PDFs: derive xs from
                # detected vertical rules, and also from a dedicated (more sensitive) vertical mask projection.
                xs = sorted(_cluster_coords([l.x1 for l in v_tbl], 2.0))
                # Build a more sensitive vertical mask for this bbox (helps when vertical strokes are faint/broken)
                roi_rgb = rgb[by : by + bh, bx : bx + bw]
                roi_gray = cv2.cvtColor(roi_rgb, cv2.COLOR_RGB2GRAY)
                bw_v = _adaptive_binarize(roi_gray, 21, 2)
                ky = max(25, int(bh / 30.0))
                vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, ky))
                roi_v = cv2.morphologyEx(bw_v, cv2.MORPH_OPEN, vert_kernel, iterations=1)
                # Connect small gaps in vertical strokes (common where text overlaps)
                roi_v = cv2.dilate(roi_v, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5)), iterations=1)

                # Find vertical dividers by requiring persistence across multiple row-bands (filters header underlines/boxes).
                peak_counts: dict[int, int] = {}
                if len(ys) >= 2:
                    ys_img = [int(round((float(y) * zoom) - by)) for y in ys]
                    ys_img = [max(0, min(bh, y)) for y in ys_img]
                    for y0b, y1b in zip(ys_img[:-1], ys_img[1:], strict=False):
                        if y1b <= y0b:
                            continue
                        band = roi_v[y0b:y1b, :]
                        min_sep = max(10, int(bw * 0.06))
                        for px in _find_vertical_peaks(band, min_sep_px=min_sep, rel_threshold=0.18):
                            peak_counts[int(px)] = peak_counts.get(int(px), 0) + 1

                    band_count = max(1, len(ys_img) - 1)
                    min_votes = 2 if band_count < 4 else max(2, int(round(band_count * 0.45)))
                    peaks = [px for px, cnt in peak_counts.items() if cnt >= min_votes]
                else:
                    min_sep = max(10, int(bw * 0.06))
                    peaks = _find_vertical_peaks(roi_v, min_sep_px=min_sep, rel_threshold=0.20)

                # Always include strong peaks if they exist; clamp to a reasonable count to avoid "too many lines".
                if peak_counts:
                    peaks = sorted(peaks, key=lambda p: (-peak_counts.get(int(p), 0), int(p)))
                else:
                    peaks = sorted(int(p) for p in peaks)
                peaks = peaks[:12]

                for px in peaks:
                    xs.append((bx + float(px)) / zoom)
                xs = sorted(_cluster_coords(xs, 2.0))
                xs = _compact_grid_coords_px(xs, min_gap_px=20.0, zoom=zoom)

                cols = max(0, len(xs) - 1)
                rows = max(0, len(ys) - 1)
                # Discard framed text boxes etc. Keep only real grids.
                if cols < 2 or rows < 2:
                    continue

                # Grid-only line filtering:
                # keep only rules that sit on the grid coordinates and span most of the table bbox.
                x_min, x_max = float(xs[0]), float(xs[-1])
                y_min, y_max = float(ys[0]), float(ys[-1])
                table_w = max(1.0, x_max - x_min)
                table_h = max(1.0, y_max - y_min)

                def keep_h(ln: LineSeg) -> bool:
                    if _nearest_dist(ys, ln.y1) > 1.2:
                        return False
                    span = abs(float(ln.x2) - float(ln.x1))
                    # Underlines/labels are usually shorter than the grid width
                    if span < 0.85 * table_w:
                        return False
                    return True

                def keep_v(ln: LineSeg) -> bool:
                    if _nearest_dist(xs, ln.x1) > 1.2:
                        return False
                    span = abs(float(ln.y2) - float(ln.y1))
                    if span < 0.85 * table_h:
                        return False
                    return True

                h_grid = [l for l in h_tbl if keep_h(l)]
                # Vertical rules: prefer existing vector/raster segments, but also add full-height inferred dividers
                # at every grid x (so missing vertical strokes don't drop the column structure).
                v_grid = [l for l in v_tbl if keep_v(l)]
                if xs and ys:
                    y_min, y_max = float(ys[0]), float(ys[-1])
                    for gx in xs:
                        if _nearest_dist([l.x1 for l in v_grid], gx) <= 1.0:
                            continue
                        v_grid.append(LineSeg(gx, y_min, gx, y_max, "vertical", "inferred"))
                used_grid_lines_pdf.extend(h_grid)
                used_grid_lines_pdf.extend(v_grid)

                cells: list[dict[str, Any]] = []
                if len(xs) >= 2 and len(ys) >= 2:
                    for r in range(len(ys) - 1):
                        for c in range(len(xs) - 1):
                            cx0, cx1 = xs[c], xs[c + 1]
                            cy0, cy1 = ys[r], ys[r + 1]
                            top = _segment_covers_horizontal(h_grid, ys[r], cx0, cx1, pos_tol=1.0, cover_tol=2.0)
                            bot = _segment_covers_horizontal(h_grid, ys[r + 1], cx0, cx1, pos_tol=1.0, cover_tol=2.0)
                            left = _segment_covers_vertical(v_grid, xs[c], cy0, cy1, pos_tol=1.0, cover_tol=2.0)
                            right = _segment_covers_vertical(v_grid, xs[c + 1], cy0, cy1, pos_tol=1.0, cover_tol=2.0)
                            if top and bot and left and right:
                                cells.append(
                                    {
                                        "row": int(r),
                                        "col": int(c),
                                        "bbox_pdf": {"x0": float(cx0), "y0": float(cy0), "x1": float(cx1), "y1": float(cy1)},
                                    }
                                )

                tables.append(
                    {
                        "table_id": int(tid),
                        "bbox_pdf": {"x0": float(x0), "y0": float(y0), "x1": float(x1), "y1": float(y1)},
                        "grid_pdf": {"xs": [float(x) for x in xs], "ys": [float(y) for y in ys]},
                        "cells": cells,
                    }
                )

            # Overlay/JSON lines: ONLY grid lines that define cells (no underlines/text boxes).
            # Merge horizontals and verticals separately.
            candidates = [_normalize_line(l) for l in used_grid_lines_pdf]
            merged_h = _merge_collinear([l for l in candidates if l.orientation == "horizontal"], pos_tol=0.8, gap_tol=1.5)
            merged_v = _merge_collinear([l for l in candidates if l.orientation == "vertical"], pos_tol=0.8, gap_tol=1.5)
            merged_pdf = merged_h + merged_v
            merged_img = [LineSeg(l.x1 * zoom, l.y1 * zoom, l.x2 * zoom, l.y2 * zoom, l.orientation, l.source) for l in merged_pdf]
            overlay = _overlay_lines(rgb, merged_img, thickness=2)

            page_num = page_index + 1
            img_path = pdf_out / f"page_{page_num:03d}_tables.png"
            json_path = pdf_out / f"page_{page_num:03d}.json"
            cv2.imwrite(str(img_path), overlay)

            h, w = rgb.shape[:2]
            page_data = {
                "source_pdf": str(pdf_path),
                "page": int(page_num),
                "zoom": float(zoom),
                "image": {"path": str(img_path), "width": int(w), "height": int(h)},
                "lines_pdf": [l.as_json() for l in merged_pdf],
                "lines_image": [l.as_json() for l in merged_img],
                "tables": tables,
            }
            _write_json(json_path, page_data)
            pages_summary.append(
                {
                    "page": int(page_num),
                    "json": str(json_path),
                    "overlay_png": str(img_path),
                    "line_count": int(len(merged_pdf)),
                    "table_count": int(len(tables)),
                }
            )

    pdf_summary = {"source_pdf": str(pdf_path), "output_dir": str(pdf_out), "pages": pages_summary}
    _write_json(pdf_out / "summary.json", pdf_summary)
    return pdf_summary


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Estrae linee/celle di tabelle da PDF nativi (vector + raster) e genera JSON + overlay PNG (linee verdi)."
    )
    ap.add_argument("--input-dir", required=True, help="Cartella con PDF/DOC/DOCX (può essere path Windows tipo C:\\...).")
    ap.add_argument("--output-dir", default="prova/out", help="Cartella di output (default: prova/out).")
    ap.add_argument("--zoom", type=float, default=2.0, help="Zoom rendering (default: 2.0).")
    ap.add_argument(
        "--convert-office",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Converte .doc/.docx in PDF via powershell/Word (default: true).",
    )
    args = ap.parse_args(list(argv) if argv is not None else None)

    input_dir = _to_posix_path(str(args.input_dir))
    if not input_dir.exists() or not input_dir.is_dir():
        print(f"Input dir not found: {input_dir}", file=sys.stderr)
        return 2

    out_dir = Path(str(args.output_dir)).expanduser().resolve()
    _ensure_dir(out_dir)
    converted_dir = out_dir / "_converted_pdfs"
    _ensure_dir(converted_dir)

    # Collect files
    src_files = sorted([p for p in input_dir.iterdir() if p.is_file()])
    pdfs: list[Path] = []
    conversions: list[dict[str, Any]] = []

    for p in src_files:
        suf = p.suffix.lower()
        if suf == ".pdf":
            pdfs.append(p)
        elif suf in {".doc", ".docx"} and bool(args.convert_office):
            # Convert into our output area
            out_pdf = converted_dir / f"{p.stem}.pdf"
            try:
                need = True
                if out_pdf.exists():
                    try:
                        need = out_pdf.stat().st_mtime < p.stat().st_mtime
                    except OSError:
                        need = True
                if need:
                    _convert_office_to_pdf_via_powershell(p, out_pdf)
                pdfs.append(out_pdf)
                conversions.append({"source": str(p), "pdf": str(out_pdf), "status": "ok"})
            except Exception as e:  # noqa: BLE001 - we want robust batch behavior
                conversions.append({"source": str(p), "pdf": str(out_pdf), "status": "error", "error": str(e)})

    if not pdfs:
        print(f"Nessun PDF (o conversione riuscita) trovato in: {input_dir}", file=sys.stderr)
        _write_json(out_dir / "summary.json", {"input_dir": str(input_dir), "pdfs": [], "conversions": conversions})
        return 3

    all_summaries: list[dict[str, Any]] = []
    for pdf in pdfs:
        try:
            all_summaries.append(_process_pdf(pdf, out_dir, zoom=float(args.zoom)))
        except Exception as e:  # noqa: BLE001
            all_summaries.append({"source_pdf": str(pdf), "status": "error", "error": str(e)})

    _write_json(out_dir / "summary.json", {"input_dir": str(input_dir), "pdfs": all_summaries, "conversions": conversions})
    print(f"Done. Output: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

def _compact_grid_coords_px(
    coords_pdf: Sequence[float],
    *,
    min_gap_px: float,
    zoom: float,
    mode: str = "mean",
) -> list[float]:
    min_gap_pdf = float(min_gap_px) / float(zoom)
    return _compact_grid_coords(coords_pdf, min_gap=min_gap_pdf, mode=mode)


# --- resolve pdf path helper (pipeline integration) ---
def _resolve_pdf_path(file_path: str | Path) -> Path:
    src = Path(file_path)
    if src.suffix.lower() == ".pdf" and src.exists():
        return src
    fallback = Path(__file__).with_name("input.pdf")
    if fallback.exists():
        return fallback
    raise FileNotFoundError(f"Nessun PDF disponibile per estrazione tabelle. Input: {src}")



def _bbox_pdf_to_xywh_list(b: dict[str, object] | None) -> list[float] | None:
    if not isinstance(b, dict):
        return None
    x0 = float(b.get("x0", 0.0) or 0.0)
    y0 = float(b.get("y0", 0.0) or 0.0)
    x1 = float(b.get("x1", 0.0) or 0.0)
    y1 = float(b.get("y1", 0.0) or 0.0)
    return [round(x0, 2), round(y0, 2), round(x1 - x0, 2), round(y1 - y0, 2)]

# --- extract_tables export guard ---
def extract_tables(file_path: str, blocks: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    """Compat per m1_pipeline/main.py: ritorna tabelle in formato M1 (bbox xywh + testo celle)."""
    del blocks
    pdf_path = _resolve_pdf_path(file_path)
    tmp_out = Path(__file__).resolve().parent / "_tmp_table_detect"
    tmp_out.mkdir(parents=True, exist_ok=True)

    # Usa la tua logica esistente (_process_pdf) senza modificarla.
    summary = _process_pdf(pdf_path, tmp_out, zoom=2.0)

    out: list[dict[str, object]] = []
    table_index = 0

    with fitz.open(pdf_path) as doc:
        for page_entry in (summary.get("pages") or []):
            page_num = int(page_entry.get("page", 1))
            page = doc.load_page(page_num - 1)
            payload = json.loads(Path(page_entry["json"]).read_text(encoding="utf-8"))

            for t in (payload.get("tables") or []):
                bb = t.get("bbox_pdf") or {}
                table_bbox = {
                    "x0": bb.get("x0", 0.0),
                    "y0": bb.get("y0", 0.0),
                    "x1": bb.get("x1", 0.0),
                    "y1": bb.get("y1", 0.0),
                }

                rows_map: dict[int, dict[int, dict[str, object]]] = {}
                max_col = -1

                for cell in (t.get("cells") or []):
                    r = int(cell.get("row", 0))
                    c = int(cell.get("col", 0))
                    max_col = max(max_col, c)

                    bb2 = cell.get("bbox_pdf") or {}
                    rect = fitz.Rect(
                        float(bb2.get("x0", 0.0)),
                        float(bb2.get("y0", 0.0)),
                        float(bb2.get("x1", 0.0)),
                        float(bb2.get("y1", 0.0)),
                    )

                    words = page.get_text("words", clip=rect) or []
                    words.sort(key=lambda w: (round(float(w[1]), 1), float(w[0])))
                    cell_text = " ".join(
                        str(w[4]).strip() for w in words if str(w[4]).strip()
                    ).strip()

                    xywh = [
                        round(float(rect.x0), 2),
                        round(float(rect.y0), 2),
                        round(float(rect.x1 - rect.x0), 2),
                        round(float(rect.y1 - rect.y0), 2),
                    ]

                    rows_map.setdefault(r, {})[c] = {
                        "colonna": f"col_{c}",
                        "testo": cell_text,
                        "fillable": (cell_text == ""),
                        "page": page_num,
                        "bbox": xywh,
                    }

                data_rows = []
                for r in sorted(rows_map.keys()):
                    cells_out = []
                    for c in range(max_col + 1):
                        item = rows_map[r].get(c)
                        if item is None:
                            item = {
                                "colonna": f"col_{c}",
                                "testo": "",
                                "fillable": True,
                                "page": page_num,
                                "bbox": None,
                            }
                        cells_out.append(item)
                    data_rows.append({"row_index": r + 1, "cells": cells_out})

                out.append(
                    {
                        "table_index": table_index,
                        "headers": [],
                        "header_cells": [],
                        "rows": data_rows,
                        "page": page_num,
                        "bbox": _bbox_pdf_to_xywh_list(table_bbox),
                    }
                )
                table_index += 1

    return out

