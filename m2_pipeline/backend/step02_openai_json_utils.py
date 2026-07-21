import ast
import json
import re
from typing import Any

try:
    from json_repair import loads as _json_repair_loads
except ImportError:  # Optional: the pipeline still has local fallbacks.
    _json_repair_loads = None


_MATCH_MARKER = "=== MATCH ==="
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_BARE_KEY_RE = re.compile(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)')


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _escape_control_chars_in_strings(text: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = False
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue
        out.append(ch)
        if ch == '"':
            in_string = True
    return "".join(out)


def _extract_text_candidates(raw: dict[str, Any]) -> list[str]:
    candidates: list[str] = []

    output_text = str(raw.get("output_text", "") or "").strip()
    if output_text:
        candidates.append(output_text)

    for output_item in raw.get("output") or []:
        if not isinstance(output_item, dict):
            continue
        for content_item in output_item.get("content") or []:
            if not isinstance(content_item, dict):
                continue
            text_value = content_item.get("text")
            if isinstance(text_value, str) and text_value.strip():
                candidates.append(text_value.strip())

    for choice in raw.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            candidates.append(content.strip())
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                text_value = part.get("text") or part.get("content")
                if isinstance(text_value, str) and text_value.strip():
                    candidates.append(text_value.strip())

    return candidates


def _extract_json_fragment(text: str) -> str:
    text = _strip_code_fences(text)
    if not text:
        return text

    starts = [idx for idx in (text.find("{"), text.find("[")) if idx >= 0]
    if not starts:
        return text
    start = min(starts)

    ends = [idx for idx in (text.rfind("}"), text.rfind("]")) if idx >= 0]
    if not ends:
        return text[start:]
    end = max(ends)

    if end >= start:
        return text[start : end + 1].strip()
    return text[start:].strip()


def _candidate_fragments(text: str) -> list[tuple[str, str]]:
    """Return likely JSON portions, preferring the explicit MATCH section."""
    text = str(text or "").strip()
    candidates: list[tuple[str, str]] = []

    if _MATCH_MARKER in text:
        match_tail = text.split(_MATCH_MARKER, 1)[1]
        candidates.append(("match_fragment", _extract_json_fragment(match_tail)))
        candidates.append(("match_tail", _strip_code_fences(match_tail)))

    candidates.append(("fragment", _extract_json_fragment(text)))
    candidates.append(("full_text", _strip_code_fences(text)))

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in candidates:
        value = value.strip()
        if value and value not in seen:
            unique.append((name, value))
            seen.add(value)
    return unique


def _local_repair_variants(text: str) -> list[tuple[str, str]]:
    escaped = _escape_control_chars_in_strings(text)
    no_trailing_commas = _TRAILING_COMMA_RE.sub(r"\1", escaped)
    quoted_keys = _BARE_KEY_RE.sub(r'\1"\2"\3', no_trailing_commas)
    python_literals = re.sub(r"\bNone\b", "null", quoted_keys)
    python_literals = re.sub(r"\bTrue\b", "true", python_literals)
    python_literals = re.sub(r"\bFalse\b", "false", python_literals)

    variants = [
        ("escaped_controls", escaped),
        ("trailing_commas", no_trailing_commas),
        ("quoted_bare_keys", quoted_keys),
        ("python_literals", python_literals),
    ]
    unique: list[tuple[str, str]] = []
    seen: set[str] = {text}
    for name, value in variants:
        if value not in seen:
            unique.append((name, value))
            seen.add(value)
    return unique


def _close_truncated_json(text: str) -> str:
    """Close an unfinished string/object/array without changing complete content."""
    stack: list[str] = []
    in_string = False
    escaped = False

    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()

    repaired = text.rstrip()
    if in_string:
        if escaped:
            repaired += "\\"
        repaired += '"'
    repaired = re.sub(r",\s*$", "", repaired)
    repaired += "".join(reversed(stack))
    return _TRAILING_COMMA_RE.sub(r"\1", repaired)


def _recover_partial_matches(text: str) -> list[dict[str, Any]]:
    """Recover complete match objects when the final response is truncated."""
    if _MATCH_MARKER in text:
        text = text.split(_MATCH_MARKER, 1)[1]

    match_key = re.search(r'["\']?matches["\']?\s*:', text, re.IGNORECASE)
    if not match_key:
        return []

    tail = text[match_key.end() :]
    array_start = tail.find("[")
    if array_start < 0:
        return []
    tail = tail[array_start + 1 :]

    recovered: list[dict[str, Any]] = []
    depth = 0
    in_string = False
    escaped = False
    object_start: int | None = None

    for index, ch in enumerate(tail):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                object_start = index
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and object_start is not None:
                candidate = tail[object_start : index + 1]
                item = _parse_single_object(candidate)
                if isinstance(item, dict) and any(
                    key in item for key in ("field_id", "label", "value", "source_path")
                ):
                    recovered.append(item)
                object_start = None

    return recovered


def _parse_single_object(text: str) -> Any | None:
    attempts = [text]
    attempts.extend(value for _, value in _local_repair_variants(text))
    for attempt in attempts:
        try:
            return json.loads(attempt)
        except Exception:
            pass

    if _json_repair_loads is not None:
        try:
            return _json_repair_loads(text)
        except Exception:
            pass

    try:
        value = ast.literal_eval(text)
        return value if isinstance(value, (dict, list)) else None
    except Exception:
        return None


def parse_json_text_tolerant(text: str) -> tuple[Any, dict[str, Any]]:
    """Parse, locally repair, or partially recover an LLM JSON response."""
    original = str(text or "").strip()
    if not original:
        raise ValueError("Testo JSON vuoto.")

    errors: list[str] = []
    candidates = _candidate_fragments(original)

    # Strict parsing is always preferred.
    for candidate_name, candidate in candidates:
        try:
            return json.loads(candidate), {
                "parse_status": "ok",
                "parse_mode": candidate_name,
                "repaired": False,
            }
        except Exception as exc:
            errors.append(f"{candidate_name}: {type(exc).__name__}: {exc}")

    # Conservative local fixes: controls, trailing commas, bare keys, literals.
    for candidate_name, candidate in candidates:
        for repair_name, repaired in _local_repair_variants(candidate):
            try:
                return json.loads(repaired), {
                    "parse_status": "repaired",
                    "parse_mode": f"local:{candidate_name}:{repair_name}",
                    "repaired": True,
                    "errors_before_repair": errors[:5],
                }
            except Exception as exc:
                errors.append(f"local:{repair_name}: {type(exc).__name__}: {exc}")

    # Dedicated library, when installed, handles more severe syntax damage.
    if _json_repair_loads is not None:
        for candidate_name, candidate in candidates:
            try:
                payload = _json_repair_loads(candidate)
                if isinstance(payload, (dict, list)):
                    return payload, {
                        "parse_status": "repaired",
                        "parse_mode": f"json_repair:{candidate_name}",
                        "repaired": True,
                        "errors_before_repair": errors[:5],
                    }
            except Exception as exc:
                errors.append(f"json_repair: {type(exc).__name__}: {exc}")

    # Close truncated containers. This may preserve all complete preceding rows.
    for candidate_name, candidate in candidates:
        closed = _close_truncated_json(candidate)
        if closed == candidate:
            continue
        for repair_name, repaired in [("closed", closed)] + _local_repair_variants(closed):
            try:
                return json.loads(repaired), {
                    "parse_status": "repaired",
                    "parse_mode": f"truncated:{candidate_name}:{repair_name}",
                    "repaired": True,
                    "errors_before_repair": errors[:5],
                }
            except Exception as exc:
                errors.append(f"truncated:{repair_name}: {type(exc).__name__}: {exc}")

    # Last resort: retain complete objects instead of discarding the whole page.
    partial_matches = _recover_partial_matches(original)
    if partial_matches:
        return {"matches": partial_matches}, {
            "parse_status": "partial",
            "parse_mode": "partial_matches_recovery",
            "repaired": True,
            "recovered_matches": len(partial_matches),
            "errors_before_repair": errors[:5],
        }

    raise ValueError("JSON non recuperabile. " + " | ".join(errors[:8]))


def parse_llm_json_payload(raw: dict[str, Any]) -> Any:
    candidates = _extract_text_candidates(raw)
    if not candidates:
        raise ValueError("Risposta LLM senza testo utile.")

    errors: list[str] = []
    for candidate in candidates:
        try:
            payload, parse_info = parse_json_text_tolerant(candidate)
            if isinstance(payload, dict):
                payload.setdefault("_parse_info", parse_info)
            return payload
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    raise ValueError(
        "Impossibile estrarre o riparare JSON dalla risposta LLM. "
        + " | ".join(errors[:5])
    )


def parse_openai_json_payload(raw: dict[str, Any]) -> Any:
    return parse_llm_json_payload(raw)
