"""Gli ingressi di una sessione che il passo 3.5 controlla: campione, procedura, conformita'.

- il campione dal suo certificato in vigore, non da un JSON (criticita' M2);
- la procedura come ingresso: punti, stabilita', letture, isteresi, contributi del metodo;
- errori e conformita' con la banda di guardia, letti come li legge sensor-manager.
"""

from __future__ import annotations

import numpy as np
import pytest

from calibration_engine.contracts import CalibrationPoint, Phase, Verdict
from calibration_engine.points import Function, conformity, pfa, stats, tolerance_at
from calibration_engine.procedure import check, merge_hysteresis, method_uncertainty, minutes_of
from calibration_engine.reference import ReferenceStandardError, certificate_points, check_valid, correct_steps, correction_at

# --------------------------------------------------------------------------------------------
# Il campione dal suo certificato (M2)
# --------------------------------------------------------------------------------------------

CERTIFICATE = {
    "number": "LAT-1",
    "revision": 2,
    "validUntil": "2027-06-30",
    "points": [
        # Il certificato di una taratura con regolazione: contano i punti AS_LEFT.
        {"phase": "AS_FOUND", "indicatedValue": 0.0, "error": 0.5, "expandedU": 0.1, "k": 2.0},
        {"phase": "AS_LEFT", "indicatedValue": 0.0, "error": -0.02, "expandedU": 0.02, "k": 2.0},
        {"phase": "AS_LEFT", "referenceValue": 50.04, "correction": 0.04, "expandedU": 0.04, "k": 2.0},
        {"phase": "AS_LEFT", "indicatedValue": 100.0, "correction": 0.10, "expandedU": 0.06, "k": 2.0},
    ],
}


def test_i_punti_del_certificato_del_campione():
    points = certificate_points(CERTIFICATE)
    assert [(p.indicated, p.correction, p.u) for p in points] == [
        pytest.approx((0.0, 0.02, 0.01)),
        pytest.approx((50.0, 0.04, 0.02)),
        pytest.approx((100.0, 0.10, 0.03)),
    ]


def test_correzione_interpolata_e_incertezza_conservativa():
    points = certificate_points(CERTIFICATE)
    correction, u = correction_at(points, 75.0)
    assert correction == pytest.approx(0.07)
    assert u == pytest.approx(0.03), "il massimo dei due punti, non la media"
    assert correction_at(points, 101.0)[0] == pytest.approx(0.10), "entro il margine del 2 %: il punto estremo"
    with pytest.raises(ReferenceStandardError) as e:
        correction_at(points, 103.0)
    assert e.value.code == "REFERENCE_OUT_OF_RANGE"


def test_scadenza_alla_data_dei_gradini():
    check_valid(CERTIFICATE, [{"startedAt": "2027-06-30T10:00:00Z"}])
    with pytest.raises(ReferenceStandardError) as e:
        check_valid(CERTIFICATE, [{"startedAt": "2027-07-01T08:00:00Z"}])
    assert e.value.code == "REFERENCE_EXPIRED"


def test_letture_corrette_e_riepilogo():
    steps = [{"phase": "AS_FOUND", "stepIndex": 0, "setpoint": 50.0, "referenceReadings": [49.0, 51.0], "sensorSamples": [1, 2]}]
    corrected, summary = correct_steps(steps, CERTIFICATE)
    # 49 sta fra 0 (+0,02) e 50 (+0,04); 51 fra 50 (+0,04) e 100 (+0,10).
    assert corrected[0]["referenceReadings"] == pytest.approx([49.0 + 0.0396, 51.0 + 0.0412])
    assert corrected[0]["referenceU"] == pytest.approx(0.02)
    assert summary["fromCertificate"] and summary["certificate"]["number"] == "LAT-1"
    assert correct_steps(steps, None) == (steps, {"fromCertificate": False})


# --------------------------------------------------------------------------------------------
# La procedura come ingresso
# --------------------------------------------------------------------------------------------

PROCEDURE = {
    "points": [-20.0, 0.0, 20.0],
    "stability": {"slopeMax": 0.01, "window": "PT5M"},
    "readings": {"blockSize": 2, "blocks": 3},
    "methodUncertainty": [{"u": 0.02}, {"u": 0.01}],
    "asLeft": {"points": [10.0]},
}


def _step(index: int, setpoint: float, drift_per_minute: float = 0.0, minutes: float = 10.0, n: int = 30) -> dict:
    readings = [setpoint + drift_per_minute * (i + 0.5) * minutes / n for i in range(n)]
    return {"phase": "AS_FOUND", "stepIndex": index, "setpoint": setpoint, "minutes": minutes, "referenceReadings": readings, "sensorSamples": [1000.0] * (n * 2)}


def test_durate_iso_8601():
    assert minutes_of("PT5M") == 5.0
    assert minutes_of("PT30S") == 0.5
    assert minutes_of("PT1H30M") == 90.0
    assert minutes_of("P1D") == 1440.0
    assert minutes_of("5 minuti") is None


def test_contributi_del_metodo_in_quadratura():
    assert method_uncertainty(PROCEDURE) == pytest.approx(np.hypot(0.02, 0.01))
    assert method_uncertainty(None) == 0.0


def test_un_gradino_instabile_fuori_dai_punti_si_esclude():
    report = check(PROCEDURE, [_step(0, -20.0), _step(1, -10.0, drift_per_minute=0.05), _step(2, 0.0), _step(3, 20.0)])
    assert report.excluded == {1}
    assert report.steps[1].slope_per_minute == pytest.approx(0.05)
    assert not report.deviations


def test_l_unico_gradino_di_un_punto_resta_ma_e_una_deviazione():
    report = check(PROCEDURE, [_step(0, -20.0), _step(1, 0.0, drift_per_minute=-0.03), _step(2, 20.0)])
    assert report.excluded == set()
    assert any("instabile" in d for d in report.deviations)


def test_la_stabilita_si_guarda_nell_ultima_finestra():
    """Un gradino che si assesta: forte deriva all'inizio, piatto negli ultimi cinque minuti."""
    step = _step(0, -20.0)
    step["referenceReadings"] = [-20.0 + (0.05 * (10 - i) if i < 10 else 0.0) for i in range(30)]
    report = check(PROCEDURE, [step, _step(1, 0.0), _step(2, 20.0)])
    assert report.steps[0].stable is True


def test_un_punto_senza_gradini_e_una_deviazione_e_le_letture_poche_un_avviso():
    report = check(PROCEDURE, [_step(0, -20.0), _step(1, 0.0, n=2)])
    assert "punto 20 della procedura senza gradini AS_FOUND" in report.deviations
    assert any("letture del campione" in w for w in report.warnings)


def test_isteresi_salita_e_discesa_si_uniscono_per_il_fit():
    procedure = {**PROCEDURE, "hysteresis": True, "points": [10.0, 20.0, 30.0]}
    steps = [_step(0, 10.0), _step(1, 20.0), _step(2, 30.0), _step(3, 20.0), _step(4, 10.0)]
    report = check(procedure, steps)
    assert [s.direction for s in report.steps] == ["up", "up", "up", "down", "down"]
    assert report.deviations == [], "30 e' il punto d'inversione: si misura una volta"
    merged = merge_hysteresis(steps, report.steps)
    assert [s["setpoint"] for s in merged] == [10.0, 20.0, 30.0]
    assert len(merged[0]["referenceReadings"]) == 60


def test_isteresi_incompleta_e_una_deviazione():
    procedure = {**PROCEDURE, "hysteresis": True, "points": [10.0, 20.0, 30.0]}
    report = check(procedure, [_step(0, 10.0), _step(1, 20.0), _step(2, 30.0), _step(3, 20.0)])
    assert any("punto 10" in d and "salita e discesa" in d for d in report.deviations)


def test_l_as_left_si_confronta_con_i_suoi_punti():
    left = {**_step(0, 10.0), "phase": "AS_LEFT"}
    assert check(PROCEDURE, [left], "AS_LEFT").deviations == []


# --------------------------------------------------------------------------------------------
# Statistiche dei gradini, errori e conformita' con la banda di guardia
# --------------------------------------------------------------------------------------------


def test_statistiche_a_blocchi_come_la_pipeline():
    """Blocchi interi di letture: le letture in piu' (qui 2 su 42) restano fuori, come in Mario."""
    readings = [float(i % 3) for i in range(42)]
    samples = [float(v) for i in range(42) for v in (i, i)]
    s = stats([{"stepIndex": 0, "referenceReadings": readings, "sensorSamples": samples}], block_size=20)[0]
    assert s.blocks == 2
    assert s.ref_mean == pytest.approx(np.mean(readings[:40]))
    assert s.raw_mean == pytest.approx(np.mean(range(40)))
    blocks = [np.mean(range(0, 20)), np.mean(range(20, 40))]
    assert s.u_a_raw == pytest.approx(np.std(blocks, ddof=1) / np.sqrt(2))


def test_la_funzione_del_template():
    block = {"type": "linear_lsb", "linear_lsb": "c[0] + c[1] * d_in", "input": ["d_in"], "c": [{"id": 1, "value": 0.5}, {"id": 0, "value": -10.0}]}
    f = Function.of(block)
    assert f(100.0) == pytest.approx(40.0)
    assert f.derivative(100.0) == pytest.approx(0.5)
    assert Function.of({"type": "none"}) is None
    assert Function.of({"type": "linear", "coefficients": [0, 0]}) is None, "un tipo senza formula non e' una funzione"


def test_tolleranza_come_sensor_manager():
    mpe = {"metrology": {"mpe": [{"max": 0, "value": 0.5}, {"min": 0, "value": 2.0, "relative": True}]}}
    assert tolerance_at(mpe, -5) == 0.5
    assert tolerance_at(mpe, 50) == pytest.approx(1.0), "2 % di 50"
    assert tolerance_at({"metrology": {"Uncertainty": [{"varName": "maxTollerance", "value": 0.6}]}}, 10) == 0.6
    assert tolerance_at({}, 10) is None


def test_pfa_simmetrica_e_al_limite():
    assert pfa(0.0, 0.1, 0.3) == pytest.approx(2 * (1 - 0.9986501), rel=1e-4)
    assert pfa(0.3, 0.1, 0.3) == pytest.approx(0.5, rel=1e-6), "sul limite, meta' della distribuzione e' fuori"


def _point(index: int, error: float, u: float) -> CalibrationPoint:
    return CalibrationPoint(phase=Phase.AS_FOUND, point_index=index, reference_value=20.0, indicated_value=20.0 + error, error=error, expanded_u=2 * u, k=2.0)


def test_conforme_zona_di_guardia_non_conforme():
    sensor = {"metrology": {"mpe": [{"value": 0.3}]}}
    verdict, _ = conformity([_point(0, 0.05, 0.05)], sensor, (201, "2.2.0"), 0.02)
    assert verdict.verdict == Verdict.CONFORM
    verdict, detail = conformity([_point(0, 0.05, 0.05), _point(1, 0.25, 0.05)], sensor, (201, "2.2.0"), 0.02)
    assert verdict.verdict == Verdict.GUARD_ZONE and detail[1]["verdict"] == "GUARD_ZONE"
    verdict, _ = conformity([_point(0, 0.35, 0.01)], sensor, (201, "2.2.0"), 0.02)
    assert verdict.verdict == Verdict.NON_CONFORM
    assert conformity([_point(0, 0.1, 0.01)], {}, (201, "2.2.0"), 0.02) == (None, [])


# --------------------------------------------------------------------------------------------
# CERT_FIT: la funzione dai punti di un certificato esterno (passo 5.3)
# --------------------------------------------------------------------------------------------

from calibration_engine.certfit import CertFitError, certificate_pairs, fit_correction, function_block, inline  # noqa: E402
from calibration_engine.formula import evaluate  # noqa: E402

NOMINAL = {"type": "linear_201", "linear_201": "c[0] + c[1] * x", "input": ["x"], "c": [{"id": 0, "value": -40.0}, {"id": 1, "value": 0.01}]}


def _lab_points(references: list[float], u: float | None = 0.05) -> list[dict]:
    """Uno strumento che legge 0,3 + 1 % in piu' del vero: indicazione = r + 0,3 + 0,01·r."""
    return [
        {"phase": "AS_LEFT", "referenceValue": r, "indicatedValue": r + 0.3 + 0.01 * r, "expandedU": None if u is None else 2 * u, "k": 2.0}
        for r in references
    ]


def test_la_formula_nominale_con_i_coefficienti_scritti():
    assert inline("c[0] + c[1] * x", [-40.0, 0.01]) == "(-40.0) + (0.01) * x"
    with pytest.raises(CertFitError):
        inline("c[0] + c[2] * x", [1.0, 2.0])


def test_la_correzione_sopra_la_formula_nominale_restituisce_il_riferimento():
    y, r, u = certificate_pairs(_lab_points([-20.0, 0.0, 20.0, 40.0]))
    c, covariance, residuals = fit_correction(y, r, u, 1)
    assert np.max(np.abs(residuals)) < 1e-12, "il difetto e' lineare: la correzione lineare lo toglie tutto"
    block = function_block(NOMINAL, c, r"\degreeCelsius")
    formula = block[block["type"]]
    values = [e["value"] for e in block["c"]]
    for reference in (-20.0, 10.0, 40.0):
        indicated = reference + 0.3 + 0.01 * reference
        raw = (indicated + 40.0) / 0.01  # il grezzo che la formula nominale porta all'indicazione
        assert evaluate(formula, raw, values) == pytest.approx(reference, abs=1e-9)
    assert np.all(np.isfinite(covariance)) and covariance.shape == (2, 2)
    assert covariance[0][0] > 0


def test_senza_incertezze_e_con_due_punti_la_covarianza_non_si_inventa():
    y, r, u = certificate_pairs(_lab_points([0.0, 40.0], u=None))
    assert u is None
    _, covariance, _ = fit_correction(y, r, u, 1)
    assert np.all(np.isnan(covariance))


def test_un_punto_solo_non_basta():
    with pytest.raises(CertFitError) as e:
        certificate_pairs(_lab_points([20.0]))
    assert e.value.code == "TOO_FEW_POINTS"
