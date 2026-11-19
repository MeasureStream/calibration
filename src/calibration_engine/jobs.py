"""L'esecuzione di un job (PIANO_METROLOGIA 3.4, 3.5, 3.6 e 5.3).

Un job alla volta, ciascuno in una cartella temporanea sua. La pipeline gira in un processo
figlio con un timeout: se lo supera, il processo si termina e il job risulta FAILED con il
codice ``TIMEOUT``, invece di bloccare la coda per sempre.

Ogni errore diventa un risultato FAILED con un codice leggibile: metrology-service lo mostra
nella sessione, e il tecnico sa se manca un gradino, se il modello non e' supportato o se la
pipeline e' caduta.

I tre job che toccano i dati di una sessione:

- **SESSION_FIT**: i gradini as-found, con le letture del campione corrette dal suo certificato
  e i gradini instabili esclusi secondo la procedura, vanno alla pipeline di Mario per il fit.
  L'as-found si calcola qui con la funzione in vigore sul server (``points.py``); i residui del
  fit stanno nel budget, perche' non sono un as-left;
- **VERIFICATION**: i gradini as-left, su punti indipendenti, valutati con la funzione che resta
  in vigore dopo la decisione del tecnico: la nuova se ha regolato, quella di prima se no;
- **DOCUMENTS**: DCC e PDF dalla pipeline, sugli stessi gradini preparati del fit.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from calibration_engine import __version__
from calibration_engine.adapter import AdapterError, item_result, item_steps, pipeline_input, tolerance_of
from calibration_engine.dcc import enrich
from calibration_engine.contracts import (
    CalibratedRange,
    CalibrationFunction,
    CalibrationJob,
    CalibrationResult,
    DocumentKind,
    DocumentRef,
    ItemResult,
    JobError,
    JobKind,
    JobStatus,
    Phase,
    SuggestedDecision,
    Verdict,
)
from calibration_engine.pipeline import command, pipeline_dir
from calibration_engine.points import DEFAULT_BLOCK_SIZE, Budget, Function, FunctionNotEvaluable, StepStats, conformity, point_errors, stats
from calibration_engine.procedure import ProcedureReport, check, hysteresis_pairs, merge_hysteresis
from calibration_engine.reference import ReferenceStandardError, correct_steps
from calibration_engine.settings import EngineSettings

log = logging.getLogger(__name__)

#: Il modello di fit se ne' il job ne' la procedura lo dicono: e' quello delle run di Mario.
DEFAULT_MODEL = "cubic"
#: La PFA massima se la procedura non la dichiara: quella della procedura 401.
DEFAULT_PFA_MAX = 0.02
#: Il campione delle run di Mario, quando la sessione non ne indica il modello.
DEFAULT_REFERENCE = pipeline_dir() / "models_in" / "references" / "fluke_9142.json"


class Metrology(Protocol):
    """Cio' che il motore chiede a metrology-service (e, attraverso il gateway, al registro)."""

    def job_context(self, job_id: str) -> dict[str, Any]: ...

    def template(self, kind: str, template_id: int, major: int) -> dict[str, Any] | None: ...

    def template_version(self, kind: str, template_id: int, version: str) -> dict[str, Any] | None: ...

    def upload_document(self, content: bytes, filename: str, mime: str, kind: str, owner_sub: str | None) -> dict[str, Any]: ...


class JobFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def execute(job: CalibrationJob, client: Metrology, settings: EngineSettings) -> CalibrationResult:
    started = datetime.now(UTC)
    version = f"{__version__}+{settings.git_sha}"
    items: list[ItemResult] = []
    documents: list[DocumentRef] = []
    error: JobError | None = None
    try:
        context = client.job_context(str(job.job_id))
        if job.kind == JobKind.SESSION_FIT:
            items = [_fit(job, context, item, client, settings) for item in _session_items(context)]
        elif job.kind == JobKind.VERIFICATION:
            items = [r for r in (_verify(context, item, client) for item in _session_items(context)) if r is not None]
        elif job.kind == JobKind.DOCUMENTS:
            for item in _session_items(context):
                documents.extend(_documents(job, context, item, client, settings))
        else:
            raise JobFailure("NOT_SUPPORTED", f"il motore non esegue ancora i job {job.kind} (passo 5.3)")
    except (JobFailure, AdapterError, ReferenceStandardError) as exc:
        error = JobError(code=exc.code, message=str(exc))
    except subprocess.TimeoutExpired:
        error = JobError(code="TIMEOUT", message=f"la pipeline ha superato {settings.job_timeout_seconds} s ed e' stata terminata")
    except Exception as exc:  # noqa: BLE001 - qualunque errore deve diventare un esito, non un crash del consumer
        log.exception("Job %s fallito", job.job_id)
        error = JobError(code="INTERNAL", message=f"{type(exc).__name__}: {exc}")

    return CalibrationResult(
        job_id=job.job_id,
        kind=job.kind,
        status=JobStatus.FAILED if error else JobStatus.DONE,
        engine_version=version,
        started_at=started,
        finished_at=datetime.now(UTC),
        error=error,
        items=[] if error else items,
        documents=[] if error else documents,
    )


def _session_items(context: dict[str, Any]) -> list[dict[str, Any]]:
    entries = context.get("items") or []
    if not entries:
        raise JobFailure("NO_ITEMS", "la sessione non ha esemplari")
    return [{**e["item"], "currentCertificate": e.get("currentCertificate"), "latestFit": e.get("latestFit")} for e in entries]


def _model(job: CalibrationJob, procedure: dict[str, Any] | None) -> str:
    return str(job.params.get("model") or (procedure or {}).get("fitModel") or DEFAULT_MODEL)


# --------------------------------------------------------------------------------------------
# I documenti del registro
# --------------------------------------------------------------------------------------------


def _sensor_template(item: dict[str, Any], client: Metrology) -> dict[str, Any]:
    sensor = None
    if item.get("sensorTemplateId") is not None and item.get("sensorTemplateMajor") is not None:
        sensor = client.template("sensor", item["sensorTemplateId"], item["sensorTemplateMajor"])
    if sensor is None:
        raise JobFailure("NO_TEMPLATE", f"template del sensore {item.get('modelName')} non trovato nel registro")
    return sensor


def _reference_template(context: dict[str, Any], client: Metrology) -> dict[str, Any] | None:
    reference = context.get("reference") or {}
    if reference.get("modelTemplateId") is None or reference.get("modelTemplateMajor") is None:
        return None
    return client.template("reference", reference["modelTemplateId"], reference["modelTemplateMajor"])


def _procedure(context: dict[str, Any], client: Metrology) -> dict[str, Any] | None:
    procedure_ref = (context.get("session") or {}).get("procedure") or {}
    if procedure_ref.get("templateId") is None:
        return None
    return client.template_version("procedure", procedure_ref["templateId"], procedure_ref["version"])


def _template_ref(sensor: dict[str, Any] | None, item: dict[str, Any]) -> tuple[int, str] | None:
    template_id = (sensor or {}).get("templateId") or item.get("sensorTemplateId")
    version = (sensor or {}).get("templateVersion") or f"{item.get('sensorTemplateMajor')}.x"
    return (int(template_id), str(version)) if template_id is not None else None


def _block_size(procedure: dict[str, Any] | None) -> int:
    value = ((procedure or {}).get("readings") or {}).get("blockSize")
    return int(value) if isinstance(value, int) and value > 0 else DEFAULT_BLOCK_SIZE


def _pfa_max(procedure: dict[str, Any] | None) -> float:
    value = ((procedure or {}).get("decisionRule") or {}).get("pfaMax")
    return float(value) if isinstance(value, int | float) else DEFAULT_PFA_MAX


def _nameplate_u(reference_model: dict[str, Any]) -> float:
    """L'incertezza tipo di targa del campione, come la legge la pipeline di Mario."""
    entries = (reference_model.get("metrology") or {}).get("Uncertainty") or []
    return float(entries[0].get("ub", 0.0)) if entries and isinstance(entries[0], dict) else 0.0


def _resolution_u(reference_model: dict[str, Any]) -> float:
    """La risoluzione del campione come incertezza tipo; 0,001/√12 come Mario se non dichiarata."""
    for entry in (reference_model.get("metrology") or {}).get("readingUncertainty") or []:
        if isinstance(entry, dict) and entry.get("varName") == "resolution" and isinstance(entry.get("value"), int | float):
            return float(entry["value"]) / math.sqrt(12.0)
    return 0.001 / math.sqrt(12.0)


# --------------------------------------------------------------------------------------------
# I gradini preparati: campione corretto, procedura controllata
# --------------------------------------------------------------------------------------------


@dataclass
class Prepared:
    """I gradini di una fase per un esemplare, pronti per il fit o per la verifica."""

    steps: list[dict[str, Any]]
    all_steps: list[dict[str, Any]]
    report: ProcedureReport
    reference: dict[str, Any]
    reference_model: dict[str, Any]
    #: L'incertezza tipo del campione per la pipeline: certificato (il massimo sui gradini) o
    #: targa, con i contributi del metodo in quadratura.
    u_reference_pipeline: float
    #: Letture per blocco nelle statistiche dei gradini (``readings.blockSize`` della procedura).
    block_size: int = DEFAULT_BLOCK_SIZE

    def stats(self) -> list[StepStats]:
        return stats(self.steps, self.block_size)

    def budget(self) -> Budget:
        return Budget(
            u_reference_default=_nameplate_u(self.reference_model),
            u_method=self.report.u_method,
            u_resolution_reference=_resolution_u(self.reference_model),
        )


def _prepare(context: dict[str, Any], item: dict[str, Any], procedure: dict[str, Any] | None, reference_model: dict[str, Any] | None, phase: str) -> Prepared:
    steps = item_steps(context, item, phase)
    if not steps:
        raise AdapterError("NO_STEPS", f"nessun gradino {phase} per il sensore {item['sensorIndex']}")
    model = reference_model or json.loads(DEFAULT_REFERENCE.read_text(encoding="utf-8"))
    corrected, reference = correct_steps(steps, context.get("referenceCertificate"))
    report = check(procedure, corrected, phase)
    kept = [s for s in corrected if s["stepIndex"] not in report.excluded]
    if report.hysteresis:
        kept = merge_hysteresis(kept, report.steps)

    from_certificate = [s["referenceU"] for s in corrected if s.get("referenceU") is not None]
    u_reference = max(from_certificate) if from_certificate else _nameplate_u(model)
    reference = {**reference, "uReferenceForFit": u_reference, "uMethod": report.u_method}
    return Prepared(
        steps=kept,
        all_steps=corrected,
        report=report,
        reference=reference,
        reference_model=model,
        u_reference_pipeline=math.hypot(u_reference, report.u_method),
        block_size=_block_size(procedure),
    )


def _raw_domain_warning(sensor: dict[str, Any] | None, steps: list[dict[str, Any]]) -> str | None:
    """I campioni devono stare nel dominio grezzo del report, o la funzione tarata converte male."""
    elec = ((sensor or {}).get("ranges") or {}).get("elec") or {}
    low, high = elec.get("min"), elec.get("max")
    samples = [float(v) for s in steps for v in s.get("sensorSamples") or []]
    if not samples or (low is None and high is None):
        return None
    if (high is not None and max(samples) > float(high)) or (low is not None and min(samples) < float(low)):
        return (
            f"campioni del sensore in [{min(samples):g}, {max(samples):g}], fuori dal campo elettrico del template "
            f"[{low}, {high}]: la funzione tarata sarebbe applicata a report in un altro dominio"
        )
    return None


# --------------------------------------------------------------------------------------------
# La pipeline
# --------------------------------------------------------------------------------------------


def _inputs(
    job: CalibrationJob,
    context: dict[str, Any],
    item: dict[str, Any],
    workdir: Path,
    sensor: dict[str, Any],
    procedure: dict[str, Any] | None,
    prepared: Prepared,
    update: str = "always",
) -> list[str]:
    """Scrive i file d'ingresso della pipeline e restituisce gli argomenti.

    ``update`` e' la ``--update-parameters`` di Mario: ``always`` per il fit e per i documenti
    di una regolazione, ``none`` per i documenti di una taratura senza regolazione, cosi' il
    DCC e il PDF dicono che i coefficienti non sono cambiati.
    """
    (workdir / "input.json").write_text(json.dumps(pipeline_input(context, item, steps=prepared.steps)), encoding="utf-8")
    (workdir / "sensor.json").write_text(json.dumps(sensor), encoding="utf-8")

    # Il campione: il modello del registro (o quello di Mario), con l'incertezza e il
    # certificato veri al posto di quelli scritti a mano nel file (M2).
    model = json.loads(json.dumps(prepared.reference_model))
    if prepared.reference.get("fromCertificate") or prepared.report.u_method > 0:
        u = prepared.u_reference_pipeline
        model.setdefault("metrology", {})["Uncertainty"] = [{"referenceValue": 0, "absUncertainty": 2.0 * u, "ub": u, "k": 2.0}]
    certificate = prepared.reference.get("certificate") or {}
    if certificate:
        model["calibrationCertificateID"] = f"{certificate.get('number')} rev. {certificate.get('revision')}"
        model["issuedBy"] = certificate.get("issuer") or model.get("issuedBy", "")
    (workdir / "reference.json").write_text(json.dumps(model), encoding="utf-8")

    args = [
        "--input", str(workdir / "input.json"),
        "--sensor", str(workdir / "sensor.json"),
        "--ref", str(workdir / "reference.json"),
        "--procedure", _model(job, procedure),
        "--update-parameters", update,
        "--cert-output", str(workdir / "certificato.json"),
        "--pdf", str(workdir / "certificato.pdf"),
        "--xml", str(workdir / "dcc.xml"),
        "--result-calibration", str(workdir / "result.json"),
        "--conformity-output", str(workdir / "conformity.json"),
        "--images-dir", str(workdir / "images"),
        "--sample-size", str(prepared.block_size),
        "--no-charts",
        "--no-verbose",
    ]
    # Tolleranza e PFA dal template e dalla procedura invece dei valori fissi 0,30 e 20 % (C7):
    # la pipeline li accetta gia' come argomenti, quindi non si tocca il suo codice.
    tolerance = tolerance_of(sensor)
    if tolerance is not None:
        args += ["--tolerance", str(tolerance), "--mae-y", str(tolerance)]
    pfa_max = ((procedure or {}).get("decisionRule") or {}).get("pfaMax")
    if pfa_max is not None:
        args += ["--pfa-threshold-pct", str(float(pfa_max) * 100)]
    return args


def _run(args: list[str], settings: EngineSettings, workdir: Path) -> None:
    completed = subprocess.run(
        command(*args),
        cwd=pipeline_dir(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=settings.job_timeout_seconds,
    )
    (workdir / "pipeline.log").write_text(completed.stdout + "\n" + completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout)[-1500:]
        raise JobFailure("PIPELINE_ERROR", f"la pipeline e' uscita con {completed.returncode}: {tail}")


# --------------------------------------------------------------------------------------------
# SESSION_FIT
# --------------------------------------------------------------------------------------------


def function_in_force(item: dict[str, Any], sensor: dict[str, Any] | None) -> tuple[Function | None, str]:
    """La funzione che il server applica oggi all'esemplare, e da dove viene."""
    certificate = item.get("currentCertificate") or {}
    function = Function.of(certificate.get("function"))
    if function is not None:
        return function, f"certificato {certificate.get('number')} rev. {certificate.get('revision')}"
    function = Function.of((sensor or {}).get("calibration"))
    if function is not None:
        return function, "formula nominale del template"
    return None, "nessuna"


def _fit(job: CalibrationJob, context: dict[str, Any], item: dict[str, Any], client: Metrology, settings: EngineSettings) -> ItemResult:
    sensor = _sensor_template(item, client)
    procedure = _procedure(context, client)
    prepared = _prepare(context, item, procedure, _reference_template(context, client), "AS_FOUND")
    with tempfile.TemporaryDirectory(prefix=f"job-{job.job_id}-") as tmp:
        workdir = Path(tmp)
        args = _inputs(job, context, item, workdir, sensor, procedure, prepared)
        _run([*args, "--no-pdf", "--no-xml"], settings, workdir)
        result = json.loads((workdir / "result.json").read_text(encoding="utf-8"))
        conformity_path = workdir / "conformity.json"
        pipeline_conformity = json.loads(conformity_path.read_text(encoding="utf-8")) if conformity_path.is_file() else None

    template_ref = _template_ref(sensor, item)
    translated = item_result(result, pipeline_conformity, item, sensor, template_ref)
    warnings = list(prepared.report.warnings)
    domain = _raw_domain_warning(sensor, prepared.steps)
    if domain:
        warnings.append(domain)

    budget = {
        **translated.budget,
        "referenceStandard": prepared.reference,
        "procedure": prepared.report.to_wire(),
    }
    points, verdicts, suggested = translated.points, translated.conformity, translated.suggested_decision

    function, source = function_in_force(item, sensor)
    if function is not None:
        try:
            found = point_errors(function, prepared.stats(), prepared.budget(), Phase.AS_FOUND, sensor.get("unit"))
            verdict, detail = conformity(found, sensor, template_ref, _pfa_max(procedure))
            points, verdicts = found, ([verdict] if verdict else [])
            suggested = SuggestedDecision.ADJUST if verdict and verdict.verdict != Verdict.CONFORM else SuggestedDecision.NONE
            budget["asFoundFrom"] = source
            budget["asFoundConformity"] = detail
        except FunctionNotEvaluable as exc:
            warnings.append(f"as-found con la funzione in vigore ({source}) non calcolabile: {exc} Si usa quello della pipeline.")
    else:
        warnings.append("nessuna funzione in vigore per l'esemplare: as-found dalla pipeline")

    if prepared.report.hysteresis:
        points, budget["hysteresis"] = _with_hysteresis(points, translated, prepared)

    budget["warnings"] = warnings
    return translated.model_copy(update={"points": points, "conformity": verdicts, "suggested_decision": suggested, "budget": budget})


def _with_hysteresis(points: list, translated: ItemResult, prepared: Prepared) -> tuple[list, list[dict[str, Any]]]:
    """L'isteresi di ogni punto misurato in salita e in discesa, con la funzione nuova.

    h = |e_salita − e_discesa| e' l'ampiezza piena di una distribuzione rettangolare, quindi
    u = h/√12; si somma in quadratura all'incertezza dei punti dello stesso valore nominale.
    """
    function = Function.of(translated.function.model_dump(mode="json")) if translated.function else None
    if function is None:
        return points, []
    usable = [s for s in prepared.all_steps if s["stepIndex"] not in prepared.report.excluded]
    pairs = hysteresis_pairs(usable, prepared.report.steps)
    found: dict[float, float] = {}
    out: list[dict[str, Any]] = []
    for setpoint, (up, down) in pairs.items():
        stats_up, stats_down = stats([up], prepared.block_size)[0], stats([down], prepared.block_size)[0]
        f_up, f_down = function(stats_up.raw_mean), function(stats_down.raw_mean)
        if f_up is None or f_down is None:
            continue
        h = abs((f_up - stats_up.ref_mean) - (f_down - stats_down.ref_mean))
        found[setpoint] = h / math.sqrt(12.0)
        out.append({"setpoint": setpoint, "h": h, "u": found[setpoint]})
    updated = []
    for p in points:
        u_h = found.get(float(p.nominal)) if p.nominal is not None else None
        if u_h and p.expanded_u is not None:
            k = p.k or 2.0
            p = p.model_copy(update={"expanded_u": k * math.hypot(p.expanded_u / k, u_h)})
        updated.append(p)
    return updated, out


# --------------------------------------------------------------------------------------------
# VERIFICATION
# --------------------------------------------------------------------------------------------


def _clean(block: dict[str, Any] | None) -> dict[str, Any] | None:
    """Del blocco ``calibration`` solo cio' che serve alla funzione (tipo, formula, input, c)."""
    if not block or not block.get("type") or block.get("type") == "none":
        return None
    kind = block["type"]
    declared = block.get("c")
    if not isinstance(declared, list) or not declared:
        declared = [{"id": i, "value": v} for i, v in enumerate(block.get("coefficients") or [])]
    return {"type": kind, kind: block.get(kind), "input": block.get("input") or ["x"], "c": [{"id": e.get("id"), "value": e.get("value"), "dsi": e.get("dsi")} for e in declared]}


def _verify(context: dict[str, Any], item: dict[str, Any], client: Metrology) -> ItemResult | None:
    """L'as-left di un esemplare sui gradini indipendenti della fase AS_LEFT."""
    latest = item.get("latestFit") or {}
    decision = latest.get("decision")
    if decision in ("REPAIR", "RETIRE"):
        return None
    if not item_steps(context, item, "AS_LEFT"):
        return None
    if decision is None:
        raise JobFailure("NO_DECISION", f"sensore {item['sensorIndex']}: l'as-left si verifica dopo la decisione di regolazione")

    sensor = _sensor_template(item, client)
    procedure = _procedure(context, client)
    prepared = _prepare(context, item, procedure, _reference_template(context, client), "AS_LEFT")

    if decision == "ADJUST":
        block, covariance, source = latest.get("function"), latest.get("covariance"), "funzione nuova (regolazione)"
    else:
        certificate = item.get("currentCertificate") or {}
        if certificate.get("function"):
            block, covariance = certificate.get("function"), certificate.get("covariance")
            source = f"certificato {certificate.get('number')} rev. {certificate.get('revision')} (nessuna regolazione)"
        else:
            block, covariance, source = sensor.get("calibration"), None, "formula nominale del template (nessuna regolazione)"
    block = _clean(block)
    function = Function.of(block)
    if function is None:
        raise JobFailure("NO_FUNCTION", f"sensore {item['sensorIndex']}: nessuna funzione da verificare ({source})")

    try:
        left = point_errors(function, prepared.stats(), prepared.budget(), Phase.AS_LEFT, sensor.get("unit"))
    except FunctionNotEvaluable as exc:
        raise JobFailure("FUNCTION_NOT_EVALUABLE", str(exc)) from exc
    template_ref = _template_ref(sensor, item)
    verdict, detail = conformity(left, sensor, template_ref, _pfa_max(procedure))
    warnings = list(prepared.report.warnings)
    domain = _raw_domain_warning(sensor, prepared.steps)
    if domain:
        warnings.append(domain)

    calibrated_range = None
    if decision == "ADJUST" and latest.get("calibratedMin") is not None and latest.get("calibratedMax") is not None:
        calibrated_range = CalibratedRange(min=latest["calibratedMin"], max=latest["calibratedMax"], dsi=latest.get("calibratedDsi"))
    if covariance is not None and (len(covariance) != len(block["c"]) or any(len(row) != len(block["c"]) for row in covariance)):
        covariance = None
    return ItemResult(
        calibrated_item_id=item["id"],
        mu_extended_id=item["muExtendedId"],
        sensor_index=item["sensorIndex"],
        function=CalibrationFunction.model_validate(block),
        covariance=covariance,
        calibrated_range=calibrated_range,
        adjustment_applied=decision == "ADJUST",
        points=left,
        budget={
            "verifiedFunction": source,
            "decision": decision,
            "referenceStandard": prepared.reference,
            "procedure": prepared.report.to_wire(),
            "asLeftConformity": detail,
            "warnings": warnings,
        },
        conformity=[verdict] if verdict else [],
        # Un as-left fuori specifica dopo la decisione e' da capire, non da regolare di nuovo.
        suggested_decision=SuggestedDecision.INVESTIGATE if verdict and verdict.verdict != Verdict.CONFORM else SuggestedDecision.NONE,
    )


# --------------------------------------------------------------------------------------------
# DOCUMENTS
# --------------------------------------------------------------------------------------------


def _documents(job: CalibrationJob, context: dict[str, Any], item: dict[str, Any], client: Metrology, settings: EngineSettings) -> list[DocumentRef]:
    """DCC XML e PDF per un esemplare, caricati su metrology-service a nome del proprietario."""
    decision = (item.get("latestFit") or {}).get("decision")
    if decision in ("REPAIR", "RETIRE"):
        # Nessun certificato per uno strumento da riparare o da ritirare (metrology-service).
        return []
    sensor = _sensor_template(item, client)
    procedure = _procedure(context, client)
    prepared = _prepare(context, item, procedure, _reference_template(context, client), "AS_FOUND")
    with tempfile.TemporaryDirectory(prefix=f"job-{job.job_id}-") as tmp:
        workdir = Path(tmp)
        args = _inputs(job, context, item, workdir, sensor, procedure, prepared, update="always" if decision in (None, "ADJUST") else "none")
        _run(args, settings, workdir)

        refs: list[DocumentRef] = []
        xml = workdir / "dcc.xml"
        if xml.is_file():
            procedure_ref = (context.get("session") or {}).get("procedure") or {}
            enrich(
                xml,
                engine_version=f"{__version__}+{settings.git_sha}",
                procedure_ref=(int(procedure_ref["templateId"]), str(procedure_ref.get("version"))) if procedure_ref.get("templateId") is not None else None,
                procedure=procedure,
                reference=context.get("reference"),
                reference_certificate=context.get("referenceCertificate"),
                deviations=prepared.report.deviations,
                delivered_function=(item.get("latestFit") or {}).get("function") if decision == "ADJUST" else None,
            )
            _validate(xml, settings)
            refs.append(_upload(client, xml, "application/xml", DocumentKind.DCC_XML, item))
        pdf = workdir / "certificato.pdf"
        if pdf.is_file():
            refs.append(_upload(client, pdf, "application/pdf", DocumentKind.CERTIFICATE_PDF, item))
        if not refs:
            raise JobFailure("NO_DOCUMENTS", "la pipeline non ha prodotto ne' il DCC ne' il PDF")
        return refs


def _upload(client: Metrology, path: Path, mime: str, kind: DocumentKind, item: dict[str, Any]) -> DocumentRef:
    name = f"{kind.value.lower()}-0x{int(item['muExtendedId']):08X}-{item['sensorIndex']}{path.suffix}"
    body = client.upload_document(path.read_bytes(), name, mime, kind.value, item.get("ownerSub"))
    return DocumentRef(document_id=body["id"], kind=kind, sha256=body["sha256"], calibrated_item_id=item["id"])


def _validate(xml: Path, settings: EngineSettings) -> None:
    """Valida il DCC contro l'XSD PTB, se configurato (passo 3.6). Un DCC non valido non parte."""
    if not settings.dcc_xsd_path:
        log.warning("XSD del DCC non configurato (ENGINE_DCC_XSD_PATH): il DCC non e' validato")
        return
    import xmlschema

    schema = xmlschema.XMLSchema(settings.dcc_xsd_path)
    errors = [str(e) for e in schema.iter_errors(str(xml))][:5]
    if errors:
        raise JobFailure("DCC_INVALID", "il DCC non rispetta l'XSD: " + " | ".join(errors))
