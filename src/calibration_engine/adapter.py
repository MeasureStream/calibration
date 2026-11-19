"""L'adattatore fra metrology-service e la pipeline di Mario (PIANO_METROLOGIA 3.4).

La pipeline resta **invariata**: legge i suoi file d'ingresso e scrive i suoi file d'uscita.
Questo modulo fa le due traduzioni:

- *andata*: dai gradini di una sessione al formato ``calib_*.json`` che Mario assemblava in
  ``KafkaCalibrationConsumer`` (letture del riferimento e campioni grezzi per gradino);
- *ritorno*: da ``last_calibration.json`` e ``conformity_results.json`` al contratto
  ``ItemResult``, con la funzione nella forma del blocco ``calibration`` di un template.

Tenere qui le traduzioni significa che le correzioni della pipeline (passo 3.5) non toccano il
contratto, e un cambiamento del contratto non tocca la pipeline.
"""

from __future__ import annotations

from typing import Any

from calibration_engine.contracts import (
    CalibratedRange,
    CalibrationFunction,
    CalibrationPoint,
    Coefficient,
    ConformityResult,
    ConformityTarget,
    ItemResult,
    Phase,
    SourceRef,
    SuggestedDecision,
    Verdict,
)
from calibration_engine.formula import centered_polynomial

#: I modelli di fit della pipeline, come formula del valutatore a grammatica chiusa del server.
#: La variabile e' la lettura grezza in LSB, come nei coefficienti a0..a3 di Mario.
FORMULAS = {
    "linear": "c[0] + c[1] * x",
    "quadratic": "c[0] + c[1] * x + c[2] * x**2",
    "cubic": "c[0] + c[1] * x + c[2] * x**2 + c[3] * x**3",
}


class AdapterError(Exception):
    """I dati della sessione non bastano a costruire l'ingresso della pipeline."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def item_steps(context: dict[str, Any], item: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    """I gradini di una fase per un esemplare, in ordine."""
    index = item["sensorIndex"]
    return sorted(
        (s for s in context.get("steps") or [] if s["sensorIndex"] == index and s["phase"] == phase),
        key=lambda s: s["stepIndex"],
    )


def pipeline_input(
    context: dict[str, Any],
    item: dict[str, Any],
    phase: str = "AS_FOUND",
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """I gradini di un esemplare nel formato che ``analisi_calib_data.py`` legge con ``--input``.

    I campioni del sensore di un gradino si dividono in parti uguali, una per lettura del
    riferimento: e' la regola di Mario, perche' il Raspberry campiona il sensore a blocchi
    durante ogni lettura del riferimento. ``steps`` sono i gradini gia' preparati dal motore
    (letture del campione corrette, gradini instabili esclusi, salita e discesa unite); senza,
    quelli della sessione cosi' come sono.
    """
    index = item["sensorIndex"]
    steps = item_steps(context, item, phase) if steps is None else steps
    if not steps:
        raise AdapterError("NO_STEPS", f"nessun gradino {phase} per il sensore {index}")

    session = context.get("session") or {}
    out: dict[str, Any] = {
        "calibration_id": f"session-{session.get('id')}-sensor-{index}",
        "mu_id": item.get("muExtendedId"),
        "sensor_id": index,
        "sensor_sampling_freq": [],
        "start_time": [],
        "start_time_dwell": [],
        "steps": [],
        "reference_temperature_samples": [],
        "sensor_raw_samples": [],
    }
    for step in steps:
        target = step.get("setpoint")
        readings = step.get("referenceReadings") or []
        samples = step.get("sensorSamples") or []
        if not readings or not samples:
            raise AdapterError("EMPTY_STEP", f"il gradino {step['stepIndex']} del sensore {index} e' vuoto")

        out["sensor_sampling_freq"].append(step.get("sensorSamplingFreq") or 1)
        out["start_time"].append(step.get("startedAt") or "")
        out["start_time_dwell"].append(step.get("dwellStartedAt") or "")
        out["steps"].append(f"({_number(target)},{step.get('minutes') or 1})")
        for reading in readings:
            out["reference_temperature_samples"].append(
                {"index_step": step["stepIndex"], "target": target, "reading": reading, "stable_hw": "True"}
            )
        per_frame = max(1, len(samples) // len(readings))
        for frame in range(len(readings)):
            values = samples[frame * per_frame : (frame + 1) * per_frame]
            if values:
                out["sensor_raw_samples"].append({"index_step": step["stepIndex"], "target": target, "value": values})
    return out


def _number(value: Any) -> Any:
    """Come Mario scriveva il setpoint nella stringa del gradino: ``-20.0``, non ``-20``."""
    return float(value) if isinstance(value, (int, float)) else value


_DEGREES = {"quadratic": 2, "cubic": 3}


def function_of(result: dict[str, Any]) -> tuple[str, list[float], list[list[float]] | None]:
    """Formula, coefficienti e covarianza della funzione da consegnare al server.

    - Quadratico e cubico, con la forma centrata della pipeline (passo 3.5): il polinomio in
      ``s = (x - centro) / semiampiezza``, con la covarianza dei coefficienti in s. Su una gamma
      stretta la covarianza sulle potenze del grezzo perde cifre quando si calcola gᵀΣg;
      quella in s no. Centro e semiampiezza stanno nella formula come costanti.
    - Quadratico e cubico senza forma centrata (run archiviate, ``--convert-units``): i
      coefficienti storici a0..an sulle potenze del grezzo, come prima.
    - Lineare: Mario scrive y = A·D + B, quindi c[0] = B e c[1] = A, con la covarianza
      ricostruita da u(A), u(B) e cov(A, B).
    """
    model = result.get("model") or ""
    coefficients = result.get("coefficients") or {}
    scaled = result.get("scaled_fit")

    if model in _DEGREES and scaled:
        formula = centered_polynomial(_DEGREES[model], float(scaled["center"]), float(scaled["half_range"]))
        return formula, [float(v) for v in scaled["b"]], scaled.get("cov_b")

    if model == "linear":
        try:
            a, b = float(coefficients["A"]), float(coefficients["B"])
        except (KeyError, TypeError) as e:
            raise AdapterError("MISSING_COEFFICIENTS", "il risultato lineare non ha A e B") from e
        u_a, u_b, cov_ab = coefficients.get("u_A"), coefficients.get("u_B"), coefficients.get("cov_AB")
        covariance = None
        if u_a is not None and u_b is not None:
            cross = float(cov_ab or 0.0)
            covariance = [[float(u_b) ** 2, cross], [cross, float(u_a) ** 2]]
        return FORMULAS["linear"], [b, a], covariance

    formula = FORMULAS.get(model)
    if formula is None:
        raise AdapterError("UNSUPPORTED_MODEL", f"il modello di fit '{model}' non ha ancora una formula per il server")
    order = len(formula.split("c[")) - 1
    try:
        values = [float(coefficients[f"a{i}"]) for i in range(order)]
    except (KeyError, TypeError) as e:
        raise AdapterError("MISSING_COEFFICIENTS", f"il risultato '{model}' non ha a0..a{order - 1}") from e
    return formula, values, coefficients.get("cov_theta")


def item_result(
    result: dict[str, Any],
    conformity: dict[str, Any] | None,
    item: dict[str, Any],
    sensor_template: dict[str, Any] | None,
    template_ref: tuple[int, str] | None,
) -> ItemResult:
    """Le uscite della pipeline per un esemplare, nel contratto ``ItemResult``."""
    model = result.get("model")
    dsi = (sensor_template or {}).get("unit")
    formula, values, covariance = function_of(result)
    # Il grezzo in LSB e' un conteggio, adimensionale in D-SI (\one): ogni coefficiente ha
    # quindi l'unita' della grandezza, sia nella forma centrata sia sulle potenze di D.
    c = [Coefficient(id=i, value=v, dsi=dsi) for i, v in enumerate(values)]

    # As-found della pipeline: errori con i «vecchi coefficienti» di Mario. E' il ripiego
    # quando la funzione in vigore sul server non e' valutabile sui grezzi della sessione; di
    # norma jobs.py li sostituisce con quelli calcolati dalla funzione in vigore (points.py).
    # I residui del fit (M_e_post) non sono un as-left: stanno nel budget [S §6.2].
    points: list[CalibrationPoint] = []
    residuals: list[dict[str, Any]] = []
    for i, p in enumerate(result.get("calibration_points") or []):
        reference = float(p["T_ref"])
        error = p.get("M_e_pre")
        if error is not None:
            points.append(
                CalibrationPoint(
                    phase=Phase.AS_FOUND,
                    point_index=i,
                    nominal=p.get("t_nominal"),
                    reference_value=reference,
                    indicated_value=reference + float(error),
                    error=float(error),
                    correction=-float(error),
                    expanded_u=p.get("U_exp"),
                    k=2.0,
                    dsi=dsi,
                )
            )
        if p.get("M_e_post") is not None:
            residuals.append({"pointIndex": i, "nominal": p.get("t_nominal"), "reference": reference, "residual": float(p["M_e_post"])})

    nominals = [float(t) for t in result.get("temp_nominali") or [] if t is not None]
    calibrated_range = CalibratedRange(min=min(nominals), max=max(nominals), dsi=dsi) if nominals else None

    budget = {
        "fitQuality": result.get("fit_quality"),
        "fitResiduals": residuals,
        "reference": result.get("reference"),
        "sensor": {k: v for k, v in (result.get("sensor") or {}).items() if not isinstance(v, list)},
        "asFoundFrom": "pipeline",
    }

    conformity_results: list[ConformityResult] = []
    suggested = SuggestedDecision.NONE
    if conformity:
        summary = conformity.get("summary") or {}
        per_point = (conformity.get("check_G") or {}).get("per_point") or []
        tolerance = next((p.get("max_allowed_error_y") for p in per_point if p.get("max_allowed_error_y") is not None), None)
        params = conformity.get("check_H_params") or {}
        if template_ref is not None:
            conformity_results.append(
                ConformityResult(
                    against=ConformityTarget.SPECIFICATION,
                    source=SourceRef(kind="sensor", template_id=template_ref[0], version=template_ref[1]),
                    decision_rule=f"guarded (PFA <= {params.get('pfa_threshold_pct', '?')} %)",
                    tolerance=float(tolerance if tolerance is not None else params.get("mae_y", 0.0)),
                    pfa=None,
                    verdict=Verdict.CONFORM if summary.get("overall") == "COMPLIANT" else Verdict.NON_CONFORM,
                )
            )
        # Un punto as-found fuori tolleranza e' il motivo per regolare.
        if any(p.get("G1_in_range") is False for p in per_point):
            suggested = SuggestedDecision.ADJUST

    return ItemResult(
        calibrated_item_id=item["id"],
        mu_extended_id=item["muExtendedId"],
        sensor_index=item["sensorIndex"],
        function=CalibrationFunction.model_validate({"type": f"{model}_lsb", f"{model}_lsb": formula, "input": ["x"], "c": [x.model_dump() for x in c]}),
        covariance=covariance,
        calibrated_range=calibrated_range,
        adjustment_applied=False,
        points=points,
        budget=budget,
        conformity=conformity_results,
        suggested_decision=suggested,
    )


def tolerance_of(sensor_template: dict[str, Any] | None, value: float | None = None) -> float | None:
    """La tolleranza del controllo G dal template (C7): ``mpe`` dello schema 2.2.0, se c'e'."""
    metrology = (sensor_template or {}).get("metrology") or {}
    for entry in metrology.get("mpe") or []:
        low, high = entry.get("min"), entry.get("max")
        if value is None or ((low is None or value >= low) and (high is None or value <= high)):
            return float(entry["value"]) if not entry.get("relative") else None
    return None
