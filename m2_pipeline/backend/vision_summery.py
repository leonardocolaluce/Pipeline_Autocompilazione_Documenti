import argparse
import base64
import json
import os
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError
import time
import argparse
import base64
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib import request
from concurrent.futures import ThreadPoolExecutor, as_completed
from .step02_openai_json_utils import parse_llm_json_payload


MISTRAL_API_URL = os.getenv("MISTRAL_API_URL", "https://api.mistral.ai/v1/chat/completions").strip()
DEFAULT_MODEL = os.getenv("MISTRAL_MODEL", "mistral-medium-2508").strip()

# key: INCOLLA_QUI_LA_TUA_MISTRAL_API_KEY
MISTRAL_API_KEY_FALLBACK = "i58x3rBFunIs5n7OOYyDsRoSPFigCRy0"


def _extract_text_from_response(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices, list):
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


def _extract_match_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    marker = "=== MATCH ==="
    if marker not in text:
        return None, "Sezione === MATCH === non trovata nella risposta."
    tail = text.split(marker, 1)[1]
    start = tail.find("{")
    if start < 0:
        return None, "JSON non trovato dopo === MATCH ===."
    # Extract a single top-level JSON object by balancing braces, ignoring braces inside strings.
    chunk = tail[start:]
    depth = 0
    in_string = False
    escaped = False
    end_idx = None
    for idx, ch in enumerate(chunk):
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
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = idx + 1
                break
    if end_idx is None:
        return None, "JSON tronco in sezione MATCH (brace balancing incompleto)."
    candidate = chunk[:end_idx].strip()
    try:
        return json.loads(candidate), None
    except Exception as exc:
        return None, f"JSON non valido in sezione MATCH: {exc}"


def _extract_sections_to_print(text: str) -> str:
    # Print only CAMPI and DATI sections (everything before MATCH marker).
    marker = "=== MATCH ==="
    if marker in text:
        return text.split(marker, 1)[0].rstrip()
    return text.rstrip()


def _flatten_scalar_values(node: Any) -> set[str]:
    values: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        elif obj is None:
            return
        else:
            text = str(obj).strip()
            if text:
                values.add(text)

    walk(node)
    return values


def _get_by_path(data: Any, path: str) -> Any:
    if not path:
        return None
    cur: Any = data
    token_re = re.compile(r"(?P<name>[A-Za-z0-9_]+)|\[(?P<index>\d+)\]")
    for match in token_re.finditer(path):
        name = match.groupdict().get("name")
        index = match.groupdict().get("index")
        if name is None and index is None:
            continue

        if name is not None:
            if not isinstance(cur, dict) or name not in cur:
                return None
            cur = cur[name]
        elif index is not None:
            if not isinstance(cur, list):
                return None
            idx = int(index)
            if idx < 0 or idx >= len(cur):
                return None
            cur = cur[idx]
    return cur


def _allow_fullname_combination(data_json: Any, source_path: str) -> str | None:
    """
    Allow one safe transformation: combine {cognome, nome} or {nome, cognome}
    when source_path points to an object containing both keys.
    """
    node = _get_by_path(data_json, source_path)
    if not isinstance(node, dict):
        return None

    # chiavi variabili + case-insensitive (Nome/NOME/Cognome/COGNOME ecc.)
    lowered = {str(k).strip().lower(): k for k in node.keys()}

    nome_keys = ("nome", "name", "first_name", "firstname", "given_name")
    cognome_keys = ("cognome", "surname", "last_name", "lastname", "family_name")

    nome_key = next((lowered.get(k) for k in nome_keys if lowered.get(k) is not None), None)
    cognome_key = next((lowered.get(k) for k in cognome_keys if lowered.get(k) is not None), None)

    nome = node.get(nome_key) if nome_key is not None else None
    cognome = node.get(cognome_key) if cognome_key is not None else None


    if not isinstance(nome, str) or not isinstance(cognome, str):
        return None

    nome = nome.strip()
    cognome = cognome.strip()
    if not nome or not cognome:
        return None
    # Prefer "COGNOME NOME" (common in your dataset), fallback to "NOME COGNOME".
    combo1 = f"{cognome} {nome}".strip()
    combo2 = f"{nome} {cognome}".strip()
    return combo1 or combo2

def _force_sottoscritto_from_person_paths(payload: dict[str, Any], data_json: Any) -> dict[str, Any]:
    matches = payload.get("matches") or []
    if not isinstance(matches, list):
        return payload

    # 1) trova un “person root path” usato per data/luogo nascita (stessa persona)
    person_root = None
    for m in matches:
        if not isinstance(m, dict):
            continue
        sp = m.get("source_path")
        if not isinstance(sp, str) or not sp:
            continue
        # esempi: soggetti_in_carica[0].data_nascita -> root: soggetti_in_carica[0]
        for suffix in (".data_nascita", ".luogo_nascita", ".dataDiNascita", ".luogoDiNascita"):
            if sp.endswith(suffix):
                person_root = sp[: -len(suffix)]
                break
        if not person_root and sp.startswith("soggetti_in_carica[") and "]" in sp:
            person_root = sp.split("]", 1)[0] + "]"

        if person_root:
            break

    if not person_root:
        return payload

    forced_value = _allow_fullname_combination(data_json, person_root)
    if not forced_value:
        return payload

    # 2) forza tutti i campi “Il sottoscritto …” che sono rimasti null
    for m in matches:
        if not isinstance(m, dict):
            continue
        label = str(m.get("label") or "").lower()
        if "sottoscritt" in label or "dichiarante" in label:
            if m.get("value") is None:
                m["value"] = forced_value
                m["source_path"] = person_root
                m["confidence"] = float(m.get("confidence") or 0.85)
    return payload


def _coerce_match_output(matches_obj: dict[str, Any], data_json: Any) -> dict[str, Any]:
    allowed = _flatten_scalar_values(data_json)
    matches = matches_obj.get("matches") or []
    if not isinstance(matches, list):
        matches = []

    cleaned: list[dict[str, Any]] = []
    for item in matches:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        source_path = item.get("source_path")

        keep = False
        if value is None:
            keep = False
        elif isinstance(value, bool):
            # Keep booleans only if they are supported by a JSON path reference.
            keep = bool(source_path)
        else:
            text_value = str(value).strip()
            keep = text_value in allowed
            if not keep and isinstance(source_path, str):
                source_value = _get_by_path(data_json, source_path)
                keep = source_value is not None
            if not keep and isinstance(source_path, str):
                # Allow fullname combo if source_path points to an object with {nome,cognome}.
                combo = _allow_fullname_combination(data_json, source_path)
                if combo and text_value == combo:
                    keep = True

        cleaned.append(
            {
                "field_id": item.get("field_id"),
                "label": item.get("label"),
                "field_type": item.get("field_type"),
                "value": value if keep else None,
                "source_path": source_path if keep else None,
                "confidence": item.get("confidence", 0.0),
            }
        )

    total = len(cleaned)
    filled = sum(1 for item in cleaned if item.get("value") is not None)
    return {
        "matches": cleaned,
        "stats": {"filled": filled, "total": total},
    }

def _encode_image_data_uri(image_path: Path) -> str:
    suffix = image_path.suffix.lower().lstrip(".")
    if suffix not in {"png", "jpg", "jpeg", "webp"}:
        suffix = "png"
    mime = "image/jpeg" if suffix in {"jpg", "jpeg"} else f"image/{suffix}"
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def run_vision_mapping(
    *,
    image_dir: str | Path,
    data_json_path: str | Path,
    out_json_path: str | Path,
    model: str | None = None,
    max_tokens: int = 5000,
) -> dict[str, Any]:
    api_key = (os.getenv("MISTRAL_API_KEY", "") or MISTRAL_API_KEY_FALLBACK).strip()
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY mancante in env.")

    image_dir = Path(image_dir)
    if not image_dir.exists():
        raise FileNotFoundError(f"Cartella immagini non trovata: {image_dir}")
    image_paths = sorted(
        [p for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}],
        key=lambda p: p.name,
    )
    if not image_paths:
        raise FileNotFoundError(f"Nessuna immagine trovata in: {image_dir}")

    data_json_path = Path(data_json_path)
    if not data_json_path.exists():
        raise FileNotFoundError(f"JSON dati non trovato: {data_json_path}")
    data_json = json.loads(data_json_path.read_text(encoding="utf-8"))

    all_matches: list[dict[str, Any]] = []
    page_errors: list[dict[str, Any]] = []

    def _process_one(image_path: Path) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
        try:
            matches_for_page: list[dict[str, Any]] = []

            # --- INCOLLA QUI (IDENTICO) IL CODICE CHE AVEVI NEL VECCHIO LOOP
            print(f"[LLM][vision-mapping] start image={image_path.name}", flush=True)
            image_parts = [{"type": "image_url", "image_url": _encode_image_data_uri(image_path)}]
    
    
            body = {
                "model": (model or DEFAULT_MODEL),
                "temperature": 0,
                "top_p": 1,
                "max_tokens": max_tokens,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            [
                                {
                                    "type": "text",
                                    "text": (
                                        "Sei un assistente per compilazione modulistica.\n"
                                        "Input:\n"
                                        "- 1 immagine (pagina del modulo) con campi evidenziati in rosso.\n"
                                        "- 1 JSON dati azienda/anagrafica.\n\n"
                                        "Obiettivo: estrarre i campi visibili e tentare un match con i dati, senza inventare.\n\n"
                                        "REGOLE IMPORTANTI:\n"
                                        "- Usa solo informazioni presenti nel JSON dati, ma puoi comporre valori da più campi quando il modulo richiede un dato completo, es. indirizzo + comune + provincia, oppure cognome + nome.\n"
                                        "- Non inventare path: i path devono essere reali e riferiti al JSON esattamente come fornito.\n"
                                        "- Distingui campi PERSONA (es. residenza persona) da campi AZIENDA (sede legale, P.IVA, ragione sociale). Se il JSON non ha residenza persona, metti N/D.\n"
                                        "- NON compilare un campo solo perché il dato esiste nel JSON: compila PEC/email/CF/P.IVA SOLO se il testo vicino allo specifico spazio da compilare lo richiede chiaramente.\n"
                                        "- Se il documento contiene molti campi \"PEC/email\" su pagine diverse, NON assumere che vadano tutti compilati: ogni campo va deciso dal contesto locale.\n"
                                        "- In caso di dubbio PEC vs email: compila la PEC solo se il campo dice esplicitamente \"PEC\"; se dice solo \"email\" usa PEC solo se è chiaramente un contatto.\n"
                                        "REGOLE PERSONA (SELEZIONE SOGGETTO):\n"
                                        "- Nel JSON c'è spesso `soggetti_in_carica` (lista di persone).\n"
                                        "- Devi scegliere UNA `persona_principale` e usarla per TUTTI i campi PERSONA (nome, cognome, nato/a il, nato/a a, CF persona), a meno che il modulo dica esplicitamente un altro ruolo (es. 'Direttore tecnico').\n"
                                        "- Per campi tipo `Il sottoscritto`, `Il/La sottoscritto/a`, `dichiarante`, `legale rappresentante`, devi compilare con COGNOME + NOME della prima persona che compare nel json.\n"
                                        "- Se usi COGNOME + NOME da `soggetti_in_carica[0]`, restituisci `value` come stringa combinata.\n"
                                        "- Per ogni campo che contiene 'sottoscritto' / 'dichiarante' / 'legale rappresentante': se `soggetti_in_carica[0].cognome` e `soggetti_in_carica[0].nome` esistono, DEVI compilare: `source_path`=\"soggetti_in_carica[0]\" e `value`=\"<cognome> <nome>\" (un solo spazio, niente titoli tipo Sig./Dott., niente spazi doppi o finali), devi ovviamente riempire con il nome della stessa persona di cui metti gli altri dati come data nascita, titolo....\n"
                                        "- Non lasciare vuoto `Il sottoscritto` se in `soggetti_in_carica[0]` esistono `cognome` e `nome` di almeno una persona.\n"
                                        "  1) LEGALE RAPPRESENTANTE | RAPPRESENTANTE LEGALE | AMMINISTRATORE UNICO | AMMINISTRATORE DELEGATO | AD | CEO | CHIEF EXECUTIVE OFFICER | PRESIDENTE | PRESIDENT | MANAGING DIRECTOR | DIRETTORE GENERALE\n"
                                        "  2) CFO | CHIEF FINANCIAL OFFICER | DIRETTORE FINANZIARIO | RESPONSABILE FINANZIARIO\n"
                                        "  3) COO | CHIEF OPERATING OFFICER | DIRETTORE OPERATIVO\n"
                                        "  4) CTO | CHIEF TECHNOLOGY OFFICER | DIRETTORE TECNICO | TECHNICAL DIRECTOR | IT MANAGER | RESPONSABILE IT\n"
                                        "  5) CISO | CHIEF INFORMATION SECURITY OFFICER | RESPONSABILE SICUREZZA INFORMATICA\n"
                                        "  6) CHRO | CHIEF HUMAN RESOURCES OFFICER | DIRETTORE RISORSE UMANE | HR MANAGER\n"
                                        "- Se nessuna carica matcha, usa `soggetti_in_carica[0]`.\n"
                                        "- Se il campo del modulo indica esplicitamente un ruolo (es. 'direttore tecnico'), allora scegli la prima persona che matcha quel ruolo.\n\n"
                                        "REGOLE EMAIL/PEC:\n"
                                        "- Se il campo chiede `email`, `e-mail`, `mail`, `indirizzo email`, e nel JSON esiste solo `mail_pec`, usa comunque una mail PEC disponibile.\n"
                                        "- Se esistono più valori in `mail_pec`, preferisci il primo valore non vuoto, salvo che il campo chieda esplicitamente PEC: in quel caso usa la PEC più appropriata.\n\n"
                                        "REGOLE SEDE/INDIRIZZO:\n"
                                        "- Se il campo chiede `sede`, `sede legale`, `sede operativa`, `indirizzo sede`, usa 'sede_legale_operativa' se presente.\n"
                                        "- Se il JSON è annidato sotto `azienda`, usa `azienda.sede_legale_operativa.indirizzo`.\n"
                                        "- Non lasciare vuoto un campo `sede` se nel JSON esiste un indirizzo in `sede_legale_operativa.indirizzo`.\n\n"
                                        "OUTPUT: rispondi con 3 sezioni in QUESTO ordine e con QUESTI titoli esatti:\n"
                                        "=== CAMPI ===\n"
                                        "Elenca i campi da compilare che vedi nell'immagine. Per ciascuno crea un oggetto con:\n"
                                        "- field_id: string (es. \"f1\", \"f2\"...)\n"
                                        "- label: testo del campo (es. \"Il/La sottoscritto/a\", \"nato/a a\", \"codice fiscale\")\n"
                                        "- field_type: uno tra [\"text\", \"date\", \"checkbox\"]\n"
                                        "- Per checkbox/caselle: restituisci nel MATCH solo quelle da spuntare.\n"
                                        "- Se una checkbox va spuntata, usa field_type=\"checkbox\", value=true e source_path reale dal JSON.\n"
                                        "- Se una checkbox NON va spuntata, non inserirla nei matches oppure usa value=null.\n"
                                        "\n"
                                        "REGOLE DI ESTRAZIONE CAMPI (IMPORTANTI):\n"
                                        "- Ogni SPAZIO DA COMPILARE è un campo distinto (linee vuote, underscore, puntini, spazi in tabelle, caselle/checkbox).\n"
                                        "- NON unire più campi della stessa riga in un solo label.\n"
                                        "- Se sulla stessa riga ci sono più spazi da compilare, crea più campi separati nell'ordine sinistra→destra.\n"
                                        "- Esempio: \"Il/La sottoscritto/a ____ nato/a a ____ (prov. ____)\" sono 3 campi distinti.\n"
                                        "- Un label non deve contenere più concetti tipo \"sottoscritto/a nato/a a (prov.)\" o \"residente a (prov.) indirizzo\": splitta in campi singoli.\n"
                                        "- Per assegnare il label usa il testo più vicino al singolo spazio da compilare (a sinistra/destra e, se serve, sopra).\n"
                                        "\n"
                                        "=== DATI ===\n"
                                        "Elenca i dati utili presenti nel JSON come lista di oggetti:\n"
                                        "- path: path JSON reale (es. \"azienda.ragione_sociale\")\n"
                                        "- value: valore (string/number/bool)\n"
                                        "\n"
                                        "=== MATCH ===\n"
                                        "Produci un JSON valido (solo JSON, niente spiegazioni) con schema:\n"
                                        "{\n"
                                        "  \"matches\": [\n"
                                        "    {\n"
                                        "      \"field_id\": \"f1\",\n"
                                        "      \"label\": \"...\",\n"
                                        "      \"field_type\": \"text\" | \"date\" | \"checkbox\",\n"
                                        "      \"value\": \"...\" | null | true | false,\n"
                                        "      \"source_path\": \"...\" | null,\n"
                                        "      \"confidence\": 0.0\n"
                                        "    }\n"
                                        "  ]\n"
                                        "}\n"
                                        "- Se non trovi valore: value=null e source_path=null.\n"
                                        "- confidence tra 0 e 1.\n\n"
                                        "JSON DATI (riferimento unico):\n"
                                        f"{json.dumps(data_json, ensure_ascii=False)}"
                                    ),
                                },
                            ]
                            + image_parts
                        ),
                    }
                ],
            }
    
            req = request.Request(
                MISTRAL_API_URL,
                method="POST",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    
            last_exc = None
            for attempt in range(6):
                try:
                    with request.urlopen(req, timeout=180) as resp:
                        raw = json.loads(resp.read().decode("utf-8"))
                    last_exc = None
                    break
                except HTTPError as exc:
                    last_exc = exc
                    if exc.code != 429:
                        raise
                    wait_s = min(120, 10 * (2 ** attempt))  # 10s,20s,40s,80s,120s,120s
                    print(f"[LLM][vision-mapping] 429 rate limit, retry in {wait_s}s (attempt {attempt+1}/6)")
                    time.sleep(wait_s)
    
            if last_exc is not None:
                raise last_exc
    
    
            # First try: robust JSON extraction from the raw API payload.
            match_obj = None
            match_err = None
            try:
                parsed = parse_llm_json_payload(raw)
                if isinstance(parsed, (dict, list)):
                    match_obj = parsed if isinstance(parsed, dict) else {"matches": parsed}
            except Exception as exc:
                match_err = str(exc)
    
            # Fallback: parse from extracted text using the MATCH marker.
            if match_obj is None:
                text = _extract_text_from_response(raw)
                match_obj, match_err = _extract_match_json(text or "")
                if match_obj is None:
                    # Non interrompere l'intera pipeline per una singola pagina con risposta
                    # malformata (succede spesso dopo 429 / output troncato). Prosegui.
                    err = {
                        "image": image_path.name,
                        "error": match_err or "Impossibile estrarre JSON dal modello.",
                        "text_excerpt": (text or "")[:600],
                    }
                    print(f"[LLM][vision-mapping] WARN: skip {image_path.name} - {match_err or 'no_json'}")
                    return image_path.name, [], err
            # Poi lascia questo pezzo sotto (è la sola parte “diversa”):

            coerced = _coerce_match_output(match_obj, data_json)
            coerced = _force_sottoscritto_from_person_paths(coerced, data_json)
            for match in coerced.get("matches") or []:
                match["image_page"] = image_path.name
                matches_for_page.append(match)
            print(f"[LLM][vision-mapping] done image={image_path.name}", flush=True)
            return image_path.name, matches_for_page, None

        except Exception as e:
            err = {"image": image_path.name, "error": f"{type(e).__name__}: {e}"}
            return image_path.name, [], err

    results_by_image: dict[str, list[dict[str, Any]]] = {}
    errors_by_image: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(_process_one, p) for p in image_paths]
        for fut in as_completed(futures):
            key, matches_for_page, err = fut.result()
            results_by_image[key] = matches_for_page
            if err is not None:
                errors_by_image[key] = err

    for key in sorted(results_by_image.keys()):
        all_matches.extend(results_by_image[key])
    for key in sorted(errors_by_image.keys()):
        page_errors.append(errors_by_image[key])


    coerced = {
        "matches": all_matches,
        "stats": {
            "filled": sum(1 for item in all_matches if item.get("value") is not None),
            "total": len(all_matches),
        },
        "errors": page_errors,
    }


    out_path = Path(out_json_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(coerced, ensure_ascii=False, indent=2), encoding="utf-8")
    return coerced


def main() -> int:
    parser = argparse.ArgumentParser(description="Vision smoke-test: summarize what the image shows via Mistral.")
    parser.add_argument(
        "--image-dir",
        default=r"",
        help="Cartella con le immagini annotate (tutte le pagine).",
    )
    parser.add_argument(
        "--data-json",
        default=r"",
        help="Path JSON dati azienda/anagrafica (xml_data.json).",
    )
    parser.add_argument(
        "--out-json",
        default=r"",
        help="Path output JSON (salvato su disco, non stampato).",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Modello Mistral vision (es. mistral-medium-2508).")
    parser.add_argument("--max-tokens", type=int, default=5000, help="Max token risposta.")
    args = parser.parse_args()

    if not args.image_dir.strip():
        raise ValueError("--image-dir obbligatorio")
    if not args.data_json.strip():
        raise ValueError("--data-json obbligatorio")
    if not args.out_json.strip():
        raise ValueError("--out-json obbligatorio")

    result = run_vision_mapping(
        image_dir=args.image_dir,
        data_json_path=args.data_json,
        out_json_path=args.out_json,
        model=args.model,
        max_tokens=args.max_tokens,
    )
    stats = result.get("stats") or {}
    print(f"[vision] compiled_fields={stats.get('filled')}/{stats.get('total')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
