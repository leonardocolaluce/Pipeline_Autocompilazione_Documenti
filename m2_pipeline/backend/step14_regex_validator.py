from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


PrintFn = Callable[[str], None]


def _read_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8"))


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm_text(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _norm_key(value: Any) -> str:
    text = _norm_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def _flatten_scalar_values(node: Any) -> set[str]:
    values: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
            return
        if isinstance(obj, list):
            for v in obj:
                walk(v)
            return
        if obj is None:
            return
        s = _norm_text(obj)
        if s:
            values.add(s)

    walk(node)
    return values


def _collect_people_names(data: dict[str, Any]) -> set[str]:
    """
    Heuristic: collect full names from common keys in `soggetti_in_carica`.
    """
    people: set[str] = set()
    items = data.get("soggetti_in_carica") or []
    if not isinstance(items, list):
        return people

    for it in items:
        if not isinstance(it, dict):
            continue
        nome = _norm_text(it.get("nome"))
        cognome = _norm_text(it.get("cognome"))
        full = _norm_text(f"{nome} {cognome}")
        if full and nome and cognome:
            people.add(full.upper())
        # sometimes already present as a single field
        for k in ("nominativo", "nome_cognome", "soggetto", "rappresentante_legale"):
            v = _norm_text(it.get(k))
            if v:
                people.add(v.upper())

    return people


def _collect_company_names(data: dict[str, Any]) -> set[str]:
    company: set[str] = set()
    azienda = data.get("azienda") or {}
    if isinstance(azienda, dict):
        for k in ("ragione_sociale", "denominazione", "denom", "impresa", "nome_impresa"):
            v = _norm_text(azienda.get(k))
            if v:
                company.add(v.upper())
    return company


CF_RE = re.compile(r"^[A-Z]{6}[0-9]{2}[A-Z][0-9]{2}[A-Z][0-9]{3}[A-Z]$")
PIVA_RE = re.compile(r"^[0-9]{11}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE = re.compile(r"^(0?[1-9]|[12][0-9]|3[01])[\/.-](0?[1-9]|1[0-2])[\/.-]([0-9]{2}|[0-9]{4})$")
PROV_RE = re.compile(r"^[A-Z]{2}$")
CAP_RE = re.compile(r"^[0-9]{5}$")
IBAN_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")

# Very simple "person-like full name" heuristic (ALL CAPS or Title Case, 2-4 tokens).
PERSON_NAME_RE = re.compile(r"^[A-ZÀ-ÖØ-Ý][A-ZÀ-ÖØ-Ý'`.-]+(?:\s+[A-ZÀ-ÖØ-Ý][A-ZÀ-ÖØ-Ý'`.-]+){1,3}$")


@dataclass(frozen=True)
class RuleVerdict:
    ok: bool
    reason: str
    field_type: str


def _classify_expected_type(row: dict[str, Any]) -> str | None:
    """
    Determine expected type for a mapping row (field/table_cell) using label/context and table headers.
    Returns a short type code (e.g., CF, PIVA, RAG_SOC, SEDE_LEGALE, DATA).
    """
    label = _norm_key(row.get("label"))
    context = _norm_key(row.get("context"))

    # Prefer table header when present.
    try:
        headers = row.get("table_headers") or []
        col_index = int(row.get("col_index", -1))
        if isinstance(headers, list) and 0 <= col_index < len(headers):
            hdr = _norm_key(headers[col_index])
            if hdr:
                label = hdr
    except Exception:
        pass

    hay = f"{label} {context}".strip()
    if not hay:
        return None

    # Fiscal codes
    if any(k in hay for k in ("cod fisc", "codice fiscale", "c f", "cf")):
        return "CF"
    if any(k in hay for k in ("p iva", "partita iva", "p.iva", "iva")):
        return "PIVA"

    # Company identity
    if any(k in hay for k in ("ragione sociale", "denominazione", "denom", "impresa", "operatore economico", "dell operatore economico")):
        return "RAG_SOC"

    # Person identity
    if any(k in hay for k in ("il la sottoscritto", "sottoscritto", "legale rappresentante", "rappresentante")):
        return "PERSONA"

    # Birth / dates
    if any(k in hay for k in ("nato a", "luogo di nascita", "nata a")):
        return "LUOGO_NASC"
    if hay in {"il", "data", "data di nascita"} or "data di nascita" in hay:
        return "DATA"

    # Address-like
    if any(k in hay for k in ("sede legale", "residente a", "domiciliato", "indirizzo", "via", "viale", "piazza", "localita", "citta", "comune")):
        return "INDIRIZZO"
    if any(k in hay for k in ("cap",)):
        return "CAP"
    if any(k in hay for k in ("provincia", "prov",)):
        return "PROV"

    # Contacts / banking
    if "pec" in hay:
        return "PEC"
    if "email" in hay or "e mail" in hay:
        return "EMAIL"
    if "iban" in hay:
        return "IBAN"

    return None


def _validate_answer(expected: str | None, answer: str, *, people_names: set[str], company_names: set[str]) -> RuleVerdict:
    text = _norm_text(answer)
    if not text or text == "N/D":
        return RuleVerdict(ok=True, reason="empty", field_type=expected or "UNKNOWN")

    upper = text.upper()
    compact_upper = re.sub(r"\s+", "", upper)

    if expected == "CF":
        if CF_RE.match(compact_upper):
            return RuleVerdict(True, "ok", "CF")
        if PERSON_NAME_RE.match(upper):
            return RuleVerdict(False, "cf_is_person_name", "CF")
        if PIVA_RE.match(re.sub(r"\D+", "", text)):
            return RuleVerdict(False, "cf_is_piva", "CF")
        return RuleVerdict(False, "cf_bad_format", "CF")

    if expected == "PIVA":
        digits = re.sub(r"\D+", "", text)
        if PIVA_RE.match(digits):
            return RuleVerdict(True, "ok", "PIVA")
        if CF_RE.match(compact_upper):
            return RuleVerdict(False, "piva_is_cf", "PIVA")
        return RuleVerdict(False, "piva_bad_format", "PIVA")

    if expected == "RAG_SOC":
        if upper in people_names:
            return RuleVerdict(False, "rag_soc_is_known_person", "RAG_SOC")
        if PERSON_NAME_RE.match(upper) and upper not in company_names:
            return RuleVerdict(False, "rag_soc_looks_like_person", "RAG_SOC")
        # Accept company-like suffixes, but not required.
        return RuleVerdict(True, "ok", "RAG_SOC")

    if expected == "PERSONA":
        if CF_RE.match(compact_upper) or PIVA_RE.match(re.sub(r"\D+", "", text)):
            return RuleVerdict(False, "persona_is_code", "PERSONA")
        if DATE_RE.match(text):
            return RuleVerdict(False, "persona_is_date", "PERSONA")
        return RuleVerdict(True, "ok", "PERSONA")

    if expected == "LUOGO_NASC":
        if DATE_RE.match(text):
            return RuleVerdict(False, "luogo_is_date", "LUOGO_NASC")
        if CF_RE.match(compact_upper):
            return RuleVerdict(False, "luogo_is_cf", "LUOGO_NASC")
        return RuleVerdict(True, "ok", "LUOGO_NASC")

    if expected == "DATA":
        if DATE_RE.match(text):
            return RuleVerdict(True, "ok", "DATA")
        if PERSON_NAME_RE.match(upper):
            return RuleVerdict(False, "data_is_person", "DATA")
        return RuleVerdict(False, "data_bad_format", "DATA")

    if expected == "EMAIL" or expected == "PEC":
        if EMAIL_RE.match(text):
            return RuleVerdict(True, "ok", expected)
        return RuleVerdict(False, "email_bad_format", expected)

    if expected == "CAP":
        digits = re.sub(r"\D+", "", text)
        if CAP_RE.match(digits):
            return RuleVerdict(True, "ok", "CAP")
        return RuleVerdict(False, "cap_bad_format", "CAP")

    if expected == "PROV":
        if PROV_RE.match(upper):
            return RuleVerdict(True, "ok", "PROV")
        return RuleVerdict(False, "prov_bad_format", "PROV")

    if expected == "IBAN":
        iban = re.sub(r"\s+", "", upper)
        if IBAN_RE.match(iban):
            return RuleVerdict(True, "ok", "IBAN")
        return RuleVerdict(False, "iban_bad_format", "IBAN")

    # Default: don't remove.
    return RuleVerdict(True, "ok_default", expected or "UNKNOWN")


def clean_mapping_with_regex_rules(
    mapping_json_path: str | Path,
    data_json_path: str | Path | None = None,
    *,
    print_fn: PrintFn | None = print,
) -> dict[str, Any]:
    """
    In-place cleaner for `campo_valore*.json`.
    - Uses rigid regex heuristics to drop clearly-wrong answers.
    - Prints removed fields and a concise summary.
    """
    mapping_json_path = Path(mapping_json_path)
    payload = _read_json(mapping_json_path)
    rows = list(payload.get("rows") or [])

    data = _read_json(data_json_path) if data_json_path else {}
    people_names = _collect_people_names(data)
    company_names = _collect_company_names(data)
    allowed_values = _flatten_scalar_values(data)

    removed = 0
    removed_by_type: dict[str, int] = {}
    removed_items: list[dict[str, Any]] = []

    for row in rows:
        item_type = str(row.get("item_type", "")).strip()
        if item_type not in {"field", "table_cell"}:
            continue
        answer = _norm_text(row.get("answer"))
        if not answer or answer == "N/D":
            continue

        expected = _classify_expected_type(row)
        verdict = _validate_answer(expected, answer, people_names=people_names, company_names=company_names)

        # Optional hard check: if we know the expected type and the value is not among known scalars, be stricter.
        # (Only for identifiers; keeps the cleaner conservative.)
        if verdict.ok and expected in {"CF", "PIVA", "EMAIL", "PEC"} and allowed_values:
            if answer not in allowed_values and answer.upper() not in allowed_values:
                verdict = RuleVerdict(False, "not_in_input_json", expected)

        if verdict.ok:
            continue

        prev_reason = _norm_text(row.get("reason"))
        row["answer"] = "N/D"
        row["confidence"] = 0.0
        row["validator_status"] = "removed_regex"
        row["reason"] = (prev_reason + "|" if prev_reason else "") + f"regex_validator:{verdict.reason}"

        removed += 1
        removed_by_type[verdict.field_type] = removed_by_type.get(verdict.field_type, 0) + 1
        removed_items.append(
            {
                "item_id": row.get("item_id"),
                "field_type": verdict.field_type,
                "label": row.get("label"),
                "answer": answer,
                "reason": verdict.reason,
            }
        )

        if print_fn is not None:
            print_fn(
                f"[regex_validator] REMOVE item_id={row.get('item_id')} type={verdict.field_type} "
                f"label={_norm_text(row.get('label'))!r} answer={answer!r} reason={verdict.reason}"
            )

    payload["rows"] = rows
    _write_json(mapping_json_path, payload)

    summary = {
        "status": "ok",
        "mapping_path": str(mapping_json_path),
        "removed_count": removed,
        "removed_by_type": dict(sorted(removed_by_type.items(), key=lambda kv: (-kv[1], kv[0]))),
        "removed_items_sample": removed_items[:30],
    }

    if print_fn is not None:
        parts = [f"{k}={v}" for k, v in summary["removed_by_type"].items()]
        print_fn(f"[regex_validator] SUMMARY removed={removed}" + (f" ({', '.join(parts)})" if parts else ""))

    return summary

