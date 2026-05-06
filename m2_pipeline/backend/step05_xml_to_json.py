import xml.etree.ElementTree as ET
import json
import os

# Namespace XML
ns = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


def get_text(node, path):
    el = node.find(path, ns)
    return el.text.strip() if el is not None and el.text else None


def parse_address(party):
    address = party.find("cac:PostalAddress", ns)
    if address is None:
        return None

    return {
        "street": get_text(address, "cbc:StreetName"),
        "city": get_text(address, "cbc:CityName"),
        "postal_code": get_text(address, "cbc:PostalZone"),
        "country": get_text(address, "cac:Country/cbc:Name"),
    }


def parse_party(party_node):
    if party_node is None:
        return None

    party_data = {
        "name": get_text(party_node, "cac:PartyName/cbc:Name"),
        "vat": get_text(party_node, "cac:PartyIdentification/cbc:ID"),
        "website": get_text(party_node, "cbc:WebsiteURI"),
        "endpoint": get_text(party_node, "cbc:EndpointID"),
        "address": parse_address(party_node),
        "contact": get_text(party_node, "cac:Contact/cbc:Name"),
    }

    # Service Provider (annidato)
    service_provider = party_node.find("cac:ServiceProviderParty/cac:Party", ns)
    if service_provider is not None:
        party_data["service_provider"] = parse_party(service_provider)

    return party_data


def parse_procurement_project(root):
    project = root.find("cac:ProcurementProject", ns)
    lot = root.find("cac:ProcurementProjectLot", ns)

    if project is None:
        return None

    return {
        "name": get_text(project, "cbc:Name"),
        "type": get_text(project, "cbc:ProcurementTypeCode"),
        "cpv_code": get_text(project, "cac:MainCommodityClassification/cbc:ItemClassificationCode"),
        "lot": get_text(lot, "cbc:ID") if lot is not None else None,
    }


def main(xml_path):
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Contracting Party (principale)
    contracting_party = root.find("cac:ContractingParty/cac:Party", ns)
    party_data = parse_party(contracting_party)

    # Project
    project_data = parse_procurement_project(root)

    result = {
        "contracting_party": party_data,
        "procurement_project": project_data,
    }

    # Salvataggio JSON nella stessa cartella di esecuzione
    output_path = os.path.join(os.getcwd(), "output.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4, ensure_ascii=False)

    print(f"JSON salvato in: {output_path}")

