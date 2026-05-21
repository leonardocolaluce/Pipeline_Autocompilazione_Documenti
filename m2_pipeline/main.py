import argparse
import json
from pathlib import Path
import shutil
import sys
import time
import os

from backend.step00_config import (
    CLASSIFICATION_FILENAME,
    FIELD_MAPPING_FILENAME,
    FINAL_DOCX_FILENAME,
    SUMMARY_FILENAME,
)
from backend.step07_classify_documents_llm import classify_bundles
from backend.step10_field_mapper_vision import map_bundle_fields_vision
from backend.step12_final_validator_llm import validate_and_prune
from backend.step04_import_file import discover_m1_bundles
from backend.step03_source_documents import resolve_source_docx, resolve_source_document, resolve_source_pdf
from backend.step11_writer_docx import write_docx_from_mapping, write_docx_preview_from_answers_json
from backend.step11_writer_pdf import write_pdf_from_answers_json
from backend.step13_provisional_excel_export import EXCEL_PROVISIONAL_FILENAME, export_mapping_comparison_to_xlsx
from backend.step06_xml_to_json_bridge import convert_xml_with_existing_script
from backend.step10_merge_tables_into_mapping import merge_tables_filled_into_mapping
from backend.step14_regex_validator import clean_mapping_with_regex_rules
from backend.step15_docx_render_qc_mistral import qc_docx_render_first_page


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_FOLDER = PROJECT_ROOT / "Millestone_1" / "Consegna_Milestone_1" / "output_di_esempio"
INPUT_XML = PROJECT_ROOT / "Sample" / "extracted" / "Gara Comune di Matera" / "G01121_eDGUE-IT_request.xml"
OUTPUT_FOLDER = PROJECT_ROOT / "Millestone_2" / "pipeline_2" / "output"
DEFAULT_BUNDLE_NAME = "Domanda di partecipazione Palazzo Nugent"
DEFAULT_VENV_PYTHON = Path(sys.executable)
DEFAULT_DATA_JSON = PROJECT_ROOT / "Millestone_2" / "anagrafica_NIKANTE.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pipeline 2 rebuilt incrementally.")
    parser.add_argument(
        "--m1-dir",
        default=str(INPUT_FOLDER),
        help="Cartella con gli output JSON della pipeline 1.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(OUTPUT_FOLDER),
        help="Cartella output della pipeline 2.",
    )
    parser.add_argument(
        "--step",
        default="run_all",
        choices=["scan_m1", "classify_documents", "convert_xml", "map_fields", "write_docx", "validate_final", "run_all"],
        help="Step da eseguire.",
    )
    parser.add_argument(
        "--xml-input",
        default=str(INPUT_XML),
        help="Path XML da convertire con xml_to_json.py.",
    )
    parser.add_argument(
        "--data-json",
        default=str(DEFAULT_DATA_JSON),
        help="Path JSON dati/anagrafica (in alternativa a --xml-input).",
    )
    parser.add_argument(
        "--bundle-name",
        default=DEFAULT_BUNDLE_NAME,
        help="Nome base del bundle M1 da processare negli step documento-singolo.",
    )
    parser.add_argument(
        "--venv-python",
        default=str(DEFAULT_VENV_PYTHON),
        help="Interprete del venv locale per lo step write_docx.",
    )
    return parser


def run_scan_m1(m1_dir: str, output_dir: str) -> dict:
    bundles = discover_m1_bundles(Path(m1_dir))
    return {
        "status": "ok",
        "step": "scan_m1",
        "bundle_count": len(bundles),
        "bundle_names": [item["base_name"] for item in bundles],
    }


def run_classify_documents(m1_dir: str, output_dir: str) -> dict:
    bundles = discover_m1_bundles(Path(m1_dir))
    classifications = classify_bundles(bundles)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / CLASSIFICATION_FILENAME
    payload = {
        "step": "classify_documents",
        "m1_dir": str(Path(m1_dir).resolve()),
        "document_count": len(classifications),
        "documents": classifications,
    }
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return {
        "status": "ok",
        "step": "classify_documents",
        "output": str(out_path),
        "compilable_count": sum(1 for item in classifications if item["is_compilable"]),
    }


def run_convert_xml(output_dir: str, xml_input: str | None) -> dict:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # If a JSON input is provided, use it directly (skip XML conversion) and place it
    # where downstream steps expect it (xml_data.json).
    if xml_input and str(xml_input).lower().endswith(".json"):
        json_input = Path(xml_input).resolve()
        if not json_input.exists():
            raise FileNotFoundError(f"JSON input non trovato: {json_input}")

        ts = time.strftime("%Y%m%d_%H%M%S")
        json_output = Path.home() / "Desktop" / "xml_data_cache" / f"xml_data_{ts}.json"
        json_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(json_input, json_output)

        return {
            "status": "ok",
            "step": "convert_xml",
            "xml_input": None,
            "json_output": str(json_output),
            "mode": "json_passthrough",
            "json_input": str(json_input),
        }


    result = convert_xml_with_existing_script(xml_path=xml_input, output_dir=out_dir)
    return {
        "status": "ok",
        "step": "convert_xml",
        "xml_input": result["xml_input"],
        "json_output": result["json_output"],
        "mode": "xml_to_json",
    }


def run_map_fields(m1_dir: str, output_dir: str, xml_input: str | None, bundle_name: str | None) -> dict:
    bundles = discover_m1_bundles(Path(m1_dir))
    classifications_path = Path(output_dir) / CLASSIFICATION_FILENAME
    with open(classifications_path, "r", encoding="utf-8") as handle:
        classifications_payload = json.load(handle)
    classifications = {item["base_name"]: item for item in (classifications_payload.get("documents") or [])}

    xml_result = run_convert_xml(output_dir, xml_input)

    selected_bundle = None
    for bundle in bundles:
        if bundle_name and bundle["base_name"] != bundle_name:
            continue
        if classifications.get(bundle["base_name"], {}).get("is_compilable"):
            selected_bundle = bundle
            break

    if selected_bundle is None:
        return {
            "status": "skipped",
            "step": "map_fields",
            "reason": "no_compilable_bundle",
            "bundle_name": bundle_name,
            "xml_output": xml_result.get("json_output"),
        }


    mapping_result = map_bundle_fields_vision(
        selected_bundle,
        xml_result["json_output"],
        Path(output_dir),
        m1_dir=m1_dir,
    )
    return {
        "status": "ok",
        "step": "map_fields",
        "bundle_name": selected_bundle["base_name"],
        "mapping_output": mapping_result["output_path"],
        "item_count": mapping_result["item_count"],
        "non_nd_count": mapping_result.get("filled_count", 0),
        "vision_match_output": mapping_result.get("vision_match_output"),
    }


def run_write_docx(output_dir: str, bundle_name: str | None, venv_python: str) -> dict:
    if not bundle_name:
        raise ValueError("Per write_docx serve --bundle-name.")

    mapping_path = Path(output_dir) / FIELD_MAPPING_FILENAME
    if not mapping_path.exists():
        raise FileNotFoundError(f"Mapping JSON non trovato: {mapping_path}")

    source_docx = resolve_source_docx(bundle_name)
    if source_docx is None:
        raise FileNotFoundError(f"Documento sorgente DOCX non trovato per bundle: {bundle_name}")

    with open(mapping_path, "r", encoding="utf-8") as handle:
        mapping_payload = json.load(handle)
    rows = mapping_payload.get("rows") or []
    effective_answers = [
        row for row in rows
        if str(row.get("answer", "")).strip() not in {"", "N/D"}
    ]

    final_docx = Path(output_dir) / FINAL_DOCX_FILENAME
    if not effective_answers:
        final_docx.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_docx, final_docx)
        return {
            "status": "ok",
            "step": "write_docx",
            "bundle_name": bundle_name,
            "compiled_output": str(final_docx),
            "replaced_count": 0,
        }

    result = write_docx_from_mapping(
        source_docx,
        mapping_path,
        final_docx,
    )
    return {
        "status": "ok",
        "step": "write_docx",
        "bundle_name": bundle_name,
        "compiled_output": result["output_path"],
        "replaced_count": result["replaced_count"],
    }


def run_validate_final(output_dir: str, bundle_name: str | None, venv_python: str) -> dict:
    if not bundle_name:
        raise ValueError("Per validate_final serve --bundle-name.")

    out_dir = Path(output_dir)
    compiled_docx = out_dir / FINAL_DOCX_FILENAME
    if not compiled_docx.exists():
        raise FileNotFoundError(f"DOCX finale non trovato: {compiled_docx}")

    validation = validate_and_prune(out_dir, compiled_docx)
    mapping_path = out_dir / FIELD_MAPPING_FILENAME
    with open(mapping_path, "r", encoding="utf-8") as handle:
        mapping_payload = json.load(handle)
    remaining_answers = [
        row for row in (mapping_payload.get("rows") or [])
        if str(row.get("answer", "")).strip() not in {"", "N/D"}
    ]
    if remaining_answers:
        write_res = run_write_docx(str(out_dir), bundle_name, venv_python)
        compiled_output = write_res["compiled_output"]
    else:
        compiled_output = str(compiled_docx)
    return {
        "status": "ok",
        "step": "validate_final",
        "removed_count": validation["removed_count"],
        "mapping_output": validation["mapping_path"],
        "summary_output": validation["summary_path"],
        "compiled_output": compiled_output,
    }


def run_all(
    m1_dir: str,
    output_dir: str,
    xml_input: str | None = None,
    bundle_name: str | None = None,
    venv_python: str = str(DEFAULT_VENV_PYTHON),
    data_json: str | None = None,
) -> dict:
    if data_json:
        xml_input = data_json
    if not bundle_name:
        bundles = discover_m1_bundles(Path(m1_dir))
        bundle_name = next((b["base_name"] for b in bundles if b.get("has_fields") or b.get("has_tables")), None)

    if not bundle_name:
        raise ValueError("Nessun bundle compilabile trovato.")

    classify_res = run_classify_documents(m1_dir, output_dir)
    xml_res = run_convert_xml(output_dir, xml_input)
    map_res = run_map_fields(m1_dir, output_dir, xml_input, bundle_name)
    if map_res.get("status") != "ok":
        return {
            "status": "skipped",
            "step": "run_all",
            "bundle_name": bundle_name,
            "classification_output": classify_res.get("output"),
            "xml_output": xml_res.get("json_output"),
            "reason": map_res.get("reason") or "no_compilable_bundle",
        }


    # Snapshot mapping before the validator (3rd LLM) potentially prunes answers.
    pre_validator_mapping = Path(output_dir) / "campo_valore_provvisorio.json"
    try:
        mapping_src = Path(output_dir) / FIELD_MAPPING_FILENAME
        if mapping_src.exists():
            shutil.copy2(mapping_src, pre_validator_mapping)
    except Exception:
        pass

    # Merge LLM vision table cells into the pre-validator mapping snapshot (strict coordinates).
    # Merge LLM vision table cells into the pre-validator mapping snapshot (strict coordinates).
    try:
        tables_filled = Path(output_dir) / "tables_filled_output.json"
        if tables_filled.exists() and pre_validator_mapping.exists():
            merge_tables_filled_into_mapping(
                tables_filled_json_path=tables_filled,
                mapping_json_path=pre_validator_mapping,
                only_if_empty=True,
            )
    except Exception:
        pass

    # Regex validator: pulisce il mapping provvisorio COMPLETO (campi + tabelle) prima del preview.
    try:
        if pre_validator_mapping.exists():
            clean_mapping_with_regex_rules(
                mapping_json_path=pre_validator_mapping,
                data_json_path=xml_res["json_output"],
            )
    except Exception:
        pass

    source_doc = resolve_source_document(bundle_name)
    pdf_mode = bool(source_doc is not None and source_doc.suffix.lower() == ".pdf")
    source_pdf = source_doc if pdf_mode else resolve_source_pdf(bundle_name)
    
    preview_pdf_path = Path(output_dir) / "documento_compilato_preview.pdf"
    preview_path = Path(output_dir) / "documento_compilato_preview.docx"
    provisional_docx_path = Path(output_dir) / "documento_compilato_provvisorio.docx"
    qc_json_path = Path(output_dir) / "document_quality.json"
    compiled_pdf_path = Path(output_dir) / "documento_compilato_finale.pdf"
    
    if pdf_mode:
        try:
            if source_doc is not None and pre_validator_mapping.exists():
                write_pdf_from_answers_json(
                    source_doc,
                    pre_validator_mapping,
                    preview_pdf_path,
                    color_rgb=(0, 0, 1),
                    add_white_bg=False,
                )
        except Exception:
            pass
    
        try:
            validate_res = validate_and_prune(Path(output_dir), Path(output_dir) / "__compiled_docx_not_available__.docx")
        except Exception:
            validate_res = {"removed_count": 0}
    else:
        write_res = run_write_docx(output_dir, bundle_name, venv_python)

        # --- QC render (Mistral) on provisional DOCX before generating preview ---
        try:
            source_docx = resolve_source_docx(bundle_name)
            if source_docx is not None and pre_validator_mapping.exists():
                # First pass with default offset (WORD_Y_OFFSET unset/0).
                os.environ.pop("WORD_Y_OFFSET", None)
                write_docx_preview_from_answers_json(
                    source_docx,
                    pre_validator_mapping,
                    provisional_docx_path,
                    color_hex="000000",
                )
                try:
                    st = provisional_docx_path.stat()
                    print(
                        f"[QC] provisional_written path={provisional_docx_path} "
                        f"size={st.st_size} mtime={st.st_mtime} WORD_Y_OFFSET_env={os.getenv('WORD_Y_OFFSET')}",
                        flush=True,
                    )
                except Exception as e:
                    print(f"[QC] provisional_written stat_error={e}", flush=True)
                qc_res = qc_docx_render_first_page(
                    compiled_docx_path=provisional_docx_path,
                    out_json_path=qc_json_path,
                    work_dir=Path(output_dir),
                )
                if not bool(qc_res.get("good", True)):
                    print(
                        f"[QC] good=False confidence={qc_res.get('confidence')} "
                        f"reason={qc_res.get('reason')} -> applying WORD_Y_OFFSET=-10",
                        flush=True,
                    )
                    # Re-write with vertical shift up.
                    os.environ["WORD_Y_OFFSET"] = "-10"
                    write_docx_preview_from_answers_json(
                        source_docx,
                        pre_validator_mapping,
                        provisional_docx_path,
                        color_hex="000000",
                    )
                    try:
                        st = provisional_docx_path.stat()
                        print(
                            f"[QC] provisional_rewritten path={provisional_docx_path} "
                            f"size={st.st_size} mtime={st.st_mtime} WORD_Y_OFFSET_env={os.getenv('WORD_Y_OFFSET')}",
                            flush=True,
                        )
                    except Exception as e:
                        print(f"[QC] provisional_rewritten stat_error={e}", flush=True)
                    # Best-effort: overwrite QC report with post-fix evaluation.
                    try:
                        qc_docx_render_first_page(
                            compiled_docx_path=provisional_docx_path,
                            out_json_path=qc_json_path,
                            work_dir=Path(output_dir),
                        )
                    except Exception:
                        pass
                else:
                    print(
                        f"[QC] good=True confidence={qc_res.get('confidence')} reason={qc_res.get('reason')}",
                        flush=True,
                    )
        except Exception as exc:
            # QC is best-effort; do not block the pipeline if it fails.
            print(f"[QC] skipped err={type(exc).__name__}: {exc}", flush=True)

        try:
            if provisional_docx_path.exists():
                preview_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(provisional_docx_path, preview_path)
        except Exception:
            pass
    
        try:
            if source_pdf is not None and pre_validator_mapping.exists():
                write_pdf_from_answers_json(
                    source_pdf,
                    pre_validator_mapping,
                    preview_pdf_path,
                    color_rgb=(0, 0, 1),
                    add_white_bg=False,
                )
        except Exception:
            pass
    
        validate_res = run_validate_final(output_dir, bundle_name, venv_python)


    post_validator_mapping = Path(output_dir) / "campo_valore_finale.json"
    try:
        mapping_src = Path(output_dir) / FIELD_MAPPING_FILENAME
        if mapping_src.exists():
            shutil.copy2(mapping_src, post_validator_mapping)
    except Exception:
        pass
    
    try:
        mapping_src = Path(output_dir) / FIELD_MAPPING_FILENAME
        if source_pdf is not None and mapping_src.exists():
            write_pdf_from_answers_json(
                source_pdf,
                mapping_src,
                compiled_pdf_path,
                color_rgb=(0, 0, 1),
                add_white_bg=False,
            )
    except Exception:
        pass
    
    provisional_excel_path = Path(output_dir) / EXCEL_PROVISIONAL_FILENAME

    try:
        if pre_validator_mapping.exists() and post_validator_mapping.exists():
            export_mapping_comparison_to_xlsx(
                pre_validator_mapping,
                post_validator_mapping,
                provisional_excel_path,
            )
    except Exception:
        pass

    return {
        "status": "ok",
        "step": "run_all",
        "bundle_name": bundle_name,
        "classification_output": classify_res["output"],
        "xml_output": xml_res["json_output"],
        "mapping_output": map_res["mapping_output"],
        "mapping_output_provvisorio": str(pre_validator_mapping) if pre_validator_mapping.exists() else None,
        "mapping_output_provvisorio_excel": str(provisional_excel_path) if provisional_excel_path.exists() else None,
        "mapping_output_finale": str(post_validator_mapping) if post_validator_mapping.exists() else None,
        "summary_output": str(Path(output_dir) / SUMMARY_FILENAME),
        "compiled_output": (write_res["compiled_output"] if not pdf_mode else None),
        "compiled_output_preview": (str(preview_path) if (not pdf_mode and preview_path.exists()) else None),
        "compiled_output_preview_pdf": str(preview_pdf_path) if preview_pdf_path.exists() else None,
        "compiled_output_pdf": str(compiled_pdf_path) if compiled_pdf_path.exists() else None,
        "validator_removed_count": int(validate_res.get("removed_count", 0) or 0),
    }


def main() -> None:
    args = build_parser().parse_args()
    if args.step == "scan_m1":
        result = run_scan_m1(args.m1_dir, args.output_dir)
    elif args.step == "classify_documents":
        result = run_classify_documents(args.m1_dir, args.output_dir)
    elif args.step == "convert_xml":
        # For backward compatibility, allow passing a JSON path via --xml-input
        # or use --data-json.
        xml_or_json = args.xml_input
        if args.data_json:
            xml_or_json = args.data_json
        result = run_convert_xml(args.output_dir, xml_or_json)
    elif args.step == "map_fields":
        xml_or_json = args.xml_input
        if args.data_json:
            xml_or_json = args.data_json
        result = run_map_fields(args.m1_dir, args.output_dir, xml_or_json, args.bundle_name)
    elif args.step == "write_docx":
        result = run_write_docx(args.output_dir, args.bundle_name, args.venv_python)
    elif args.step == "validate_final":
        result = run_validate_final(args.output_dir, args.bundle_name, args.venv_python)
    elif args.step == "run_all":
        xml_or_json = args.xml_input
        if args.data_json:
            xml_or_json = args.data_json
        result = run_all(args.m1_dir, args.output_dir, xml_or_json, args.bundle_name, args.venv_python)
    else:
        raise ValueError(f"Step non gestito: {args.step}")

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
