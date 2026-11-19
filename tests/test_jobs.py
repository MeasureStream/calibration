"""Il motore come consumer: dai gradini di una sessione al risultato nel contratto.

Il caso di prova e' la run 5 di Mario (fit cubico sul payload simulato a 13 punti): la sessione
si costruisce a ritroso dal suo file d'ingresso, come se i gradini fossero arrivati dal
Raspberry, e il job SESSION_FIT deve restituire gli stessi coefficienti e la stessa covarianza
che Mario otteneva dalla riga di comando (prova del passo 3.4).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from calibration_engine.adapter import AdapterError, item_result, pipeline_input
from calibration_engine.consumer import handle, healthy, touch
from calibration_engine.contracts import CalibrationJob, CalibrationResult, JobKind, JobStatus, SuggestedDecision, Verdict
from calibration_engine.jobs import execute
from calibration_engine.pipeline import pipeline_dir
from calibration_engine.settings import EngineSettings

PIPELINE = pipeline_dir()
RUN5 = PIPELINE / "test_runs" / "run5_cubic_13p"
SIMULATED = PIPELINE / "data_in" / "simulated_13p.json"
ITEM = {"id": 12, "muExtendedId": 65537, "sensorIndex": 4, "sensorTemplateId": 201, "sensorTemplateMajor": 2, "ownerSub": "cliente", "modelName": "TemperatureSensorNTC"}


def _steps_from_mario(path: Path) -> list[dict[str, Any]]:
    """Il file d'ingresso di Mario riportato ai gradini che il Raspberry pubblica."""
    data = json.loads(path.read_text(encoding="utf-8"))
    steps = []
    for index, label in enumerate(data["steps"]):
        target, minutes = label.strip("()").split(",")
        readings = [r["reading"] for r in data["reference_temperature_samples"] if r["index_step"] == index]
        samples = [v for s in data["sensor_raw_samples"] if s["index_step"] == index for v in s["value"]]
        steps.append(
            {
                "phase": "AS_FOUND",
                "sensorIndex": ITEM["sensorIndex"],
                "stepIndex": index,
                "setpoint": float(target),
                "minutes": int(minutes),
                "startedAt": data["start_time"][index],
                "dwellStartedAt": data["start_time_dwell"][index],
                "sensorSamplingFreq": data["sensor_sampling_freq"][index],
                "referenceReadings": readings,
                "sensorSamples": samples,
            }
        )
    return steps


def _context(steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "session": {"id": 42, "muExtendedId": ITEM["muExtendedId"], "procedure": {"templateId": 401, "version": "1.0.0"}},
        "items": [{"item": ITEM, "currentCertificate": None}],
        "steps": _steps_from_mario(SIMULATED) if steps is None else steps,
        "reference": {"id": 3, "serialNumber": "B12345", "modelTemplateId": 301, "modelTemplateMajor": 1},
        "referenceCertificate": None,
    }


class FakeMetrology:
    """metrology-service e il registro, serviti dai file del repository."""

    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context
        self.uploads: list[tuple[str, str, str | None, int]] = []

    def job_context(self, job_id: str) -> dict[str, Any]:
        return self.context

    def template(self, kind: str, template_id: int, major: int) -> dict[str, Any] | None:
        files = {("sensor", 201): "models_in/sensors/ntc_temperature.json", ("reference", 301): "models_in/references/fluke_9142.json"}
        path = files.get((kind, template_id))
        return json.loads((PIPELINE / path).read_text(encoding="utf-8")) if path else None

    def template_version(self, kind: str, template_id: int, version: str) -> dict[str, Any] | None:
        # Come la procedura 401 pubblicata: fit cubico.
        return {"kind": "procedure", "templateId": 401, "fitModel": "cubic"} if kind == "procedure" else None

    def upload_document(self, content: bytes, filename: str, mime: str, kind: str, owner_sub: str | None) -> dict[str, Any]:
        self.uploads.append((filename, kind, owner_sub, len(content)))
        import hashlib

        return {"id": len(self.uploads), "sha256": hashlib.sha256(content).hexdigest()}


def _job(kind: JobKind) -> CalibrationJob:
    return CalibrationJob(job_id=uuid.uuid4(), kind=kind, session_id=42, requested_by="anna", requested_at=datetime.now(UTC))


def _settings(tmp_path: Path) -> EngineSettings:
    return EngineSettings(heartbeat_file=str(tmp_path / "hb"), job_timeout_seconds=300, git_sha="test")


def test_i_gradini_ricostruiscono_l_ingresso_di_mario():
    original = json.loads(SIMULATED.read_text(encoding="utf-8"))
    rebuilt = pipeline_input(_context(), ITEM)

    assert rebuilt["steps"] == original["steps"]
    assert [r["reading"] for r in rebuilt["reference_temperature_samples"]] == [r["reading"] for r in original["reference_temperature_samples"]]
    assert [s["value"] for s in rebuilt["sensor_raw_samples"]] == [s["value"] for s in original["sensor_raw_samples"]]


def test_le_uscite_di_mario_nel_contratto():
    result = json.loads((RUN5 / "last_calibration.json").read_text(encoding="utf-8"))
    conformity = json.loads((RUN5 / "conformity_results.json").read_text(encoding="utf-8"))
    item = item_result(result, conformity, ITEM, {"unit": "\\degreeCelsius"}, (201, "2.0.1"))

    assert item.function.type == "cubic_lsb"
    assert [c.value for c in item.function.c] == [result["coefficients"][f"a{i}"] for i in range(4)]
    assert item.covariance == result["coefficients"]["cov_theta"]
    assert item.calibrated_range.min == -20.0
    assert item.conformity[0].verdict == Verdict.NON_CONFORM
    assert item.suggested_decision == SuggestedDecision.ADJUST, "l'as-found e' fuori tolleranza: si regola"
    assert {p.phase.value for p in item.points} == {"AS_FOUND", "AS_LEFT"}


def test_session_fit_riproduce_la_run_di_mario(tmp_path: Path):
    expected = json.loads((RUN5 / "last_calibration.json").read_text(encoding="utf-8"))["coefficients"]
    result = execute(_job(JobKind.SESSION_FIT), FakeMetrology(_context()), _settings(tmp_path))

    assert result.status == JobStatus.DONE, result.error
    function = result.items[0].function
    for i, coefficient in enumerate(function.c):
        assert coefficient.value == pytest.approx(expected[f"a{i}"], rel=1e-9)
    for row, expected_row in zip(result.items[0].covariance, expected["cov_theta"], strict=True):
        assert row == pytest.approx(expected_row, rel=1e-9)
    # Il messaggio rispetta il contratto: si rilegge uguale.
    assert CalibrationResult.model_validate_json(result.to_json()) == result


def test_una_sessione_senza_gradini_e_un_errore_leggibile(tmp_path: Path):
    result = execute(_job(JobKind.SESSION_FIT), FakeMetrology(_context(steps=[])), _settings(tmp_path))
    assert result.status == JobStatus.FAILED
    assert result.error.code == "NO_STEPS"


def test_un_tipo_di_job_non_ancora_supportato(tmp_path: Path):
    result = execute(_job(JobKind.VERIFICATION), FakeMetrology(_context()), _settings(tmp_path))
    assert result.error.code == "NOT_SUPPORTED"


def test_documents_carica_dcc_e_pdf_a_nome_del_proprietario(tmp_path: Path):
    metrology = FakeMetrology(_context())
    result = execute(_job(JobKind.DOCUMENTS), metrology, _settings(tmp_path))

    assert result.status == JobStatus.DONE, result.error
    assert {d.kind.value for d in result.documents} == {"DCC_XML", "CERTIFICATE_PDF"}
    assert all(d.calibrated_item_id == ITEM["id"] for d in result.documents)
    assert all(owner == "cliente" for _, _, owner, _ in metrology.uploads)


def test_un_job_illeggibile_si_scarta_senza_fermare_la_coda(tmp_path: Path):
    assert handle(b"{non json", FakeMetrology(_context()), _settings(tmp_path)) is None


def test_healthcheck(tmp_path: Path):
    settings = _settings(tmp_path)
    assert not healthy(settings), "senza battito il consumer non e' vivo"
    touch(settings)
    assert healthy(settings)
    assert not healthy(settings, now=Path(settings.heartbeat_file).stat().st_mtime + 10_000)


def test_adattatore_rifiuta_un_modello_senza_formula():
    with pytest.raises(AdapterError):
        item_result({"model": "steinhart", "coefficients": {}}, None, ITEM, None, None)


def test_simulate_session_indirizza_i_gradini_registrati_a_una_sessione_vera():
    from calibration_engine.simulate import default_file, load, rewrite

    steps = [rewrite(m, 42, "AS_FOUND", 0) for m in load(default_file())]
    assert {s["calib_id"] for s in steps} == {"42"}
    assert all(s["phase"] == "AS_FOUND" and s["sensor_index"] == 0 for s in steps)
    first = steps[0]
    assert isinstance(first["step_summary"], list) and isinstance(first["ref_readings"], list)
    assert isinstance(first["target"], float) and isinstance(first["step_index"], int)
