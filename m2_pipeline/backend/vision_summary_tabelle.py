import argparse
import base64
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed
from .step02_openai_json_utils import parse_json_text_tolerant


MISTRAL_API_URL = os.getenv("MISTRAL_API_URL", "https://api.mistral.ai/v1/chat/completions").strip()
DEFAULT_MODEL = os.getenv("MISTRAL_MODEL", "mistral-medium-2508").strip()

# key: INCOLLA_QUI_LA_TUA_MISTRAL_API_KEY
MISTRAL_API_KEY_FALLBACK = ""

def _page_number(path: Path) -> int | None:
    match = re.search(r"page[_-]?0*(\d+)", path.stem, re.I)
    return int(match.group(1)) if match else None

def _extract_text_from_response(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


def _extract_first_json(text: str) -> tuple[Any | None, str | None]:
    if not isinstance(text, str) or not text.strip():
        return None, "Risposta vuota."

    start = None
    opener = None
    for idx, ch in enumerate(text):
        if ch in "{[":
            start = idx
            opener = ch
            break
    if start is None or opener is None:
        return None, "Nessun JSON trovato nella risposta (manca '{' o '[')."

    closer = "}" if opener == "{" else "]"
    chunk = text[start:]

    depth = 0
    in_string = False
    escaped = False
    end_idx = None
    for i, ch in enumerate(chunk):
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break

    if end_idx is None:
        try:
            payload, parse_info = parse_json_text_tolerant(chunk)
    
            if isinstance(payload, dict):
                payload.setdefault("_parse_info", parse_info)
    
            return payload, None
    
        except Exception as exc:
            return None, f"JSON tronco non recuperabile: {exc}"

    candidate = chunk[:end_idx].strip()

    try:
        payload, parse_info = parse_json_text_tolerant(candidate)
    
        if isinstance(payload, dict):
            payload.setdefault("_parse_info", parse_info)
    
        return payload, None
    
    except Exception as exc:
        return None, f"JSON non recuperabile: {exc}"


def _encode_image_data_uri(image_path: Path) -> str:
    suffix = image_path.suffix.lower().lstrip(".")
    if suffix not in {"png", "jpg", "jpeg", "webp"}:
        suffix = "png"
    mime = "image/jpeg" if suffix in {"jpg", "jpeg"} else f"image/{suffix}"
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _build_prompt() -> str:
    return (
        "Sei un assistente VISION per riconoscere TABELLE/GRIGLIE *da compilare* su una pagina di modulistica.\n"
        "Input: 1 immagine (1 pagina).\n\n"
        "Obiettivo: individuare e descrivere SOLO le tabelle/griglie che sono effettivamente da compilare (campi vuoti).\n"
        "Considera valida anche una griglia a 2 colonne \"etichetta | valore\" (key-value), con testo a sinistra e spazio vuoto a destra.\n"
        "Considera valida anche una tabella dove ogni riga ha un titolo/etichetta a sinistra e il resto della riga è spazio/celle vuote da compilare.\n\n"
        "REGOLE:\n"
        "- Rispondi con SOLO JSON valido (niente testo extra).\n"
        "- Se non ci sono tabelle DA COMPILARE: restituisci {\"tables\":[]}.\n"
        "- NON considerare tabella: elenchi di opzioni con quadratini/checkbox/radio, righe singole da compilare con puntini \".....\" o linee \"_____\", o liste puntate.\n"
        "- Considera tabella SOLO se vedi una griglia evidente (linee che delimitano celle) oppure un layout ripetuto etichetta|valore su più righe.\n"
        "- Per table_type=\"grid\": richiedi una vera tabella con almeno 2 colonne e 2 righe (o comunque una griglia evidente con intestazioni di colonna). Se vedi solo 1 colonna o solo righe di testo separato, NON includere.\n"
        "- Per table_type=\"kv\": includi anche tabelle con 1 o 2 righe se contengono dati previdenziali/assicurativi come INPS, INAIL, CASSA EDILE, oppure contatti/sede/PEC/email.\n"
        "- Per table_type=\"kv\": negli altri casi includi se ci sono almeno 3 righe etichetta→valore/campo, con area valore allineata (spesso vuota) a destra. Se sono opzioni con checkbox, NON includere.\n"
        "- rows/cols devono essere numeri interi >0 quando possibile.\n"
        "- table_type: \"kv\" per tabelle etichetta|valore, altrimenti \"grid\".\n"
        "- headers: lista di stringhe SOLO se vedi intestazioni di colonna reali (non valori nelle celle).\n"
        "- ESCLUDI (non riportare) tabelle informative/non compilabili: se TUTTE o QUASI TUTTE le celle contengono già testo/valori (es. elenchi già compilati, tabelle di soglie, tabelle descrittive), allora NON è una tabella da compilare.\n"
        "- Se una griglia non ha intestazioni visibili ma contiene celle vuote da compilare, includila comunque usando headers generici: [\"col_0\", \"col_1\", ...].\n"
        "- ECCEZIONE: se è chiaramente una tabella da compilare con etichette fisse a sinistra e campi vuoti a destra, descrivila come table_type=\"kv\" (cols=2).\n\n"
        "SCHEMA OUTPUT:\n"
        "{\n"
        "  \"tables\": [\n"
        "    {\n"
        "      \"table_id\": \"t1\",\n"
        "      \"table_type\": \"kv\" | \"grid\",\n"
        "      \"rows\": 0,\n"
        "      \"cols\": 0,\n"
        "      \"headers\": [\"...\"]\n"
        "    }\n"
        "  ]\n"
        "}\n"
    )


def _list_images(folder: Path) -> list[Path]:
    if not folder.exists() or not folder.is_dir():
        return []
    return sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}],
        key=lambda p: p.name,
    )


def _select_image_folder(base_dir: Path) -> tuple[Path, list[Path]]:
    images = _list_images(base_dir)
    if images:
        return base_dir, images

    candidates: list[tuple[float, Path, list[Path]]] = []
    for sub in sorted([p for p in base_dir.iterdir() if p.is_dir()], key=lambda p: p.name):
        imgs = _list_images(sub)
        if imgs:
            try:
                mtime = sub.stat().st_mtime
            except Exception:
                mtime = 0.0
            candidates.append((mtime, sub, imgs))

    if not candidates:
        raise FileNotFoundError(f"Nessuna immagine trovata in: {base_dir} (né nelle sottocartelle immediate).")

    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1], candidates[0][2]


def _call_mistral(api_key: str, model: str, max_tokens: int, image_path: Path, retries: int, retry_wait: float, retry_max_wait: float) -> dict[str, Any]:
    body = {
        "model": model,
        "temperature": 0,
        "top_p": 1,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _build_prompt()},
                    {"type": "image_url", "image_url": _encode_image_data_uri(image_path)},
                ],
            }
        ],
    }

    req = request.Request(
        MISTRAL_API_URL,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )

    raw: dict[str, Any] | None = None
    wait_s = max(0.0, float(retry_wait))
    max_wait_s = max(wait_s, float(retry_max_wait))
    last_err: Exception | None = None

    for attempt in range(max(0, int(retries)) + 1):
        try:
            with request.urlopen(req, timeout=180) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            last_err = None
            break
        except HTTPError as exc:
            last_err = exc
            should_retry = exc.code == 429 or (500 <= exc.code < 600)
            if not should_retry or attempt >= int(retries):
                raise

            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after:
                try:
                    sleep_s = float(retry_after)
                except Exception:
                    sleep_s = wait_s
            else:
                sleep_s = wait_s

            sleep_s = min(max_wait_s, max(0.0, sleep_s) + random.uniform(0.0, 0.5))
            print(f"[vision] {image_path.name}: HTTP {exc.code} - retry {attempt + 1}/{retries} in {sleep_s:.1f}s")
            time.sleep(sleep_s)
            wait_s = min(max_wait_s, wait_s * 2 if wait_s > 0 else 1.0)
        except URLError as exc:
            last_err = exc
            if attempt >= int(retries):
                raise
            sleep_s = min(max_wait_s, max(0.0, wait_s) + random.uniform(0.0, 0.5))
            print(f"[vision] {image_path.name}: network error - retry {attempt + 1}/{retries} in {sleep_s:.1f}s")
            time.sleep(sleep_s)
            wait_s = min(max_wait_s, wait_s * 2 if wait_s > 0 else 1.0)

    if raw is None:
        raise RuntimeError(f"Richiesta fallita dopo i retry. Ultimo errore: {last_err}")
    return raw


def _coerce_tables_only(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict) and isinstance(obj.get("tables"), list):
        tables_out: list[dict[str, Any]] = []
        for idx, t in enumerate(obj.get("tables") or []):
            if not isinstance(t, dict):
                continue
            table_id = t.get("table_id") or f"t{idx+1}"
            table_type = t.get("table_type") or "grid"
            try:
                rows = int(t.get("rows") or 0)
            except Exception:
                rows = 0
            try:
                cols = int(t.get("cols") or 0)
            except Exception:
                cols = 0
            headers = t.get("headers")
            if not isinstance(headers, list):
                headers = []
            headers = [str(h).strip() for h in headers if str(h).strip()]
            tables_out.append(
                {
                    "table_id": str(table_id),
                    "table_type": str(table_type),
                    "rows": max(0, rows),
                    "cols": max(0, cols),
                    "headers": headers,
                }
            )
        return {"tables": tables_out}
    return {"tables": []}


def _build_fill_prompt(company_data: dict[str, Any], detected_tables: list[dict[str, Any]]) -> str:
    company_json = json.dumps(company_data, ensure_ascii=False, indent=2)
    tables_json = json.dumps(detected_tables, ensure_ascii=False, indent=2)
    return (
        "Sei un assistente VISION per COMPILARE tabelle/griglie di modulistica partendo da un'immagine e da un JSON anagrafica azienda.\n"
        "Input:\n"
        "- 1 immagine (1 pagina)\n"
        "- JSON anagrafica azienda (vedi sotto)\n"
        "- Lista tabelle già rilevate su questa pagina (vedi sotto). Devi considerare SOLO quelle.\n\n"
        "Obiettivo:\n"
        "- Ricostruire la struttura delle tabelle rilevate (etichette, intestazioni, righe visibili).\n"
        "- Compilare i campi vuoti usando ESCLUSIVAMENTE i dati presenti nell'anagrafica.\n\n"
        "REGOLE IMPORTANTI:\n"
        "- Rispondi con SOLO JSON valido (niente testo extra).\n"
        "- Compila ogni cella vuota quando il dato è ragionevolmente compatibile con intestazione, riga o posizione. Usa value vuoto solo se nessun dato del JSON è semanticamente compatibile.\n"
        "- NON aggiungere tabelle non presenti in 'TABELLE_RILEVATE'. Usa esattamente gli stessi table_id.\n"
        "- Se una tabella grid non ha headers, usa headers generici coerenti con il numero di colonne: col_0, col_1, col_2.\n"
        "- Se una tabella è già piena (celle già compilate), riportala comunque ma NON sovrascrivere: copia i valori già presenti come 'value'.\n"
        "- Se una tabella non è compilabile (è solo informativa), NON deve comparire.\n"
        "- Per ogni value compilato, aggiungi 'source' con un percorso tipo 'azienda.dati_fiscali.partita_iva' oppure 'soggetti_in_carica[0].codice_fiscale'.\n"
        "\n"
        "REGOLE SPECIFICHE POSIZIONI PREVIDENZIALI/ASSICURATIVE:\n"
        "- Se una tabella contiene righe INPS, INAIL, CASSA EDILE, compila tutte le celle disponibili usando questi mapping:\n"
        "  - Riga INPS:\n"
        "    - posizione/matricola/codice = azienda.inps.posizione oppure azienda.inps.matricola\n"
        "    - sede/comune = azienda.inps.comune\n"
        "    - indirizzo = azienda.inps.indirizzo\n"
        "    - provincia = azienda.inps.provincia\n"
        "  - Riga INAIL:\n"
        "    - codice ditta = azienda.inail.codice_ditta\n"
        "    - PAT/numero PAT = azienda.inail.numero_pat\n"
        "    - sede/comune = azienda.inail.comune\n"
        "    - provincia = azienda.inail.provincia\n"
        "  - Riga CASSA EDILE:\n"
        "    - codice ditta = azienda.cassa_edile.codice_ditta\n"
        "    - codice cassa = azienda.cassa_edile.codice_cassa\n"
        "    - sede/comune = azienda.cassa_edile.comune\n"
        "    - indirizzo = azienda.cassa_edile.indirizzo\n"
        "    - CAP = azienda.cassa_edile.cap\n"
        "- Se ci sono due celle vuote sulla stessa riga, usa la prima per posizione/codice/matricola e la seconda per sede/comune/indirizzo, quando coerente con le intestazioni visibili.\n"
        "\n"
        "- PER TABELLE GRID: devi restituire anche coordinate ESATTE:\n"
        "  - row_index (0-based) = indice riga nella tabella (ordine dall'alto verso il basso)\n"
        "  - col_index (0-based) = indice colonna in base a grid.headers nell'ordine fornito\n"
        "  - Non usare solo il nome colonna: row_index e col_index sono OBBLIGATORI per ogni cella compilata.\n\n"
        "SCHEMA OUTPUT:\n"
        "{\n"
        "  \"tables\": [\n"
        "    {\n"
        "      \"table_id\": \"t1\",\n"
        "      \"table_type\": \"kv\" | \"grid\",\n"
        "      \"headers\": [\"...\"] ,\n"
        "      \"rows\": [\n"
        "        {\n"
        "          \"label\": \"...\",\n"
        "          \"value\": \"...\",\n"
        "          \"source\": \"...\"\n"
        "        }\n"
        "      ],\n"
        "      \"grid\": {\n"
        "        \"headers\": [\"...\"],\n"
        "        \"cells\": [\n"
        "          {\n"
        "            \"row_index\": 0,\n"
        "            \"col_index\": 0,\n"
        "            \"value\": \"...\",\n"
        "            \"source\": \"...\"\n"
        "          }\n"
        "        ]\n"
        "      }\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "NOTE SUL FORMATO:\n"
        "- Se table_type=\"kv\": usa 'rows' (lista di label/value). 'grid' può essere omesso o null.\n"
        "- Se table_type=\"grid\": usa 'grid.headers' e 'grid.cells' (NON grid.rows).\n"
        "- In grid.cells inserisci SOLO celle con value non vuoto.\n\n"
        "ANAGRAFICA_AZIENDA:\n"
        f"{company_json}\n\n"
        "TABELLE_RILEVATE (usa solo queste):\n"
        f"{tables_json}\n"
    )


def _call_mistral_fill(
    api_key: str,
    model: str,
    max_tokens: int,
    image_path: Path,
    company_data: dict[str, Any],
    detected_tables: list[dict[str, Any]],
    retries: int,
    retry_wait: float,
    retry_max_wait: float,
) -> dict[str, Any]:
    body = {
        "model": model,
        "temperature": 0,
        "top_p": 1,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _build_fill_prompt(company_data, detected_tables)},
                    {"type": "image_url", "image_url": _encode_image_data_uri(image_path)},
                ],
            }
        ],
    }

    req = request.Request(
        MISTRAL_API_URL,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )

    raw: dict[str, Any] | None = None
    wait_s = max(0.0, float(retry_wait))
    max_wait_s = max(wait_s, float(retry_max_wait))
    last_err: Exception | None = None

    for attempt in range(max(0, int(retries)) + 1):
        try:
            with request.urlopen(req, timeout=180) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            last_err = None
            break
        except HTTPError as exc:
            last_err = exc
            should_retry = exc.code == 429 or (500 <= exc.code < 600)
            if not should_retry or attempt >= int(retries):
                raise

            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after:
                try:
                    sleep_s = float(retry_after)
                except Exception:
                    sleep_s = wait_s
            else:
                sleep_s = wait_s

            sleep_s = min(max_wait_s, max(0.0, sleep_s) + random.uniform(0.0, 0.5))
            print(f"[vision-fill] {image_path.name}: HTTP {exc.code} - retry {attempt + 1}/{retries} in {sleep_s:.1f}s")
            time.sleep(sleep_s)
            wait_s = min(max_wait_s, wait_s * 2 if wait_s > 0 else 1.0)
        except URLError as exc:
            last_err = exc
            if attempt >= int(retries):
                raise
            sleep_s = min(max_wait_s, max(0.0, wait_s) + random.uniform(0.0, 0.5))
            print(f"[vision-fill] {image_path.name}: network error - retry {attempt + 1}/{retries} in {sleep_s:.1f}s")
            time.sleep(sleep_s)
            wait_s = min(max_wait_s, wait_s * 2 if wait_s > 0 else 1.0)

    if raw is None:
        raise RuntimeError(f"Richiesta fill fallita dopo i retry. Ultimo errore: {last_err}")
    return raw


def _coerce_filled_tables_only(
    obj: Any,
    allowed_table_ids: set[str],
    expected_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(obj, dict) or not isinstance(obj.get("tables"), list):
        return {"tables": []}

    out_tables: list[dict[str, Any]] = []
    for t in obj.get("tables") or []:
        if not isinstance(t, dict):
            continue
        table_id = t.get("table_id")
        if not isinstance(table_id, str) or table_id not in allowed_table_ids:
            continue

        expected = expected_by_id.get(table_id) or {}
        expected_type = str(expected.get("table_type") or (t.get("table_type") or "grid"))
        expected_headers = expected.get("headers")
        if not isinstance(expected_headers, list):
            expected_headers = []
        expected_headers = [str(h).strip() for h in expected_headers if str(h).strip()]

        table_type = expected_type
        headers = expected_headers

        rows = t.get("rows")
        if not isinstance(rows, list):
            rows = []
        kv_rows: list[dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue

            label = r.get("label")
            value = r.get("value")
            source = r.get("source")

            if not isinstance(label, str):
                continue

            if not isinstance(value, str):
                value = "" if value is None else str(value)

            if isinstance(source, list):
                source = ",".join(str(x) for x in source if x)
            elif not isinstance(source, str):
                source = ""

            kv_rows.append(
                {
                    "label": label.strip(),
                    "value": value.strip(),
                    "source": source.strip(),
                }
            )

        grid = t.get("grid")
        grid_out: dict[str, Any] | None = None
        if isinstance(grid, dict) and str(table_type).strip().lower() == "grid":
        
            g_headers = headers

            # Prefer new format: grid.cells = [{row_index,col_index,value,source}, ...]
            g_cells = grid.get("cells")
            if isinstance(g_cells, list):
                cells_out: list[dict[str, Any]] = []
                for cell in g_cells:
                    if not isinstance(cell, dict):
                        continue
                    try:
                        row_index = int(cell.get("row_index"))
                        col_index = int(cell.get("col_index"))
                    except Exception:
                        continue
                    if row_index < 0 or col_index < 0:
                        continue
                    while len(g_headers) <= col_index:
                        g_headers.append(f"col_{len(g_headers)}")
                    value = cell.get("value")
                    source = cell.get("source")
                    if isinstance(source, list):
                        source = ",".join(str(x) for x in source if x)
                    elif not isinstance(source, str):
                        source = ""
                    value = value.strip()
                    source = source.strip()
                    if not value:
                        continue
                    cells_out.append(
                        {
                            "row_index": row_index,
                            "col_index": col_index,
                            "col_header": g_headers[col_index] if col_index < len(g_headers) else "",
                            "value": value,
                            "source": source,
                        }
                    )
                grid_out = {"headers": g_headers, "cells": cells_out}

            else:
                # Backward compatibility: grid.rows = [{header: value, ...}, ...]
                g_rows = grid.get("rows")
                if not isinstance(g_rows, list):
                    g_rows = []
                g_rows_out: list[dict[str, Any]] = []
                for gr in g_rows:
                    if not isinstance(gr, dict):
                        continue
                    coerced_row: dict[str, str] = {}
                    for col in g_headers:
                        v = gr.get(col)
                        coerced_row[col] = "" if v is None else str(v).strip()
                    if any(v.strip() for v in coerced_row.values()):
                        g_rows_out.append(coerced_row)
                grid_out = {"headers": g_headers, "rows": g_rows_out}

        out_tables.append(
            {
                "table_id": table_id,
                "table_type": str(table_type),
                "headers": headers,
                "rows": kv_rows,
                "grid": grid_out,
            }
        )

    return {"tables": out_tables}


def _fill_placeholders_for_missing_tables(
    detected_tables: list[dict[str, Any]],
    filled_tables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for t in filled_tables:
        if isinstance(t, dict) and isinstance(t.get("table_id"), str):
            by_id[t["table_id"]] = t

    out: list[dict[str, Any]] = []
    for dt in detected_tables:
        if not isinstance(dt, dict):
            continue
        table_id = dt.get("table_id")
        if not isinstance(table_id, str) or not table_id:
            continue
        if table_id in by_id:
            out.append(by_id[table_id])
            continue

        table_type = str(dt.get("table_type") or "grid")
        headers = dt.get("headers")
        if not isinstance(headers, list):
            headers = []
        headers = [str(h).strip() for h in headers if str(h).strip()]

        placeholder: dict[str, Any] = {"table_id": table_id, "table_type": table_type, "headers": headers, "rows": [], "grid": None}
        if table_type.strip().lower() == "grid":
            placeholder["grid"] = {"headers": headers, "rows": []}
        out.append(placeholder)

    return out


def export_tables_filled_json_to_excel(filled_json_path: Path, out_xlsx_path: Path) -> None:
    """
    Reads the *second* JSON output (tables_filled_output.json) and writes an Excel file where each sheet is one table.
    Sheet content mirrors the table structure:
      - kv: columns = label, value, source
      - grid: columns = headers (in the same order), rows = values
    """
    data = json.loads(filled_json_path.read_text(encoding="utf-8"))
    images = data.get("images")
    if not isinstance(images, list):
        raise RuntimeError("JSON filled: campo 'images' mancante o non valido.")

    try:
        from openpyxl import Workbook  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("Dipendenza mancante: installa 'openpyxl' per esportare Excel (pip install openpyxl).") from exc

    def _safe_sheet_name(name: str) -> str:
        # Excel sheet name: max 31 chars, cannot contain: []:*?/\
        name = re.sub(r"[\[\]\:\*\?\/\\]", "_", name)
        name = name.strip() or "Sheet"
        return name[:31]

    wb = Workbook()
    # Remove default empty sheet created by openpyxl
    if wb.worksheets:
        wb.remove(wb.worksheets[0])

    used_names: set[str] = set()

    for img in images:
        if not isinstance(img, dict):
            continue
        file_name = img.get("file")
        if not isinstance(file_name, str):
            file_name = "page"
        tables = img.get("tables") or []
        if not isinstance(tables, list):
            continue

        for t in tables:
            if not isinstance(t, dict):
                continue
            table_id = t.get("table_id")
            if not isinstance(table_id, str) or not table_id:
                continue
            table_type = str(t.get("table_type") or "grid").strip().lower()

            base_sheet = _safe_sheet_name(f"{Path(file_name).stem}_{table_id}")
            sheet_name = base_sheet
            n = 2
            while sheet_name in used_names:
                suffix = f"_{n}"
                sheet_name = _safe_sheet_name(base_sheet[: max(0, 31 - len(suffix))] + suffix)
                n += 1
            used_names.add(sheet_name)

            ws = wb.create_sheet(title=sheet_name)

            if table_type == "kv":
                ws.append(["label", "value", "source"])
                rows = t.get("rows") or []
                if isinstance(rows, list):
                    for r in rows:
                        if not isinstance(r, dict):
                            continue
                        ws.append(
                            [
                                "" if r.get("label") is None else str(r.get("label")),
                                "" if r.get("value") is None else str(r.get("value")),
                                "" if r.get("source") is None else str(r.get("source")),
                            ]
                        )
            else:
                grid = t.get("grid") or {}
                headers = None
                rows = None
                if isinstance(grid, dict):
                    headers = grid.get("headers")
                    rows = grid.get("rows")
                if not isinstance(headers, list):
                    headers = t.get("headers")
                if not isinstance(headers, list):
                    headers = []
                headers = [str(h) for h in headers]
                ws.append(headers)
                if isinstance(rows, list):
                    for gr in rows:
                        if not isinstance(gr, dict):
                            continue
                        ws.append(["" if gr.get(h) is None else str(gr.get(h)) for h in headers])

    out_xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_xlsx_path)


def _project_second_output_onto_first(
    detected_tables: list[dict[str, Any]],
    filled_tables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    detected_by_id: dict[str, dict[str, Any]] = {}
    detected_order: list[str] = []
    for dt in detected_tables:
        if not isinstance(dt, dict):
            continue
        tid = dt.get("table_id")
        if not isinstance(tid, str) or not tid:
            continue
        detected_by_id[tid] = dt
        detected_order.append(tid)

    filled_by_id: dict[str, dict[str, Any]] = {}
    for ft in filled_tables:
        if isinstance(ft, dict) and isinstance(ft.get("table_id"), str):
            filled_by_id[ft["table_id"]] = ft

    out: list[dict[str, Any]] = []
    for tid in detected_order:
        dt = detected_by_id.get(tid) or {}
        table_type = str(dt.get("table_type") or "grid")
        headers = dt.get("headers")
        if not isinstance(headers, list):
            headers = []
        headers = [str(h).strip() for h in headers if str(h).strip()]

        ft = filled_by_id.get(tid)
        if ft is None:
            # keep placeholder (filled later)
            out.append({"table_id": tid, "table_type": table_type, "headers": headers, "rows": [], "grid": None})
            continue

        projected = {
            "table_id": tid,
            "table_type": table_type,
            "headers": headers,
            "rows": ft.get("rows") if table_type.strip().lower() == "kv" else [],
            "grid": ft.get("grid") if table_type.strip().lower() == "grid" else None,
        }
        out.append(projected)

    return out

def run_vision_tables(
    *,
    image_dir: str | Path,
    data_json_path: str | Path,
    out_json_path: str | Path,
    out_json_detect_path: str | Path | None = None,
    out_json_filled_path: str | Path | None = None,
    model: str | None = None,
    max_tokens: int = 2500,
    max_tokens_fill: int = 3500,
    retries: int = 8,
    retry_wait: float = 5.0,
    retry_max_wait: float = 120.0,
    allowed_pages: set[int] | None = None,
) -> dict[str, Any]:
    """
    Versione "da pipeline": usa le immagini annotate e il data-json, esegue detect+fill
    e salva un JSON flat in out_json_path con schema:
      { "matches": [...], "stats": {"filled": X, "total": Y} }
    """
    api_key = (os.getenv("MISTRAL_API_KEY", "") or MISTRAL_API_KEY_FALLBACK).strip()
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY mancante in env.")

    base_dir = Path(image_dir)
    if not base_dir.exists():
        raise FileNotFoundError(f"Cartella immagini non trovata: {base_dir}")

    selected_dir, image_paths = _select_image_folder(base_dir)

    all_image_paths = image_paths

    if allowed_pages is not None:
        image_paths = [
            path for path in all_image_paths
            if _page_number(path) in allowed_pages
        ]
    
    skipped_pages = [
        {
            "file": path.name,
            "page": _page_number(path),
            "status": "skipped",
            "reason": "no_m1_tables",
            "tables": [],
        }
        for path in all_image_paths
        if path not in image_paths
    ]
    if not image_paths:
        raise FileNotFoundError(f"Nessuna immagine trovata in: {selected_dir}")

    data_path = Path(data_json_path)
    if not data_path.exists():
        raise FileNotFoundError(f"JSON dati non trovato: {data_path}")
    company_data = json.loads(data_path.read_text(encoding="utf-8"))

    # CELLA 2 — DOPO (PARALLELO 2 WORKER, output deterministico)
    matches: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    filled_results: list[dict[str, Any]] = []

    def _process_one(img: Path) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        # returns: (img.name, detected_row, filled_row, matches_local_without_ids)
        raw = _call_mistral(
            api_key=api_key,
            model=(model or DEFAULT_MODEL),
            max_tokens=int(max_tokens),
            image_path=img,
            retries=int(retries),
            retry_wait=float(retry_wait),
            retry_max_wait=float(retry_max_wait),
        )
        text = _extract_text_from_response(raw)
        obj, err = _extract_first_json(text or "")
        if err:
            tables: list[dict[str, Any]] = []
        else:
            tables = (_coerce_tables_only(obj).get("tables") or [])
            if not isinstance(tables, list):
                tables = []
    
        detected_row = {"file": img.name, "tables": tables}
    
        allowed_table_ids = {str(t.get("table_id")) for t in tables if isinstance(t, dict) and t.get("table_id")}
        expected_by_id = {
            str(t.get("table_id")): {"table_type": t.get("table_type"), "headers": t.get("headers")}
            for t in tables
            if isinstance(t, dict) and t.get("table_id")
        }
    
        filled_tables: list[dict[str, Any]] = []
        if allowed_table_ids:
            raw_fill = _call_mistral_fill(
                api_key=api_key,
                model=(model or DEFAULT_MODEL),
                max_tokens=int(max_tokens_fill),
                image_path=img,
                company_data=company_data,
                detected_tables=tables,
                retries=int(retries),
                retry_wait=float(retry_wait),
                retry_max_wait=float(retry_max_wait),
            )
            fill_text = _extract_text_from_response(raw_fill)
            fill_obj, fill_err = _extract_first_json(fill_text or "")
            if fill_err:
                filled = {"tables": []}
            else:
                filled = _coerce_filled_tables_only(fill_obj, allowed_table_ids, expected_by_id)
    
            filled_tables = filled.get("tables") or []
            if not isinstance(filled_tables, list):
                filled_tables = []
            filled_tables = _project_second_output_onto_first(tables, filled_tables)
            filled_tables = _fill_placeholders_for_missing_tables(tables, filled_tables)
    
        filled_row = {"file": img.name, "tables": filled_tables}
    
        matches_local: list[dict[str, Any]] = []
    
        # FLATTEN -> matches (senza field_id; lo assegniamo dopo in modo deterministico)
        for t in filled_tables:
            if not isinstance(t, dict):
                continue
            table_id = str(t.get("table_id") or "").strip()
            table_type = str(t.get("table_type") or "").strip().lower()
    
            if table_type == "kv":
                rows = t.get("rows") or []
                if not isinstance(rows, list):
                    continue
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    label = str(r.get("label") or "").strip()
                    value = str(r.get("value") or "").strip()
                    source = str(r.get("source") or "").strip()
                    matches_local.append(
                        {
                            "label": label or f"{table_id}:kv",
                            "value": value if value else None,
                            "source_path": source if source else None,
                            "confidence": 0.6 if value else 0.0,
                            "image_page": img.name,
                            "table_id": table_id,
                            "table_type": "kv",
                        }
                    )
    
            elif table_type == "grid":
                grid = t.get("grid") or {}
                if not isinstance(grid, dict):
                    continue
                headers = grid.get("headers") or []
                if not isinstance(headers, list):
                    continue
                headers = [str(h) for h in headers]
    
                cells = grid.get("cells")
                if isinstance(cells, list):
                    for cell in cells:
                        if not isinstance(cell, dict):
                            continue
                        try:
                            ridx = int(cell.get("row_index"))
                            cidx = int(cell.get("col_index"))
                        except Exception:
                            continue
                        if ridx < 0 or cidx < 0:
                            continue
                        while len(headers) <= cidx:
                            headers.append(f"col_{len(headers)}")
                        value = "" if cell.get("value") is None else str(cell.get("value")).strip()
                        if not value:
                            continue
                        source = "" if cell.get("source") is None else str(cell.get("source")).strip()
                        h = headers[cidx]
                        matches_local.append(
                            {
                                "label": f"{table_id} | {h} | row {ridx+1}",
                                "value": value,
                                "source_path": source if source else None,
                                "confidence": 0.6,
                                "image_page": img.name,
                                "table_id": table_id,
                                "table_type": "grid",
                                "row_index": ridx,
                                "col_index": cidx,
                                "col_header": h,
                            }
                        )
    
                else:
                    rows = grid.get("rows") or []
                    if not isinstance(rows, list):
                        continue
                    for ridx, gr in enumerate(rows):
                        if not isinstance(gr, dict):
                            continue
                        for cidx, h in enumerate(headers):
                            cell = gr.get(h)
                            value = "" if cell is None else str(cell).strip()
                            if not value:
                                continue
                            matches_local.append(
                                {
                                    "label": f"{table_id} | {h} | row {ridx+1}",
                                    "value": value,
                                    "source_path": None,
                                    "confidence": 0.6,
                                    "image_page": img.name,
                                    "table_id": table_id,
                                    "table_type": "grid",
                                    "row_index": ridx,
                                    "col_index": cidx,
                                    "col_header": h,
                                }
                            )
    
        return img.name, detected_row, filled_row, matches_local
    
    
    by_name: dict[str, tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = {}
    
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(_process_one, img) for img in image_paths]
        for fut in as_completed(futures):
            name, detected_row, filled_row, matches_local = fut.result()
            by_name[name] = (detected_row, filled_row, matches_local)
    
    # merge deterministico nell’ordine originale delle immagini
    seq = 0
    for img in image_paths:
        detected_row, filled_row, matches_local = by_name[img.name]
        results.append(detected_row)
        filled_results.append(filled_row)
        for m in matches_local:
            seq += 1
            m["field_id"] = f"t{seq}"
            matches.append(m)

    out_payload = {
        "matches": matches,
        "processed_pages": [
            {
                "image": path.name,
                "page": _page_number(path),
                "status": "processed",
            }
            for path in image_paths
        ],
        "skipped_pages": skipped_pages,
        "stats": {
            "filled": sum(
                1 for match in matches
                if match.get("value") is not None
            ),
            "total": len(matches),
            "pages_processed": len(image_paths),
            "pages_skipped": len(skipped_pages),
        },
    }

    out_path = Path(out_json_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- EXTRA OUTPUTS: detected + filled (compatibili col main CLI) ---
    if out_json_detect_path is not None:
        detected_payload = {
            "image_dir": str(selected_dir),
            "model": (model or DEFAULT_MODEL),
            "images": results,
            "skipped_pages": skipped_pages,
            "stats": {
                "images_processed": len(results),
                "images_skipped": len(skipped_pages),
                "tables_total": sum(
                    len(x.get("tables") or [])
                    for x in results
                ),
            },
        }
        p = Path(out_json_detect_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(detected_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if out_json_filled_path is not None:
        filled_payload = {
            "image_dir": str(selected_dir),
            "model": (model or DEFAULT_MODEL),
            "anagrafica_json": str(Path(data_json_path)),
            "images": filled_results,
            "skipped_pages": skipped_pages,
            "stats": {
                "images_processed": len(filled_results),
                "images_skipped": len(skipped_pages),
                "tables_total": sum(
                    len(x.get("tables") or [])
                    for x in filled_results
                ),
            },
        }
        p = Path(out_json_filled_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(filled_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return out_payload



def main() -> int:
    parser = argparse.ArgumentParser(description="Vision: detect tables (one request per page) via Mistral.")
    parser.add_argument(
        "--image-dir",
        default=r"C:\Users\39334\Desktop\Autocompilazione file\Millestone_3\pipeline_4\output\m1_output",
        help="Cartella base che contiene una sottocartella con le immagini (nome variabile) o le immagini direttamente.",
    )
    parser.add_argument("--image-index", type=int, default=0, help="Indice (0-based) della prima immagine (default: 0).")
    parser.add_argument(
        "--max-images",
        type=int,
        default=0,
        help="Quante immagini processare (0 = tutte dalla image-index).",
    )
    parser.add_argument(
        "--out-json",
        default=str(Path(__file__).resolve().parent / "tables_output.json"),
        help="Path output JSON (salvato su disco).",
    )
    parser.add_argument(
        "--anagrafica-json",
        default=r"C:\Users\39334\Desktop\Autocompilazione file\Millestone_2\anagrafica_NIKANTE.json",
        help="Path JSON anagrafica azienda (input per compilazione).",
    )
    parser.add_argument(
        "--out-json-filled",
        default=str(Path(__file__).resolve().parent / "tables_filled_output.json"),
        help="Path output JSON con sole tabelle valide + compilazione (salvato su disco).",
    )
    parser.add_argument(
        "--out-excel",
        default="",
        help="Se impostato, esporta il JSON filled in un file Excel (1 foglio = 1 tabella). Se vuoto, usa lo stesso path del JSON filled con estensione .xlsx.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Modello Mistral vision (es. mistral-medium-2508).")
    parser.add_argument("--max-tokens", type=int, default=2500, help="Max token risposta.")
    parser.add_argument("--max-tokens-fill", type=int, default=3500, help="Max token risposta per compilazione.")
    parser.add_argument("--retries", type=int, default=8, help="Numero retry su HTTP 429/5xx.")
    parser.add_argument("--retry-wait", type=float, default=5.0, help="Attesa iniziale (secondi) prima del retry.")
    parser.add_argument("--retry-max-wait", type=float, default=120.0, help="Attesa massima (secondi) tra i retry.")
    args = parser.parse_args()

    api_key = (os.getenv("MISTRAL_API_KEY", "") or MISTRAL_API_KEY_FALLBACK).strip()
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY mancante in env.")

    base_dir = Path(args.image_dir)
    if not base_dir.exists():
        parent = base_dir.parent
        if parent.exists() and parent.is_dir():
            print(f"[vision] image-dir not found; falling back to parent: {parent}")
            base_dir = parent
        else:
            raise FileNotFoundError(f"Cartella immagini non trovata: {base_dir}")

    selected_dir, image_paths = _select_image_folder(base_dir)
    if selected_dir != base_dir:
        print(f"[vision] image-dir contains subfolders; auto-selected: {selected_dir}")

    start_idx = max(0, int(args.image_index))
    if start_idx >= len(image_paths):
        raise FileNotFoundError(f"image-index={start_idx} fuori range (totale immagini: {len(image_paths)}) in: {selected_dir}")

    max_images = int(args.max_images)
    if max_images > 0:
        selected_images = image_paths[start_idx : start_idx + max_images]
    else:
        selected_images = image_paths[start_idx:]

    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[vision] model={args.model}")
    print(f"[vision] image_dir={selected_dir}")
    print(f"[vision] images_total={len(image_paths)}")
    print(f"[vision] images_selected={len(selected_images)}")
    print(f"[vision] out_json={out_path}")

    company_path = Path(args.anagrafica_json)
    if not company_path.exists():
        raise FileNotFoundError(f"Anagrafica JSON non trovata: {company_path}")
    company_data = json.loads(company_path.read_text(encoding="utf-8"))
    if not isinstance(company_data, dict):
        raise RuntimeError("Anagrafica JSON: formato non valido (atteso oggetto JSON).")

    results: list[dict[str, Any]] = []
    filled_results: list[dict[str, Any]] = []
    for idx, img in enumerate(selected_images, start=1):
        print(f"[vision] ({idx}/{len(selected_images)}) processing={img.name}")
        raw = _call_mistral(
            api_key=api_key,
            model=args.model,
            max_tokens=int(args.max_tokens),
            image_path=img,
            retries=int(args.retries),
            retry_wait=float(args.retry_wait),
            retry_max_wait=float(args.retry_max_wait),
        )
        text = _extract_text_from_response(raw)
        obj, err = _extract_first_json(text or "")
        if err:
            print(f"[vision] {img.name}: parse error: {err}")
            coerced = {"tables": []}
        else:
            coerced = _coerce_tables_only(obj)

        tables = coerced.get("tables") or []
        print(f"[vision] {img.name}: tables={len(tables)}")

        results.append({"file": img.name, "tables": tables})

        allowed_table_ids = {str(t.get("table_id")) for t in tables if isinstance(t, dict) and t.get("table_id")}
        expected_by_id = {
            str(t.get("table_id")): {"table_type": t.get("table_type"), "headers": t.get("headers")}
            for t in tables
            if isinstance(t, dict) and t.get("table_id")
        }
        if not allowed_table_ids:
            filled_results.append({"file": img.name, "tables": []})
            continue

        print(f"[vision-fill] {img.name}: tables={len(allowed_table_ids)}")
        raw_fill = _call_mistral_fill(
            api_key=api_key,
            model=args.model,
            max_tokens=int(args.max_tokens_fill),
            image_path=img,
            company_data=company_data,
            detected_tables=tables,
            retries=int(args.retries),
            retry_wait=float(args.retry_wait),
            retry_max_wait=float(args.retry_max_wait),
        )
        fill_text = _extract_text_from_response(raw_fill)
        fill_obj, fill_err = _extract_first_json(fill_text or "")
        if fill_err:
            print(f"[vision-fill] {img.name}: parse error: {fill_err}")
            filled = {"tables": []}
        else:
            filled = _coerce_filled_tables_only(fill_obj, allowed_table_ids, expected_by_id)

        filled_tables = filled.get("tables") or []
        if not isinstance(filled_tables, list):
            filled_tables = []
        # Ensure the second JSON contains ONLY tables from the first JSON, in the same order.
        filled_tables = _project_second_output_onto_first(tables, filled_tables)
        filled_tables = _fill_placeholders_for_missing_tables(tables, filled_tables)
        filled_results.append({"file": img.name, "tables": filled_tables})

    payload = {
        "image_dir": str(selected_dir),
        "model": args.model,
        "images": results,
        "stats": {"images_processed": len(results), "tables_total": sum(len(x.get("tables") or []) for x in results)},
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[vision] done images_processed={payload['stats']['images_processed']} tables_total={payload['stats']['tables_total']}")

    out_filled_path = Path(args.out_json_filled)
    out_filled_path.parent.mkdir(parents=True, exist_ok=True)
    payload_filled = {
        "image_dir": str(selected_dir),
        "model": args.model,
        "anagrafica_json": str(company_path),
        "images": filled_results,
        "stats": {
            "images_processed": len(filled_results),
            "tables_total": sum(len(x.get("tables") or []) for x in filled_results),
        },
    }
    out_filled_path.write_text(json.dumps(payload_filled, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[vision-fill] done images_processed={payload_filled['stats']['images_processed']} "
        f"tables_total={payload_filled['stats']['tables_total']} out_json={out_filled_path}"
    )

    out_excel_arg = args.out_excel if isinstance(args.out_excel, str) else ""
    out_excel_path = Path(out_excel_arg) if out_excel_arg.strip() else out_filled_path.with_suffix(".xlsx")
    export_tables_filled_json_to_excel(out_filled_path, out_excel_path)
    print(f"[vision-excel] exported xlsx={out_excel_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
