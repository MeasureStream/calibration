"""Il DCC dopo la pipeline: cio' che la pipeline non sa (passo 3.6 [S §6.2]).

``generate_dcc_xml.py`` di Mario scrive il DCC dai file della pipeline. Del contesto della
sessione non sa nulla, quindi qui il motore aggiunge, dove lo schema PTB 3.3.0 li prevede:

- in ``measurementResult/usedMethods`` la **procedura** del registro (id, versione, punti, regola
  decisionale), come primo metodo;
- in ``measurementResult/usedSoftware`` la **versione del motore** con l'hash git: e' il
  software che ha prodotto i numeri, e una versione diversa puo' produrne di diversi;
- in ``measurementResult/measuringEquipments`` il **campione** (che la pipeline vi scrive dal
  passo 3.6, non piu' fra gli oggetti tarati) con numero di serie, certificato, emittente e
  validita';
- in ``administrativeData/statements`` le **deviazioni dal metodo** rilevate sui dati e la
  funzione come la applica il server (forma centrata del passo 3.5), perche' chi legge il DCC
  possa riconciliarla con i coefficienti storici.

Le aggiunte rispettano l'ordine degli elementi dello schema; la validazione con l'XSD, se
configurata (``ENGINE_DCC_XSD_PATH``), si fa dopo, sul file finale. Ogni testo ha la versione
inglese, che il DCC di Mario dichiara obbligatoria (``mandatoryLangCodeISO639_1``), e quella
italiana.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

DCC = "https://ptb.de/dcc"


def _q(tag: str) -> str:
    return f"{{{DCC}}}{tag}"


def _after(parent: ET.Element, reference: ET.Element, element: ET.Element) -> None:
    """Inserisce ``element`` subito dopo ``reference`` fra i figli di ``parent``."""
    parent.insert(list(parent).index(reference) + 1, element)


def _register_namespaces(path: Path) -> None:
    """Tiene i prefissi del file (dcc, si, ...) quando lo si riscrive."""
    for _, (prefix, uri) in ET.iterparse(str(path), events=("start-ns",)):
        if prefix:
            ET.register_namespace(prefix, uri)


def _texts(parent: ET.Element, tag: str, en: str, it: str | None = None) -> ET.Element:
    """Un elemento di tipo testo (``dcc:content`` per lingua), inglese sempre, italiano se c'e'."""
    element = ET.SubElement(parent, _q(tag))
    for lang, text in (("en", en), ("it", it)):
        if text:
            content = ET.SubElement(element, _q("content"), lang=lang)
            content.text = text
    return element


def _child(parent: ET.Element, tag: str) -> ET.Element | None:
    return parent.find(_q(tag))


def _procedure_method(procedure_ref: tuple[int, str], procedure: dict[str, Any] | None) -> ET.Element:
    procedure = procedure or {}
    template_id, version = procedure_ref
    method = ET.Element(_q("usedMethod"))
    title = procedure.get("name") or f"Procedure {template_id}"
    _texts(method, "name", f"Calibration procedure {template_id} v{version}: {title}", f"Procedura di taratura {template_id} v{version}: {title}")
    details_en = [f"Procedure {template_id}, version {version}, from the template registry."]
    details_it = [f"Procedura {template_id}, versione {version}, dal registro dei template."]
    points = procedure.get("points")
    if points:
        details_en.append(f"Points: {', '.join(f'{p:g}' for p in points)}.")
        details_it.append(f"Punti: {', '.join(f'{p:g}' for p in points)}.")
    rule = procedure.get("decisionRule") or {}
    if rule.get("pfaMax") is not None:
        details_en.append(f"Decision rule: {rule.get('type', 'guarded')}, PFA <= {float(rule['pfaMax']) * 100:g} %.")
        details_it.append(f"Regola decisionale: {rule.get('type', 'guarded')}, PFA <= {float(rule['pfaMax']) * 100:g} %.")
    _texts(method, "description", " ".join(details_en), " ".join(details_it))
    return method


def _software(engine_version: str) -> ET.Element:
    used = ET.Element(_q("usedSoftware"))
    software = ET.SubElement(used, _q("software"), id="calibration-engine")
    _texts(software, "name", "calibration-engine")
    release = ET.SubElement(software, _q("release"))
    release.text = engine_version
    kind = ET.SubElement(software, _q("type"))
    kind.text = "application"
    _texts(
        software,
        "description",
        "Calibration engine: fit, GUM uncertainty budget, as-found and as-left, conformity. Release = package version + git commit.",
        "Motore di taratura: fit, budget d'incertezza GUM, as-found e as-left, conformita'. Release = versione del pacchetto + commit git.",
    )
    return used


def _equipment(result: ET.Element, after: ET.Element, reference: dict[str, Any] | None) -> ET.Element | None:
    """Il measuringEquipment del campione: quello della pipeline, o uno nuovo."""
    equipment = result.find(f"{_q('measuringEquipments')}/{_q('measuringEquipment')}[@id='reference-standard']")
    if equipment is not None or not reference:
        return equipment
    equipments = ET.Element(_q("measuringEquipments"))
    _after(result, after, equipments)
    equipment = ET.SubElement(equipments, _q("measuringEquipment"), id="reference-standard")
    _texts(equipment, "name", reference.get("name") or reference.get("model") or "Reference standard")
    return equipment


def _identification(identifications: ET.Element, issuer: str, value: str, en: str, it: str) -> None:
    for existing in identifications.findall(_q("identification")):
        if (existing.findtext(_q("value")) or "") == value:
            return
    ident = ET.SubElement(identifications, _q("identification"))
    ET.SubElement(ident, _q("issuer")).text = issuer
    ET.SubElement(ident, _q("value")).text = value
    _texts(ident, "name", en, it)


def _describe_reference(equipment: ET.Element, reference: dict[str, Any] | None, certificate: dict[str, Any] | None) -> None:
    if not reference and not certificate:
        return
    reference, certificate = reference or {}, certificate or {}
    if certificate:
        en = (
            f"Calibration certificate {certificate.get('number')} rev. {certificate.get('revision')}"
            f" issued by {certificate.get('issuer') or 'n/a'}"
            + (f" ({certificate['issuerAccreditation']})" if certificate.get("issuerAccreditation") else "")
            + (f", calibrated on {certificate['calibrationDate']}" if certificate.get("calibrationDate") else "")
            + (f", valid until {str(certificate['validUntil'])[:10]}" if certificate.get("validUntil") else "")
            + ". Readings corrected with the certificate corrections; standard uncertainty from the certificate."
        )
        it = (
            f"Certificato di taratura {certificate.get('number')} rev. {certificate.get('revision')}"
            f" emesso da {certificate.get('issuer') or 'n.d.'}"
            + (f" ({certificate['issuerAccreditation']})" if certificate.get("issuerAccreditation") else "")
            + (f", tarato il {certificate['calibrationDate']}" if certificate.get("calibrationDate") else "")
            + (f", valido fino al {str(certificate['validUntil'])[:10]}" if certificate.get("validUntil") else "")
            + ". Letture corrette con le correzioni del certificato; incertezza tipo dal certificato."
        )
        if _child(equipment, "description") is None:
            description = ET.Element(_q("description"))
            for lang, text in (("en", en), ("it", it)):
                ET.SubElement(description, _q("content"), lang=lang).text = text
            _after(equipment, _child(equipment, "name"), description)

    identifications = _child(equipment, "identifications")
    if identifications is None:
        identifications = ET.SubElement(equipment, _q("identifications"))
    if reference.get("serialNumber"):
        _identification(identifications, "manufacturer", str(reference["serialNumber"]), "Serial number", "Numero di serie")
    if certificate.get("number"):
        _identification(identifications, "calibrationLaboratory", f"{certificate['number']} rev. {certificate.get('revision')}", "Calibration certificate", "Certificato di taratura")


def _statement(root: ET.Element, en: str, it: str) -> None:
    admin = _child(root, "administrativeData")
    statements = _child(admin, "statements") if admin is not None else None
    if statements is None:
        return
    statement = ET.SubElement(statements, _q("statement"))
    _texts(statement, "declaration", en, it)


def enrich(
    path: Path,
    *,
    engine_version: str,
    procedure_ref: tuple[int, str] | None = None,
    procedure: dict[str, Any] | None = None,
    reference: dict[str, Any] | None = None,
    reference_certificate: dict[str, Any] | None = None,
    deviations: list[str] | None = None,
    delivered_function: dict[str, Any] | None = None,
) -> None:
    """Completa il DCC scritto dalla pipeline, sul posto."""
    _register_namespaces(path)
    tree = ET.parse(str(path))
    root = tree.getroot()
    result = root.find(f".//{_q('measurementResult')}")
    if result is None:
        raise ValueError("il DCC non ha measurementResult")

    methods = _child(result, "usedMethods")
    if methods is None:
        methods = ET.Element(_q("usedMethods"))
        _after(result, _child(result, "name"), methods)
    if procedure_ref is not None:
        methods.insert(0, _procedure_method(procedure_ref, procedure))

    software = _child(result, "usedSoftware")
    if software is None:
        software = _software(engine_version)
        _after(result, methods, software)

    equipment = _equipment(result, software, reference)
    if equipment is not None:
        _describe_reference(equipment, reference, reference_certificate)

    if deviations:
        _statement(
            root,
            "Deviations from the calibration procedure detected on the data: " + "; ".join(deviations) + ".",
            "Deviazioni dalla procedura di taratura rilevate sui dati: " + "; ".join(deviations) + ".",
        )
    if delivered_function:
        kind = delivered_function.get("type")
        formula = delivered_function.get(kind) if kind else None
        values = [c.get("value") for c in sorted(delivered_function.get("c") or [], key=lambda c: c.get("id", 0))]
        if formula:
            _statement(
                root,
                f"Instrument function as applied by the server: f(x) = {formula}, c = {values}. "
                "It is the same function as the coefficients above, in the centred variable of the fit.",
                f"Funzione dell'esemplare come la applica il server: f(x) = {formula}, c = {values}. "
                "E' la stessa funzione dei coefficienti sopra, nella variabile centrata del fit.",
            )

    tree.write(str(path), xml_declaration=True, encoding="UTF-8")
