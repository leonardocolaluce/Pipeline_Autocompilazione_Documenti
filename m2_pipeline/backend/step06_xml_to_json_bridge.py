import contextlib
import os
from pathlib import Path
from typing import Dict

from .step00_config import DEFAULT_XML_INPUT, DEFAULT_XML_JSON_DIR, PIPELINE_ROOT, XML_JSON_FILENAME


XML_TO_JSON_SOURCE = PIPELINE_ROOT / "backend" / "step05_xml_to_json.py"


@contextlib.contextmanager
def _pushd(path: Path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _load_xml_to_json_main():
    if not XML_TO_JSON_SOURCE.exists():
        raise FileNotFoundError(f"xml_to_json.py non trovato: {XML_TO_JSON_SOURCE}")

    source = XML_TO_JSON_SOURCE.read_text(encoding="utf-8")
    marker = "# INPUT FILE"
    usable_source = source.split(marker)[0] if marker in source else source

    namespace: Dict[str, object] = {}
    exec(usable_source, namespace)
    main_fn = namespace.get("main")
    if not callable(main_fn):
        raise RuntimeError("Funzione main(xml_path) non trovata in xml_to_json.py")
    return main_fn


def convert_xml_with_existing_script(
    xml_path: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> Dict[str, str]:
    xml_file = Path(xml_path or DEFAULT_XML_INPUT).resolve()
    if not xml_file.exists():
        raise FileNotFoundError(f"XML input non trovato: {xml_file}")

    out_dir = Path(output_dir or DEFAULT_XML_JSON_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    main_fn = _load_xml_to_json_main()
    with _pushd(out_dir):
        main_fn(str(xml_file))

    generated = out_dir / "output.json"
    if not generated.exists():
        raise FileNotFoundError(f"JSON non generato da xml_to_json.py in: {generated}")

    final_path = out_dir / XML_JSON_FILENAME
    if final_path.exists():
        final_path.unlink()
    generated.replace(final_path)

    return {
        "xml_input": str(xml_file),
        "json_output": str(final_path),
    }
