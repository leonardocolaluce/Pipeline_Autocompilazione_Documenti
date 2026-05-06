import json
import re
from pathlib import Path
from typing import Any, Dict, List, Set
from urllib import request

from .step00_config import FIELD_MAPPING_FILENAME, MISTRAL_API_KEY, MISTRAL_MODEL, MISTRAL_TIMEOUT_SEC
from .step02_openai_json_utils import parse_llm_json_payload


MAPPER_BATCH_SIZE = 12
MAPPER_MAX_RETRIES = 3

MAPPER_SYSTEM_PROMPT = """Sei il motore di mapping principale per la compilazione di modulistica di gara.

Compito specifico:
- ricevi una lista di campi vuoti, checkbox o celle tabellari vuote;
- ricevi un JSON dati aziendali/anagrafici;
- devi associare ad ogni campo il valore corretto ricavabile dal JSON dati.

Regole generali:
- la fonte dati ammessa è solo il JSON dati ricevuto;
- usa sempre `context`, `context_line`, `context_above`, `page`, `bbox`, `placeholder` e l'ordine degli item per capire quale placeholder stai riempiendo;
- quando più placeholder sono nella stessa riga, interpreta la frase completa e assegna i valori in sequenza logica;
- non compilare un campo solo perché il valore esiste nel JSON: deve essere semanticamente compatibile con quel campo;
- se un campo è ambiguo o il dato non è presente nel JSON, rispondi "N/D";
- restituisci solo JSON valido.

Regole per campi anagrafici:
- per "Il/La sottoscritto/a" usa il soggetto in carica con ruolo di legale rappresentante/amministratore, se presente nel JSON;
- per il nome del dichiarante puoi combinare nome e cognome dello stesso soggetto solo se entrambi sono presenti nello stesso oggetto JSON;
- per "nato/a" usa il luogo di nascita del medesimo soggetto;
- per la provincia tra parentesi dopo il luogo di nascita usa la sigla provincia se presente nel luogo o nei dati disponibili; se non è disponibile, usa "N/D";
- per "il" dopo "nato/a" usa la data di nascita del medesimo soggetto;
- per "codice fiscale" del dichiarante usa il codice fiscale del medesimo soggetto, non quello dell'azienda.

Regole per indirizzi:
- se il modulo richiede sede legale dell'impresa, usa i dati della sede legale/operativa dell'azienda;
- se il modulo richiede residenza della persona fisica, usa dati di residenza personale solo se presenti nel JSON; non usare automaticamente la sede aziendale come residenza personale;
- puoi separare indirizzo e numero civico solo se il JSON contiene chiaramente un indirizzo unico con numero finale, ad esempio "Via Giuseppe Altobello, 12/A";
- se devi separare via e numero, mantieni il testo senza inventare componenti.

Regole per azienda:
- per "dell'Impresa/Società", "Denominazione/Ragione Sociale" o "Operatore economico" usa la ragione sociale dell'azienda;
- per codice fiscale/P.IVA dell'impresa usa i dati fiscali aziendali;
- per forma giuridica o tipo società usa la forma giuridica dell'azienda;
- per sede legale usa comune, CAP, provincia e indirizzo della sede legale/operativa, secondo il campo richiesto.

Regole per checkbox:
- seleziona una checkbox solo se il JSON contiene un dato che la giustifica chiaramente;
- se il soggetto ha carica "AMMINISTRATORE UNICO E LEGALE RAPPRESENTANTE", la checkbox "Titolare o Legale rappresentante" è semanticamente supportata;
- se l'azienda ha forma giuridica "Società a responsabilità limitata (s.r.l.)", la checkbox/tipo "SOCIETA'" è semanticamente supportata;
- per checkbox non supportate chiaramente dal JSON, rispondi "N/D".

Regole per tabelle:
- compila solo righe e celle che corrispondono chiaramente ai dati disponibili;
- se una tabella richiede consorziati, mandanti, RTI, GEIE o operatori multipli e nel JSON c'è una sola azienda senza dati di raggruppamento, non inventare altri operatori;
- non usare nomi di persone come "BIAGIO", "LOREDANA" o simili per campi aziendali come sede legale, prestazioni, percentuali o denominazione sociale;
- non usare il settore aziendale come percentuale o quota;
- per prestazioni/categorie lavori usa il settore/codice attività solo se il campo richiede chiaramente una categoria/settore di lavorazione, altrimenti "N/D".

Regole sui valori:
- preferisci valori letterali presenti nel JSON;
- sono ammesse solo trasformazioni sicure e tracciabili dai dati JSON, come unire nome+cognome dello stesso soggetto o separare via/numero da un unico indirizzo;
- non inventare valori mancanti;
- non usare valori appartenenti a un soggetto diverso o a un contesto diverso;
- se costruisci o separi un valore, spiega brevemente in `reason` quali campi JSON hai usato;
- se non sei sicuro, rispondi "N/D".

Schema obbligatorio:
[
  {
    "item_id": "field:0",
    "answer": "N/D",
    "confidence": 0.0,
    "reason": "unsupported_by_data_json"
  }
]
"""



def _require_llm() -> None:
    if not MISTRAL_API_KEY:
        raise RuntimeError("LLM mapping non disponibile: MISTRAL_API_KEY mancante.")


def _chunk_items(items: List[Dict[str, Any]], size: int = MAPPER_BATCH_SIZE) -> List[List[Dict[str, Any]]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def _flatten_exact_values(node: Any) -> Set[str]:
    values: Set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
        elif obj is not None:
            text = str(obj).strip()
            if text:
                values.add(text)

    walk(node)
    values.add("N/D")
    return values


def _flatten_exact_entries(node: Any, prefix: str = "") -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []

    def walk(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                next_path = f"{path}.{key}" if path else str(key)
                walk(value, next_path)
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                next_path = f"{path}[{index}]" if path else f"[{index}]"
                walk(value, next_path)
        elif obj is not None:
            text = str(obj).strip()
            if text:
                entries.append({"path": path, "value": text})

    walk(node, prefix)
    return entries


def _normalize_answer_value(answer: Any) -> str:
    if answer is None:
        return ""
    if isinstance(answer, str):
        return answer.strip()
    if isinstance(answer, bool):
        return "N/D"
    if isinstance(answer, (int, float)):
        return str(answer).strip()
    if isinstance(answer, dict):
        candidate = answer.get("value")
        if isinstance(candidate, str):
            return candidate.strip()
        checked = answer.get("checked")
        if isinstance(checked, bool):
            return "N/D"
        return ""
    return str(answer).strip()


def _llm_map_chunk(items: List[Dict[str, Any]], xml_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not items:
        return []

    _require_llm()
    body = {
        "model": MISTRAL_MODEL,
        "temperature": 0,
        "top_p": 1,
        "messages": [
            {"role": "system", "content": MAPPER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "data_json": xml_data,
                        "items": items,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }

    req = request.Request(
        "https://api.mistral.ai/v1/chat/completions",
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    last_exc: Exception | None = None
    for attempt in range(1, MAPPER_MAX_RETRIES + 1):
        try:
            with request.urlopen(req, timeout=MISTRAL_TIMEOUT_SEC) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            parsed = parse_llm_json_payload(raw)

            if not isinstance(parsed, list):
                raise RuntimeError("Risposta LLM non è una lista JSON.")

            parsed_by_id: Dict[str, Dict[str, Any]] = {}
            for item in parsed:
                if isinstance(item, dict) and "item_id" in item:
                    parsed_by_id[str(item["item_id"])] = item

            out: List[Dict[str, Any]] = []
            for item in items:
                llm_item = parsed_by_id.get(str(item["item_id"]))
                if not llm_item:
                    raise RuntimeError(f"LLM mapping risposta incompleta, item mancante: {item['item_id']}")
                out.append(
                    {
                        "item_id": str(item["item_id"]),
                        "answer": _normalize_answer_value(llm_item.get("answer")),
                        "confidence": max(0.0, min(1.0, float(llm_item.get("confidence", 0.0) or 0.0))),
                        "reason": str(llm_item.get("reason", "")).strip() or "llm",
                        "llm_enabled": True,
                    }
                )
            return out
        except Exception as exc:
            last_exc = exc
            if attempt < MAPPER_MAX_RETRIES:
                print(f"[LLM][mapping] retry batch - attempt={attempt + 1}/{MAPPER_MAX_RETRIES} - reason={exc}")
    raise RuntimeError(f"LLM mapping fallito: {last_exc}") from last_exc


def _items_from_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "item_id": row.get("item_id"),
            "item_type": row.get("item_type"),
            "label": row.get("label"),
            "context": row.get("context"),
            "context_line": row.get("context_line"),
            "context_above": row.get("context_above"),
            "placeholder": row.get("placeholder"),
            "page": row.get("page"),
            "bbox": row.get("bbox"),
            "table_index": row.get("table_index"),
            "row_index": row.get("row_index"),
            "col_index": row.get("col_index"),
            "table_headers": row.get("table_headers"),
            "row_cells": row.get("row_cells"),
            "row_labels": row.get("row_labels"),
            "marker_bbox": row.get("marker_bbox"),
            "checkbox_lines": row.get("checkbox_lines"),
            "checkbox_bbox": row.get("checkbox_bbox"),
        }
        for row in rows
    ]


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _score_entry_for_row(row: Dict[str, Any], entry: Dict[str, str]) -> int:
    path = _normalize_text(entry["path"])
    value = _normalize_text(entry["value"])
    label_text = _normalize_text(str(row.get("label", "")))
    row_text = " ".join(
        _normalize_text(str(row.get(key, "")))
        for key in ("label", "context", "context_line", "context_above")
    )
    score = 0

    def add_if(condition: bool, points: int) -> None:
        nonlocal score
        if condition:
            score += points

    add_if("registro delle imprese" in label_text and "camera_commercio" in path, 22)
    add_if("registro delle imprese" in label_text and "registro_imprese" in path, 10)
    add_if("partita iva" in label_text and "partita_iva" in path, 16)
    add_if(("codice fiscale" in label_text or "c.f." in label_text or "cf" in label_text) and ("codice_fiscale" in path or path.endswith(".cf")), 16)
    add_if("registro delle imprese" in row_text and ("registro_imprese" in path or "camera_commercio" in path), 8)
    add_if("partita iva" in row_text and "partita_iva" in path, 8)
    add_if(("codice fiscale" in row_text or "c.f." in row_text or "cf" in row_text) and ("codice_fiscale" in path or path.endswith(".cf")), 8)
    add_if(("prefettura" in row_text or "white list" in row_text) and "prefett" in value, 9)
    add_if(("prefettura" in row_text or "white list" in row_text) and "prefett" in path, 7)
    add_if(("ccnl" in row_text or "codice alfanumerico unico" in row_text) and "ccnl" in path, 11)
    add_if(("domicilio digitale" in row_text or "servizio elettronico" in row_text or "recapito certificato" in row_text) and ("pec" in path or "email" in path or "domicilio_digitale" in path), 11)
    add_if(("forma di partecipazione" in row_text or "tipologia societaria" in row_text or "forma giuridica" in row_text) and ("forma_giuridica" in path or "tipologia" in path), 10)
    add_if(("operatore esecutore" in row_text or "ragione sociale" in row_text or "denominazione" in row_text) and ("ragione_sociale" in path or "denominazione" in path or "azienda.nome" in path or path.endswith(".nome")), 10)
    add_if("sede" in row_text and ("indirizzo" in path or "sede" in path), 8)
    add_if("al n" in row_text and ("numero" in path or "n_rea" in path), 5)

    if entry["value"] and _normalize_text(entry["value"]) in row_text:
        score += 3
    return score


def _heuristic_exact_match(row: Dict[str, Any], exact_entries: List[Dict[str, str]]) -> tuple[str, str] | None:
    candidates: List[tuple[int, Dict[str, str]]] = []
    for entry in exact_entries:
        score = _score_entry_for_row(row, entry)
        if score > 0:
            candidates.append((score, entry))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], len(item[1]["value"])))
    best_score, best_entry = candidates[0]
    if best_score < 8:
        return None
    return best_entry["value"], f"exact_match_from_data_json: '{best_entry['path']}' → '{best_entry['value']}'"


def _dedupe_single_company_table_rows(rows: List[Dict[str, Any]]) -> None:
    grouped: Dict[int, Dict[int, List[Dict[str, Any]]]] = {}
    for row in rows:
        if str(row.get("item_type", "")).strip() != "table_cell":
            continue
        if str(row.get("answer", "")).strip() in {"", "N/D"}:
            continue
        table_index = row.get("table_index")
        row_index = row.get("row_index")
        if table_index in (None, "") or row_index in (None, ""):
            continue
        grouped.setdefault(int(table_index), {}).setdefault(int(row_index), []).append(row)

    for _table_index, rows_by_index in grouped.items():
        signatures: Dict[tuple[tuple[int, str], ...], int] = {}
        for row_index in sorted(rows_by_index):
            cells = rows_by_index[row_index]
            signature = tuple(
                sorted(
                    (int(cell.get("col_index", 0) or 0), str(cell.get("answer", "")).strip())
                    for cell in cells
                    if str(cell.get("answer", "")).strip() not in {"", "N/D"}
                )
            )
            if not signature:
                continue
            if signature in signatures:
                for cell in cells:
                    cell["answer"] = "N/D"
                    cell["confidence"] = 0.0
                    cell["reason"] = "dedup_single_company_row"
            else:
                signatures[signature] = row_index


def fill_mapping_file(
    output_dir: str | Path,
    xml_json_path: str | Path,
) -> Dict[str, Any]:
    xml_path = Path(xml_json_path).resolve()
    if not xml_path.exists():
        raise FileNotFoundError(f"JSON dati non trovato: {xml_path}")

    out_dir = Path(output_dir)
    mapping_path = out_dir / FIELD_MAPPING_FILENAME
    if not mapping_path.exists():
        raise FileNotFoundError(f"Mapping JSON non trovato: {mapping_path}")

    with open(mapping_path, "r", encoding="utf-8") as handle:
        mapping_payload = json.load(handle)
    rows = mapping_payload.get("rows") or []

    with open(xml_path, "r", encoding="utf-8") as handle:
        xml_data = json.load(handle)

    allowed_values = _flatten_exact_values(xml_data)
    exact_entries = _flatten_exact_entries(xml_data)
    rows_by_id = {str(row["item_id"]): row for row in rows}
    items = _items_from_rows(rows)
    chunks = _chunk_items(items)

    for index, chunk in enumerate(chunks, start=1):
        print(f"[LLM][mapping] batch {index}/{len(chunks)} - items={len(chunk)}")
        try:
            mapped_chunk = _llm_map_chunk(chunk, xml_data)
        except Exception as exc:
            print(f"[LLM][mapping] fallback batch {index}/{len(chunks)} - reason={exc}")
            mapped_chunk = [
                {
                    "item_id": str(item["item_id"]),
                    "answer": "N/D",
                    "confidence": 0.0,
                    "reason": "fallback_unavailable_llm",
                    "llm_enabled": False,
                }
                for item in chunk
            ]

        mapped_by_id = {item["item_id"]: item for item in mapped_chunk}
        for item in chunk:
            row = rows_by_id[str(item["item_id"])]
            verdict = mapped_by_id[str(item["item_id"])]
            answer = _normalize_answer_value(verdict.get("answer"))
            reason = str(verdict.get("reason", "")).strip() or "llm"
            heuristic = None
            if not answer or answer == "N/D":
                heuristic = _heuristic_exact_match(row, exact_entries)
                if heuristic:
                    answer, reason = heuristic
            if not answer or answer not in allowed_values:
                heuristic = heuristic or _heuristic_exact_match(row, exact_entries)
                if heuristic and heuristic[0] in allowed_values:
                    answer, reason = heuristic
            if not answer or answer not in allowed_values:
                answer = "N/D"
                if reason != "fallback_unavailable_llm":
                    reason = "unsupported_by_data_json_exact"
            row["answer"] = answer
            row["confidence"] = max(0.0, min(1.0, float(verdict.get("confidence", 0.0) or 0.0)))
            row["reason"] = reason
            row["llm_enabled"] = bool(verdict.get("llm_enabled", False))

        with open(mapping_path, "w", encoding="utf-8") as handle:
            json.dump(mapping_payload, handle, ensure_ascii=False, indent=2)

    _dedupe_single_company_table_rows(rows)
    with open(mapping_path, "w", encoding="utf-8") as handle:
        json.dump(mapping_payload, handle, ensure_ascii=False, indent=2)

    non_nd_count = sum(1 for row in rows if str(row.get("answer", "")).strip() not in {"", "N/D"})
    print(f"[LLM][mapping] end - mode=mistral - mapped_non_nd={non_nd_count}/{len(rows)}")
    return {
        "mapping_path": str(mapping_path),
        "item_count": len(rows),
        "non_nd_count": non_nd_count,
    }
