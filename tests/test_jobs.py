"""Il motore come consumer: dai gradini di una sessione al risultato nel contratto.

Il caso di prova e' la run 5 di Mario (fit cubico sul payload simulato a 13 punti): la sessione
si costruisce a ritroso dal suo file d'ingresso, come se i gradini fossero arrivati dal
Raspberry, e il job SESSION_FIT deve restituire gli stessi coefficienti e la stessa covarianza
che Mario otteneva dalla riga di comando (prova del passo 3.4).
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from calibration_engine import __version__
from calibration_engine.adapter import AdapterError, item_result, pipeline_input
from calibration_engine.consumer import handle, healthy, touch
from calibration_engine.contracts import CalibrationJob, CalibrationResult, JobKind, JobStatus, SuggestedDecision, Verdict
from calibration_engine.formula import evaluate, standard_uncertainty
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
        self.contents: dict[str, bytes] = {}

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
        self.contents[kind] = content
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
    # Passo 3.5: i residui del fit non sono un as-left (stessi punti del fit): stanno nel budget.
    # L'as-left si misura su punti indipendenti, nel job VERIFICATION.
    assert {p.phase.value for p in item.points} == {"AS_FOUND"}
    assert [r["residual"] for r in item.budget["fitResiduals"]] == [p["M_e_post"] for p in result["calibration_points"]]


def test_session_fit_riproduce_la_run_di_mario(tmp_path: Path):
    """La funzione consegnata e' quella della run di Mario, nella forma centrata del passo 3.5.

    Fino al passo 3.4 si confrontavano i coefficienti a0..a3 uno per uno. Dal passo 3.5 il
    motore consegna il polinomio in s = (D - centro) / semiampiezza, con la covarianza in s
    (vedi poly_gum.py e adapter.function_of): stessa funzione, altri coefficienti. Si confronta
    quindi cio' che conta, sui valori del grezzo che la run ha toccato: il valore della funzione
    e la sua incertezza gᵀΣg, valutati come li valuta sensor-manager.
    """
    expected = json.loads((RUN5 / "last_calibration.json").read_text(encoding="utf-8"))["coefficients"]
    theta = [expected[f"a{i}"] for i in range(4)]
    result = execute(_job(JobKind.SESSION_FIT), FakeMetrology(_context()), _settings(tmp_path))

    assert result.status == JobStatus.DONE, result.error
    function = result.items[0].function
    c = [coefficient.value for coefficient in function.c]
    assert "((x - " in function.formula, "forma centrata"
    for d in (2667.0, 10000.0, 30000.0, 45000.0, 58668.0):
        mario = sum(t * d**k for k, t in enumerate(theta))
        assert evaluate(function.formula, d, c) == pytest.approx(mario, rel=1e-9)
        g = [d**k for k in range(4)]
        u_mario = math.sqrt(sum(g[i] * expected["cov_theta"][i][j] * g[j] for i in range(4) for j in range(4)))
        assert standard_uncertainty(function.formula, d, c, result.items[0].covariance) == pytest.approx(u_mario, rel=1e-6)
    # Il messaggio rispetta il contratto: si rilegge uguale.
    assert CalibrationResult.model_validate_json(result.to_json()) == result


def test_una_sessione_senza_gradini_e_un_errore_leggibile(tmp_path: Path):
    result = execute(_job(JobKind.SESSION_FIT), FakeMetrology(_context(steps=[])), _settings(tmp_path))
    assert result.status == JobStatus.FAILED
    assert result.error.code == "NO_STEPS"


def test_un_tipo_di_job_non_ancora_supportato(tmp_path: Path):
    result = execute(_job(JobKind.CERT_FIT), FakeMetrology(_context()), _settings(tmp_path))
    assert result.error.code == "NOT_SUPPORTED"


# --------------------------------------------------------------------------------------------
# As-found con la funzione in vigore, as-left su punti indipendenti, campione dal certificato
# --------------------------------------------------------------------------------------------


def _run5_function() -> tuple[dict, list[list[float]]]:
    """La funzione della run 5 nella forma del blocco ``calibration``, sulle potenze del grezzo."""
    k = json.loads((RUN5 / "last_calibration.json").read_text(encoding="utf-8"))["coefficients"]
    block = {"type": "cubic_lsb", "cubic_lsb": "c[0] + c[1] * x + c[2] * x**2 + c[3] * x**3", "input": ["x"], "c": [{"id": i, "value": k[f"a{i}"]} for i in range(4)]}
    return block, k["cov_theta"]


def _mario_points() -> dict[float, dict]:
    points = json.loads((RUN5 / "last_calibration.json").read_text(encoding="utf-8"))["calibration_points"]
    return {p["t_nominal"]: p for p in points}


def _with_as_left(context: dict, setpoints: tuple[float, ...]) -> dict:
    """Gradini AS_LEFT con gli stessi dati dei gradini as-found agli stessi setpoint."""
    found = [s for s in context["steps"] if s["setpoint"] in setpoints]
    left = [{**s, "phase": "AS_LEFT", "stepIndex": i} for i, s in enumerate(found)]
    return {**context, "steps": context["steps"] + left}


def test_verification_con_la_funzione_nuova_ridà_i_residui_di_mario(tmp_path: Path):
    """L'as-left sugli stessi dati del fit, con la funzione del fit, e' il residuo di Mario:
    e' la prova che statistiche dei gradini ed errori del motore sono quelli della pipeline."""
    block, covariance = _run5_function()
    context = _with_as_left(_context(), (-10.0, 30.0))
    context["items"][0]["latestFit"] = {"decision": "ADJUST", "function": block, "covariance": covariance, "calibratedMin": -20.0, "calibratedMax": 100.0}
    result = execute(_job(JobKind.VERIFICATION), FakeMetrology(context), _settings(tmp_path))

    assert result.status == JobStatus.DONE, result.error
    item = result.items[0]
    assert item.adjustment_applied is True
    assert {p.phase.value for p in item.points} == {"AS_LEFT"}
    mario = _mario_points()
    for p in item.points:
        assert p.error == pytest.approx(mario[p.nominal]["M_e_post"], rel=1e-6)
        assert p.reference_value == pytest.approx(mario[p.nominal]["T_ref"], rel=1e-12)
        # Mario conta la risoluzione del campione come 0,001/√12, il motore usa quella del
        # modello del campione (0,01): l'incertezza e' di poco piu' grande, mai piu' piccola.
        assert mario[p.nominal]["U_exp"] <= p.expanded_u <= 1.01 * mario[p.nominal]["U_exp"]
    # Errori fino a 1,5 °C contro una tolleranza di 0,6: l'as-left non e' conforme.
    assert item.conformity[0].verdict == Verdict.NON_CONFORM
    assert item.suggested_decision == SuggestedDecision.INVESTIGATE


def test_verification_senza_decisione_si_ferma(tmp_path: Path):
    context = _with_as_left(_context(), (-10.0,))
    result = execute(_job(JobKind.VERIFICATION), FakeMetrology(context), _settings(tmp_path))
    assert result.error.code == "NO_DECISION"


def test_verification_senza_regolazione_verifica_la_funzione_in_vigore(tmp_path: Path):
    block, covariance = _run5_function()
    context = _with_as_left(_context(), (30.0,))
    context["items"][0]["currentCertificate"] = {"id": 5, "number": "LAB-2026-0001", "revision": 1, "function": block, "covariance": covariance}
    context["items"][0]["latestFit"] = {"decision": "NO_ADJUSTMENT", "function": {"type": "linear_lsb", "linear_lsb": "c[0] + c[1] * x", "input": ["x"], "c": [{"id": 0, "value": 0}, {"id": 1, "value": 1}]}}
    result = execute(_job(JobKind.VERIFICATION), FakeMetrology(context), _settings(tmp_path))

    assert result.status == JobStatus.DONE, result.error
    item = result.items[0]
    assert item.adjustment_applied is False
    assert "LAB-2026-0001" in item.budget["verifiedFunction"]
    assert item.points[0].error == pytest.approx(_mario_points()[30.0]["M_e_post"], rel=1e-6)


def test_session_fit_calcola_l_as_found_con_la_funzione_in_vigore(tmp_path: Path):
    block, covariance = _run5_function()
    context = _context()
    context["items"][0]["currentCertificate"] = {"id": 5, "number": "LAB-2026-0001", "revision": 1, "function": block, "covariance": covariance}
    result = execute(_job(JobKind.SESSION_FIT), FakeMetrology(context), _settings(tmp_path))

    assert result.status == JobStatus.DONE, result.error
    item = result.items[0]
    assert "LAB-2026-0001" in item.budget["asFoundFrom"]
    mario = _mario_points()
    assert [p.error for p in item.points] == pytest.approx([mario[p.nominal]["M_e_post"] for p in item.points], rel=1e-6)
    assert item.suggested_decision == SuggestedDecision.ADJUST


def _reference_certificate(valid_until: str = "2027-12-31T00:00:00Z", correction: float = 0.1) -> dict:
    return {
        "id": 9,
        "number": "LAT-999-123",
        "revision": 1,
        "issuer": "Laboratorio accreditato",
        "validUntil": valid_until,
        "points": [
            {"phase": "AS_LEFT", "pointIndex": 0, "indicatedValue": -30.0, "correction": correction, "expandedU": 0.02, "k": 2.0},
            {"phase": "AS_LEFT", "pointIndex": 1, "indicatedValue": 110.0, "correction": correction, "expandedU": 0.04, "k": 2.0},
        ],
    }


def test_il_fit_usa_le_correzioni_del_certificato_del_campione(tmp_path: Path):
    """Una correzione costante di +0,1 sul campione sposta la funzione di +0,1 (minimi quadrati
    lineari nei coefficienti), e l'incertezza del campione e' quella del certificato."""
    plain = execute(_job(JobKind.SESSION_FIT), FakeMetrology(_context()), _settings(tmp_path)).items[0]
    context = _context()
    context["referenceCertificate"] = _reference_certificate()
    corrected = execute(_job(JobKind.SESSION_FIT), FakeMetrology(context), _settings(tmp_path))

    assert corrected.status == JobStatus.DONE, corrected.error
    item = corrected.items[0]
    for d in (5000.0, 30000.0, 55000.0):
        shifted = evaluate(item.function.formula, d, [c.value for c in item.function.c])
        original = evaluate(plain.function.formula, d, [c.value for c in plain.function.c])
        assert shifted - original == pytest.approx(0.1, abs=1e-9)
    reference = item.budget["referenceStandard"]
    assert reference["fromCertificate"] is True
    assert reference["certificate"]["number"] == "LAT-999-123"
    assert reference["uReferenceForFit"] == pytest.approx(0.02), "il massimo di U/k dei punti che racchiudono le letture"


def test_un_campione_scaduto_ferma_il_fit(tmp_path: Path):
    context = _context()
    context["referenceCertificate"] = _reference_certificate(valid_until="2026-01-01T00:00:00Z")
    result = execute(_job(JobKind.SESSION_FIT), FakeMetrology(context), _settings(tmp_path))
    assert result.error.code == "REFERENCE_EXPIRED"


def test_documents_carica_dcc_e_pdf_a_nome_del_proprietario(tmp_path: Path):
    metrology = FakeMetrology(_context())
    result = execute(_job(JobKind.DOCUMENTS), metrology, _settings(tmp_path))

    assert result.status == JobStatus.DONE, result.error
    assert {d.kind.value for d in result.documents} == {"DCC_XML", "CERTIFICATE_PDF"}
    assert all(d.calibrated_item_id == ITEM["id"] for d in result.documents)
    assert all(owner == "cliente" for _, _, owner, _ in metrology.uploads)


def test_documents_niente_per_chi_va_riparato(tmp_path: Path):
    context = _context()
    context["items"][0]["latestFit"] = {"decision": "REPAIR"}
    metrology = FakeMetrology(context)
    result = execute(_job(JobKind.DOCUMENTS), metrology, _settings(tmp_path))
    assert result.status == JobStatus.DONE and result.documents == [] and metrology.uploads == []


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


def test_il_dcc_cita_procedura_motore_e_campione(tmp_path: Path):
    """Passo 3.6: procedura in usedMethods, motore con l'hash git in usedSoftware, campione con
    il suo certificato in measuringEquipments, e non piu' fra gli oggetti tarati."""
    import xml.etree.ElementTree as ET

    block, covariance = _run5_function()
    context = _context()
    context["referenceCertificate"] = _reference_certificate()
    context["items"][0]["latestFit"] = {"decision": "ADJUST", "function": block, "covariance": covariance}
    metrology = FakeMetrology(context)
    metrology.template_version = lambda kind, template_id, version: {  # type: ignore[method-assign]
        "kind": "procedure", "templateId": 401, "name": "Taratura per confronto in pozzetto a secco",
        "fitModel": "cubic", "points": [-20.0, 0.0, 20.0, 40.0], "decisionRule": {"type": "guarded", "pfaMax": 0.02},
    }
    result = execute(_job(JobKind.DOCUMENTS), metrology, _settings(tmp_path))
    assert result.status == JobStatus.DONE, result.error

    ns = {"dcc": "https://ptb.de/dcc"}
    root = ET.fromstring(metrology.contents["DCC_XML"])
    measurement = root.find(".//dcc:measurementResult", ns)
    order = [child.tag.split("}")[1] for child in measurement]
    assert order[:5] == ["name", "usedMethods", "usedSoftware", "measuringEquipments", "influenceConditions"], order

    first_method = measurement.find("dcc:usedMethods/dcc:usedMethod/dcc:name/dcc:content[@lang='en']", ns).text
    assert "401" in first_method and "1.0.0" in first_method
    assert measurement.findtext("dcc:usedSoftware/dcc:software/dcc:release", namespaces=ns) == f"{__version__}+test"

    equipment = measurement.find("dcc:measuringEquipments/dcc:measuringEquipment[@id='reference-standard']", ns)
    values = [e.text for e in equipment.findall("dcc:identifications/dcc:identification/dcc:value", ns)]
    assert "B12345" in values and "LAT-999-123 rev. 1" in values
    assert len(root.findall("dcc:administrativeData/dcc:items/dcc:item", ns)) == 1, "il campione non e' un oggetto tarato"
    statements = " ".join(c.text or "" for c in root.findall(".//dcc:statement/dcc:declaration/dcc:content", ns))
    assert f"f(x) = {block['cubic_lsb']}" in statements, "la funzione come la applica il server"
