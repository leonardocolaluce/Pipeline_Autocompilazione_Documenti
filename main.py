import argparse
from pathlib import Path
import sys
import importlib.util
import os
import re
import shutil


ROOT = Path(__file__).resolve().parent
M1_PATH = ROOT / "m1_pipeline" / "main.py"
M2_PATH = ROOT / "m2_pipeline" / "main.py"

DEFAULT_DOC_INPUT = r"C:\Users\39334\Desktop\Autocompilazione file\Millestone_3\pipeline_4\new\file_sample\Domanda di partecipazione Palazzo Nugent.docx"
DEFAULT_DATA_JSON = r"C:\Users\39334\Desktop\Autocompilazione file\Millestone_2\anagrafica_NIKANTE.json"
DEFAULT_OUTPUT_DIR = str(ROOT / "output")


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Impossibile caricare modulo: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


WINDOWS_DRIVE_RE = re.compile(r"^(?P<drive>[A-Za-z]):[\\/](?P<rest>.*)$")


def _coerce_path(value: str | Path) -> Path:
    if isinstance(value, Path):
        return value
    text = str(value).strip()
    match = WINDOWS_DRIVE_RE.match(text)
    if match and os.name != "nt":
        drive = match.group("drive").lower()
        rest = match.group("rest").replace("\\", "/").lstrip("/")
        return Path("/mnt") / drive / rest
    return Path(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pipeline 3: Milestone 1 + Milestone 2 unificate.")
    parser.add_argument("--doc-input", default=DEFAULT_DOC_INPUT, help="File PDF/DOC/DOCX di input.")
    parser.add_argument("--data-json", default=DEFAULT_DATA_JSON, help="JSON anagrafica/dati per compilazione.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Cartella output complessiva.")
    parser.add_argument("--bundle-name", default=None, help="Nome bundle per M2 (opzionale).")
    parser.add_argument("--source-docx", default=None, help="DOCX template da usare se --doc-input è PDF (opzionale).")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    doc_input = _coerce_path(args.doc_input)
    source_docx = _coerce_path(args.source_docx) if getattr(args, "source_docx", None) else None
    data_json = _coerce_path(args.data_json)
    output_root = Path(args.output_dir).resolve()

    if not doc_input.exists():
        raise FileNotFoundError(f"Documento input non trovato: {doc_input}")
    if not data_json.exists():
        raise FileNotFoundError(f"JSON input non trovato: {data_json}")

    m1_out = output_root / "m1_output"
    m2_out = output_root / "m2_output"
    m1_out.mkdir(parents=True, exist_ok=True)
    m2_out.mkdir(parents=True, exist_ok=True)

    # --- Milestone 1 ---
    sys.path.insert(0, str(ROOT / "m1_pipeline"))
    m1_main = _load_module("m1_main", M1_PATH)
    # ensure source docx is available to milestone 2
    docx_source = None
    if doc_input.suffix.lower() == ".doc":
        from m1_pipeline.loaders.word_loader import convert_doc_to_docx
        temp_docx = Path(convert_doc_to_docx(str(doc_input)))
        docx_source = m1_out / f"{doc_input.stem}.docx"
        shutil.copy2(temp_docx, docx_source)
        try:
            shutil.rmtree(temp_docx.parent, ignore_errors=True)
        except Exception:
            pass
    else:
        docx_source = m1_out / doc_input.name
        shutil.copy2(doc_input, docx_source)

    m1_main.process(str(doc_input), output_path=None, merge_nearby=False, output_dir=str(m1_out))

    # --- Milestone 2 ---
    sys.path.insert(0, str(ROOT / "m2_pipeline"))
    os.environ["M2_EXTRA_DOCX_DIRS"] = str(m1_out)
    if source_docx and not source_docx.exists(): raise FileNotFoundError(f"DOCX template non trovato: {source_docx}")
    if source_docx: os.environ["M2_SOURCE_DOCX_OVERRIDE"] = str(source_docx)
    m2_main = _load_module("m2_main", M2_PATH)
    os.environ["M2_CLASSIFY_INPUT_DOC"] = str(doc_input)
    m2_result = m2_main.run_all(
        m1_dir=str(m1_out),
        output_dir=str(m2_out),
        data_json=str(data_json),
        bundle_name=args.bundle_name,
        venv_python=sys.executable,
    )

    compiled_output = None
    compiled_preview = None
    compiled_output_pdf = None
    compiled_preview_pdf = None
    if isinstance(m2_result, dict):
        compiled_output = m2_result.get("compiled_output")
        compiled_preview = m2_result.get("compiled_output_preview")
        compiled_output_pdf = m2_result.get("compiled_output_pdf")
        compiled_preview_pdf = m2_result.get("compiled_output_preview_pdf")
    compiled_path = Path(compiled_output) if compiled_output else (m2_out / "documento_compilato_finale.docx")

    compiled_pdf_path = Path(compiled_output_pdf) if compiled_output_pdf else (m2_out / "documento_compilato_finale.pdf")
    if compiled_pdf_path.exists():
        compiled_dir = ROOT / "file_compilati"
        compiled_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(compiled_pdf_path, compiled_dir / compiled_pdf_path.name)
    
    preview_pdf_path = Path(compiled_preview_pdf) if compiled_preview_pdf else (m2_out / "documento_compilato_preview.pdf")
    if preview_pdf_path.exists():
        compiled_dir = ROOT / "file_compilati"
        compiled_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(preview_pdf_path, compiled_dir / preview_pdf_path.name)

    if compiled_path.exists():
        compiled_dir = ROOT / "file_compilati"
        compiled_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(compiled_path, compiled_dir / compiled_path.name)
    preview_path = Path(compiled_preview) if compiled_preview else (m2_out / "documento_compilato_preview.docx")
    if preview_path.exists():
        compiled_dir = ROOT / "file_compilati"
        compiled_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(preview_path, compiled_dir / preview_path.name)


if __name__ == "__main__":
    main()
