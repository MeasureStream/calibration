"""L'esecuzione di un job (PIANO_METROLOGIA 3.4 e 3.6).

Un job alla volta, ciascuno in una cartella temporanea sua. La pipeline gira in un processo
figlio con un timeout: se lo supera, il processo si termina e il job risulta FAILED con il
codice ``TIMEOUT``, invece di bloccare la coda per sempre.

Ogni errore diventa un risultato FAILED con un codice leggibile: metrology-service lo mostra
nella sessione, e il tecnico sa se manca un gradino, se il modello non e' supportato o se la
pipeline e' caduta.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from calibration_engine import __version__
from calibration_engine.adapter import AdapterError, item_result, pipeline_input, tolerance_of
from calibration_engine.contracts import (
    CalibrationJob,
    CalibrationResult,
    DocumentKind,
    DocumentRef,
    ItemResult,
    JobError,
    JobKind,
    JobStatus,
)
from calibration_engine.pipeline import command, pipeline_dir
from calibration_engine.settings import EngineSettings

log = logging.getLogger(__name__)

#: Il modello di fit se ne' il job ne' la procedura lo dicono: e' quello delle run di Mario.
DEFAULT_MODEL = "cubic"


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
        elif job.kind == JobKind.DOCUMENTS:
            for item in _session_items(context):
                documents.extend(_documents(job, context, item, client, settings))
        else:
            raise JobFailure("NOT_SUPPORTED", f"il motore non esegue ancora i job {job.kind} (passi 3.5 e 5.3)")
    except (JobFailure, AdapterError) as exc:
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
    return [{**e["item"], "currentCertificate": e.get("currentCertificate")} for e in entries]


def _model(job: CalibrationJob, procedure: dict[str, Any] | None) -> str:
    return str(job.params.get("model") or (procedure or {}).get("fitModel") or DEFAULT_MODEL)


def _inputs(job: CalibrationJob, context: dict[str, Any], item: dict[str, Any], client: Metrology, workdir: Path) -> tuple[list[str], dict[str, Any] | None, dict[str, Any] | None]:
    """Scrive i file d'ingresso della pipeline e restituisce gli argomenti, il template e la procedura."""
    (workdir / "input.json").write_text(json.dumps(pipeline_input(context, item)), encoding="utf-8")

    sensor = None
    if item.get("sensorTemplateId") is not None and item.get("sensorTemplateMajor") is not None:
        sensor = client.template("sensor", item["sensorTemplateId"], item["sensorTemplateMajor"])
    if sensor is None:
        raise JobFailure("NO_TEMPLATE", f"template del sensore {item.get('modelName')} non trovato nel registro")
    (workdir / "sensor.json").write_text(json.dumps(sensor), encoding="utf-8")

    reference = context.get("reference") or {}
    ref_template = None
    if reference.get("modelTemplateId") is not None and reference.get("modelTemplateMajor") is not None:
        ref_template = client.template("reference", reference["modelTemplateId"], reference["modelTemplateMajor"])
    ref_path = workdir / "reference.json"
    if ref_template is not None:
        ref_path.write_text(json.dumps(ref_template), encoding="utf-8")
    else:
        # Il modello del campione serve alla pipeline per le sue incertezze di targa. Senza, si
        # usa quello del pozzetto delle run di Mario, e il budget lo dichiara.
        ref_path = pipeline_dir() / "models_in" / "references" / "fluke_9142.json"

    procedure_ref = (context.get("session") or {}).get("procedure") or {}
    procedure = None
    if procedure_ref.get("templateId") is not None:
        procedure = client.template_version("procedure", procedure_ref["templateId"], procedure_ref["version"])

    args = [
        "--input", str(workdir / "input.json"),
        "--sensor", str(workdir / "sensor.json"),
        "--ref", str(ref_path),
        "--procedure", _model(job, procedure),
        "--update-parameters", "always",
        "--cert-output", str(workdir / "certificato.json"),
        "--pdf", str(workdir / "certificato.pdf"),
        "--xml", str(workdir / "dcc.xml"),
        "--result-calibration", str(workdir / "result.json"),
        "--conformity-output", str(workdir / "conformity.json"),
        "--images-dir", str(workdir / "images"),
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
    return args, sensor, procedure


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


def _fit(job: CalibrationJob, context: dict[str, Any], item: dict[str, Any], client: Metrology, settings: EngineSettings) -> ItemResult:
    with tempfile.TemporaryDirectory(prefix=f"job-{job.job_id}-") as tmp:
        workdir = Path(tmp)
        args, sensor, _ = _inputs(job, context, item, client, workdir)
        _run([*args, "--no-pdf", "--no-xml"], settings, workdir)
        result = json.loads((workdir / "result.json").read_text(encoding="utf-8"))
        conformity_path = workdir / "conformity.json"
        conformity = json.loads(conformity_path.read_text(encoding="utf-8")) if conformity_path.is_file() else None
        template_id = (sensor or {}).get("templateId") or item.get("sensorTemplateId")
        version = (sensor or {}).get("templateVersion") or f"{item.get('sensorTemplateMajor')}.x"
        return item_result(result, conformity, item, sensor, (int(template_id), str(version)) if template_id is not None else None)


def _documents(job: CalibrationJob, context: dict[str, Any], item: dict[str, Any], client: Metrology, settings: EngineSettings) -> list[DocumentRef]:
    """DCC XML e PDF per un esemplare, caricati su metrology-service a nome del proprietario."""
    with tempfile.TemporaryDirectory(prefix=f"job-{job.job_id}-") as tmp:
        workdir = Path(tmp)
        args, _, _ = _inputs(job, context, item, client, workdir)
        _run(args, settings, workdir)

        refs: list[DocumentRef] = []
        xml = workdir / "dcc.xml"
        if xml.is_file():
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
