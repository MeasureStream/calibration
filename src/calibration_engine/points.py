"""Errori di un esemplare con una funzione data, e conformita' con la banda di guardia.

As-found e as-left sono la stessa misura fatta in due momenti, con due funzioni diverse
(passo 3.5 [S §6.2]):

- **as-found**: i gradini del fit, valutati con la funzione **in vigore** sul server prima di
  questa taratura: quella del certificato corrente dell'esemplare, o la formula nominale del
  template se l'esemplare non ne ha. E' lo stato in cui lo strumento e' stato trovato. Mario
  la calcolava con i «vecchi coefficienti» dello stesso modello di fit, che non sono la
  funzione che il server stava usando;
- **as-left**: gradini indipendenti da quelli del fit (``asLeft.points`` della procedura),
  valutati con la funzione che resta in vigore dopo la decisione: la nuova se si regola, la
  stessa di prima se no (job VERIFICATION).

L'errore e' ``e = f(D̄) − R̄``, con R̄ la media delle letture del campione gia' corrette dal suo
certificato. L'incertezza di e ha la struttura del budget di Mario: tipo A del campione, il
campione (certificato e metodo), la sua risoluzione, tipo A dell'indicazione e la risoluzione
di un LSB, entrambe portate in grandezza con la sensibilita' locale f'(D̄). L'incertezza dei
coefficienti della funzione non entra: e' l'errore *di quella funzione* che si misura.

La conformita' e' quella delle specifiche del modello (MPE del template, con la stessa lettura
di ``MetrologySpec.mpeAt`` di sensor-manager) con una regola a banda di guardia: un punto e'
conforme se |e| ≤ MPE e la probabilita' di falsa accettazione (PFA) non supera quella della
procedura; con |e| ≤ MPE ma PFA oltre la soglia e' in zona di guardia; con |e| > MPE non e'
conforme.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from calibration_engine.contracts import CalibrationPoint, ConformityResult, ConformityTarget, Phase, SourceRef, Verdict
from calibration_engine.formula import evaluate

#: Fattore di copertura dei punti.
K = 2.0


class FunctionNotEvaluable(Exception):
    """La funzione non da' un numero sui grezzi della sessione (dominio diverso?)."""


#: Le letture per blocco se la procedura non lo dice: il ``sample_size`` di Mario.
DEFAULT_BLOCK_SIZE = 20


@dataclass(frozen=True)
class StepStats:
    """Medie e incertezze tipo A di un gradino: del campione (grandezza) e del sensore (LSB)."""

    phase: str
    step_index: int
    setpoint: float | None
    ref_mean: float
    u_a_ref: float
    raw_mean: float
    u_a_raw: float
    blocks: int
    u_reference: float | None = None


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def stats(steps: list[dict[str, Any]], block_size: int = DEFAULT_BLOCK_SIZE) -> list[StepStats]:
    """Le statistiche di ogni gradino con la regola della pipeline di Mario.

    I campioni del sensore si dividono in un frame per lettura del campione (come fa
    l'adattatore), di ogni frame si prende la media; poi letture e frame si tengono a blocchi
    interi di ``block_size``: la media e' quella dei blocchi, l'incertezza tipo A e' lo scarto
    tipo delle medie dei blocchi diviso √(numero di blocchi). Cosi' as-found e as-left del
    motore usano gli stessi numeri del fit. Un gradino con meno di un blocco (as-left brevi)
    usa tutte le letture, con lo scarto tipo della media.
    """
    out = []
    for s in sorted(steps, key=lambda s: s["stepIndex"]):
        readings = [float(r) for r in s.get("referenceReadings") or []]
        samples = [float(v) for v in s.get("sensorSamples") or []]
        if not readings or not samples:
            continue
        per_frame = max(1, len(samples) // len(readings))
        frames = [_mean(samples[f * per_frame : (f + 1) * per_frame]) for f in range(len(readings)) if samples[f * per_frame : (f + 1) * per_frame]]
        n = min(len(readings), len(frames))
        blocks = n // block_size if block_size > 0 else 0
        if blocks >= 1:
            keep = blocks * block_size
            ref_blocks = [_mean(readings[b * block_size : (b + 1) * block_size]) for b in range(blocks)]
            raw_blocks = [_mean(frames[b * block_size : (b + 1) * block_size]) for b in range(blocks)]
            ref_mean, raw_mean = _mean(ref_blocks), _mean(raw_blocks)
            u_a_ref = _std(ref_blocks) / math.sqrt(blocks) if blocks > 1 else _std(readings[:keep]) / math.sqrt(keep)
            u_a_raw = _std(raw_blocks) / math.sqrt(blocks) if blocks > 1 else _std(frames[:keep]) / math.sqrt(keep)
        else:
            ref_mean, raw_mean = _mean(readings), _mean(samples)
            u_a_ref = _std(readings) / math.sqrt(len(readings))
            u_a_raw = _std(samples) / math.sqrt(len(samples))
        out.append(
            StepStats(
                phase=s.get("phase") or "AS_FOUND",
                step_index=s["stepIndex"],
                setpoint=s.get("setpoint"),
                ref_mean=ref_mean,
                u_a_ref=u_a_ref,
                raw_mean=raw_mean,
                u_a_raw=u_a_raw,
                blocks=blocks,
                u_reference=s.get("referenceU"),
            )
        )
    return out


@dataclass(frozen=True)
class Function:
    """Una funzione nella forma del blocco ``calibration`` di un template."""

    formula: str
    c: list[float]
    variable: str = "x"

    @staticmethod
    def of(block: dict[str, Any] | None) -> Function | None:
        if not block:
            return None
        kind = block.get("type")
        if not kind or kind == "none":
            return None
        formula = block.get(kind)
        if not isinstance(formula, str):
            return None
        declared = block.get("c")
        if isinstance(declared, list) and declared:
            c = [float(e.get("value", 0.0)) for e in sorted(declared, key=lambda e: e.get("id", 0))]
        else:
            c = [float(v) for v in block.get("coefficients") or []]
        inputs = block.get("input")
        variable = inputs[0] if isinstance(inputs, list) and inputs else "x"
        return Function(formula, c, variable)

    def __call__(self, x: float) -> float | None:
        return evaluate(self.formula, x, self.c, self.variable)

    def derivative(self, x: float) -> float | None:
        """Differenze centrali su un LSB, come ``ConversionFunction.derivative`` del server."""
        above, below = self(x + 1), self(x - 1)
        if above is None or below is None:
            return None
        return (above - below) / 2.0


@dataclass(frozen=True)
class Budget:
    """I contributi del campione che non dipendono dal gradino."""

    #: Incertezza tipo del campione quando il certificato non da' quella del gradino (targa).
    u_reference_default: float
    #: Contributi del metodo, gia' in quadratura.
    u_method: float = 0.0
    #: Risoluzione del campione, come incertezza tipo.
    u_resolution_reference: float = 0.001 / math.sqrt(12.0)


def point_errors(function: Function, steps: list[StepStats], budget: Budget, phase: Phase, dsi: str | None) -> list[CalibrationPoint]:
    points = []
    for i, s in enumerate(steps):
        indicated = function(s.raw_mean)
        sensitivity = function.derivative(s.raw_mean)
        if indicated is None or sensitivity is None:
            raise FunctionNotEvaluable(
                f"la funzione non e' valutabile sul grezzo {s.raw_mean:.6g} del gradino {s.step_index}: "
                "i campioni della sessione sono nello stesso dominio del report?"
            )
        u_ref_b = s.u_reference if s.u_reference is not None else budget.u_reference_default
        u_c = math.sqrt(
            s.u_a_ref**2
            + u_ref_b**2
            + budget.u_method**2
            + budget.u_resolution_reference**2
            + (abs(sensitivity) * s.u_a_raw) ** 2
            + (abs(sensitivity) / math.sqrt(12.0)) ** 2
        )
        error = indicated - s.ref_mean
        points.append(
            CalibrationPoint(
                phase=phase,
                point_index=i,
                nominal=s.setpoint,
                reference_value=s.ref_mean,
                indicated_value=indicated,
                error=error,
                correction=-error,
                expanded_u=K * u_c,
                k=K,
                dsi=dsi,
            )
        )
    return points


def tolerance_at(sensor_template: dict[str, Any] | None, y: float | None) -> float | None:
    """L'MPE per il valore y, letto come ``MetrologySpec.mpeAt`` di sensor-manager."""
    metrology = (sensor_template or {}).get("metrology") or {}
    entries = [e for e in metrology.get("mpe") or [] if isinstance(e, dict) and isinstance(e.get("value"), int | float)]
    if not entries:
        for key, name in (("Uncertainty", "maxTollerance"), ("readingUncertainty", "absUncertainty")):
            found = next((e for e in metrology.get(key) or [] if isinstance(e, dict) and e.get("varName") == name), None)
            if found and isinstance(found.get("value"), int | float) and found["value"] > 0:
                entries = [{"value": float(found["value"])}]
                break
    for e in entries:
        low, high = e.get("min"), e.get("max")
        if y is None or ((low is None or y >= low) and (high is None or y <= high)):
            value = float(e["value"])
            if e.get("relative"):
                return None if y is None else abs(y) * value / 100.0
            return value
    return None


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def pfa(error: float, u: float, tolerance: float) -> float:
    """Probabilita' che l'errore vero stia fuori da ±tolleranza, dato quello misurato."""
    if u <= 0:
        return 0.0 if abs(error) <= tolerance else 1.0
    return _phi((-tolerance - error) / u) + 1.0 - _phi((tolerance - error) / u)


def conformity(
    points: list[CalibrationPoint],
    sensor_template: dict[str, Any] | None,
    template_ref: tuple[int, str] | None,
    pfa_max: float,
) -> tuple[ConformityResult | None, list[dict[str, Any]]]:
    """Il verdetto sulle specifiche del modello e il dettaglio per punto."""
    detail: list[dict[str, Any]] = []
    for p in points:
        tolerance = tolerance_at(sensor_template, p.reference_value)
        if tolerance is None or p.error is None or p.expanded_u is None:
            continue
        probability = pfa(p.error, p.expanded_u / (p.k or K), tolerance)
        if abs(p.error) > tolerance:
            verdict = Verdict.NON_CONFORM
        elif probability > pfa_max:
            verdict = Verdict.GUARD_ZONE
        else:
            verdict = Verdict.CONFORM
        detail.append({"pointIndex": p.point_index, "phase": p.phase.value, "tolerance": tolerance, "pfa": probability, "verdict": verdict.value})
    if not detail or template_ref is None:
        return None, detail
    verdicts = {d["verdict"] for d in detail}
    overall = Verdict.NON_CONFORM if Verdict.NON_CONFORM.value in verdicts else Verdict.GUARD_ZONE if Verdict.GUARD_ZONE.value in verdicts else Verdict.CONFORM
    return (
        ConformityResult(
            against=ConformityTarget.SPECIFICATION,
            source=SourceRef(kind="sensor", template_id=template_ref[0], version=template_ref[1]),
            decision_rule=f"guarded (PFA <= {pfa_max * 100:g} %)",
            tolerance=min(d["tolerance"] for d in detail),
            pfa=max(d["pfa"] for d in detail),
            verdict=overall,
        ),
        detail,
    )
