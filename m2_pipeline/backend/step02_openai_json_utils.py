import json
import re
from typing import Any


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
                if isinstance(part, dict):
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


def parse_llm_json_payload(raw: dict[str, Any]) -> Any:
    candidates = _extract_text_candidates(raw)
    if not candidates:
        raise ValueError("Risposta LLM senza testo utile.")

    errors: list[str] = []
    for candidate in candidates:
        normalized = _extract_json_fragment(candidate)
        escaped = _escape_control_chars_in_strings(normalized)
        for attempt in (candidate, normalized, escaped):
            try:
                return json.loads(attempt)
            except Exception as exc:
                preview = attempt[:300].replace("\n", " ")
                errors.append(f"{type(exc).__name__}: {preview}")

    raise ValueError("Impossibile estrarre JSON valido dalla risposta LLM. " + " | ".join(errors[:4]))


def parse_openai_json_payload(raw: dict[str, Any]) -> Any:
    return parse_llm_json_payload(raw)
