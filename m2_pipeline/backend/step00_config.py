from pathlib import Path
import os


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PIPELINE_ROOT.parents[1]

M1_OUTPUT_DIR = PROJECT_ROOT / "Millestone_1" / "Consegna_Milestone_1" / "output_di_esempio"
DEFAULT_XML_INPUT = (
    PROJECT_ROOT
    / "Sample"
    / "extracted"
    / "Gara Comune di Matera"
    / "G01121_eDGUE-IT_request.xml"
)
DEFAULT_OUTPUT_DIR = PIPELINE_ROOT / "output"
DEFAULT_XML_JSON_DIR = DEFAULT_OUTPUT_DIR

XML_JSON_FILENAME = "xml_data.json"
CLASSIFICATION_FILENAME = "documenti_da_compilare.json"
FIELD_MAPPING_FILENAME = "campo_valore.json"
FINAL_DOCX_FILENAME = "documento_compilato_finale.docx"
SUMMARY_FILENAME = "riepilogo_compilazione.json"

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "i58x3rBFunIs5n7OOYyDsRoSPFigCRy0").strip()
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-medium-2508").strip()
MISTRAL_TIMEOUT_SEC = int(os.getenv("MISTRAL_TIMEOUT_SEC", "180"))
