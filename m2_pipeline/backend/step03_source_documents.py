import re
import zipfile
import os
from pathlib import Path
from typing import Optional
from functools import lru_cache
from .step00_config import PROJECT_ROOT


SAMPLE_EXTRACTED_ROOT = PROJECT_ROOT / "Sample" / "extracted"


def _extra_roots() -> list[Path]:
    raw = os.getenv("M2_EXTRA_DOCX_DIRS", "")
    if not raw.strip():
        return []
    roots = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        roots.append(Path(part))
    return roots


def _normalize_name(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def _score_candidate(target: str, candidate: str) -> int:
    score = 0
    if candidate == target:
        score = 1000
    elif target in candidate or candidate in target:
        score = min(len(target), len(candidate))
    return score


def _source_candidates() -> tuple[Path, ...]:
    candidates = []
    for suffix in ("*.docx", "*.pdf"):
        candidates.extend(
            path for path in SAMPLE_EXTRACTED_ROOT.rglob(suffix)
            if not path.name.startswith("~$")
        )
        for root in _extra_roots():
            if root.exists():
                candidates.extend(
                    path for path in root.rglob(suffix)
                    if not path.name.startswith("~$")
                )
    return tuple(candidates)


def resolve_source_document(base_name: str) -> Optional[Path]:
    forced = os.getenv("M2_FORCE_SOURCE_DOCX", "").strip()
    if forced:
        p = Path(forced)
        print(f"[SOURCE] resolve_source_document FORCED={p} exists={p.exists()}", flush=True)
        if p.exists():
            return p

    target = _normalize_name(base_name)
    best_path: Optional[Path] = None
    best_score = -1

    for path in _source_candidates():
        candidate = _normalize_name(path.stem)
        score = _score_candidate(target, candidate)
        if score > best_score:
            best_score = score
            best_path = path

    return best_path if best_score > 0 else None


def resolve_source_docx(base_name: str) -> Optional[Path]:
    override = os.getenv("M2_SOURCE_DOCX_OVERRIDE", "").strip()
    if override:
        p = Path(override)
        if p.exists() and p.suffix.lower() == ".docx":
            return p
    forced = os.getenv("M2_FORCE_SOURCE_DOCX", "").strip()
    if forced:
        p = Path(forced)
        print(f"[SOURCE] resolve_source_docx FORCED={p} exists={p.exists()}", flush=True)
        if p.exists() and p.suffix.lower() == ".docx":
            return p

    target = _normalize_name(base_name)
    best_path: Optional[Path] = None
    best_score = -1

    for root in _extra_roots() + [SAMPLE_EXTRACTED_ROOT]:
        if not root.exists():
            continue
        for path in root.rglob("*.docx"):
            if path.name.startswith("~$"):
                continue
            candidate = _normalize_name(path.stem)
            score = _score_candidate(target, candidate)
            if score > best_score:
                best_score = score
                best_path = path

    return best_path if best_score > 0 else None


def _extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        data = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    text = data.replace("</w:p>", "\n")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _extract_pdf_text(path: Path) -> str:
    raw = path.read_bytes()[:300000].decode("latin-1", errors="ignore")
    chunks = re.findall(r"\(([^()]*)\)\s*Tj", raw)
    chunks.extend(re.findall(r"\[(.*?)\]\s*TJ", raw, flags=re.DOTALL))
    if not chunks:
        chunks = re.findall(r"[A-Za-z0-9À-ÿ][A-Za-z0-9À-ÿ\s,;:.()/%'\"]{20,}", raw)
    text = "\n".join(chunk.replace("\\)", ")").replace("\\(", "(") for chunk in chunks)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@lru_cache(maxsize=128)
def _extract_source_text_cached(path_str: str, max_chars: int) -> str:
    path = Path(path_str)
    try:
        if path.suffix.lower() == ".docx":
            text = _extract_docx_text(path)
        elif path.suffix.lower() == ".pdf":
            text = _extract_pdf_text(path)
        else:
            text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    return text[:max_chars]


def extract_source_text(path: Path | None, max_chars: int = 12000) -> str:
    if path is None or not path.exists():
        return ""
    return _extract_source_text_cached(str(path.resolve()), max_chars)
