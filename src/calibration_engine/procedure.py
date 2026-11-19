"""La procedura come ingresso del motore (passo 3.5 [S §6.2]).

La procedura pubblicata nel registro (``kind: procedure``, per esempio la 401) dice dove
fermarsi, quando una lettura e' stabile, quante letture fare, se percorrere i punti in salita
e in discesa e quali contributi aggiunge il metodo. Prima del passo 3.5 il motore ne usava solo
il modello di fit e la PFA; qui si controllano sui dati ricevuti anche gli altri campi.

Le scelte, che valgono come regola del laboratorio e stanno anche in IMPLEMENTAZIONE_METROLOGIA:

- **stabilita'**: la pendenza delle letture del campione nell'ultima finestra del gradino
  (``stability.window``, ISO 8601) deve stare sotto ``stability.slopeMax`` per minuto. Le
  letture si considerano equispaziate sulla durata del gradino (``minutes``), perche' il
  Raspberry non manda l'istante di ciascuna. Un gradino instabile si **esclude dal fit**; se
  pero' e' l'unico per un punto della procedura lo si tiene, e la deviazione dal metodo resta
  scritta nel risultato, dove il tecnico e l'approvatore la vedono;
- **punti**: un punto della procedura senza gradini e' una deviazione dal metodo, non un errore
  del job: il fit si puo' fare lo stesso, ma il certificato deve dirlo;
- **letture**: la pipeline fa la media a blocchi di ``blockSize`` letture del campione, ognuna
  con il suo frame di campioni del sensore, e ne vuole ``blocks``: meno di ``blockSize × blocks``
  letture del campione, o meno campioni del sensore che letture, e' un avviso (sotto un blocco
  intero la pipeline scarta il gradino);
- **isteresi**: con ``hysteresis: true`` ogni punto va misurato in salita e in discesa. I due
  gradini si uniscono per il fit, e la loro differenza diventa un contributo d'incertezza
  (rettangolare di ampiezza piena h, quindi u = h/√12), calcolato dopo il fit;
- **contributi del metodo**: ``methodUncertainty[].u`` sono incertezze tipo e si sommano in
  quadratura al campione.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class StepCheck:
    phase: str
    step_index: int
    setpoint: float | None
    slope_per_minute: float | None = None
    stable: bool | None = None
    direction: str | None = None
    excluded: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "stepIndex": self.step_index,
            "setpoint": self.setpoint,
            "slopePerMinute": self.slope_per_minute,
            "stable": self.stable,
            "direction": self.direction,
            "excluded": self.excluded,
        }


@dataclass
class ProcedureReport:
    steps: list[StepCheck] = field(default_factory=list)
    deviations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    u_method: float = 0.0
    hysteresis: bool = False

    @property
    def excluded(self) -> set[int]:
        return {s.step_index for s in self.steps if s.excluded}

    def to_wire(self) -> dict[str, Any]:
        return {
            "steps": [s.to_wire() for s in self.steps],
            "methodDeviations": self.deviations,
            "warnings": self.warnings,
            "uMethod": self.u_method,
            "hysteresis": self.hysteresis,
        }


_DURATION = re.compile(r"^P(?:(?P<d>\d+(?:\.\d+)?)D)?(?:T(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$")


def minutes_of(duration: str | None) -> float | None:
    """Una durata ISO 8601 semplice (``PT5M``, ``PT30S``, ``PT1H``, ``P1D``) in minuti."""
    if not duration:
        return None
    match = _DURATION.match(duration.strip().upper())
    if not match or not any(match.groupdict().values()):
        return None
    parts = {k: float(v) if v else 0.0 for k, v in match.groupdict().items()}
    return parts["d"] * 1440 + parts["h"] * 60 + parts["m"] + parts["s"] / 60


def slope_per_minute(readings: list[float], minutes: float | None, window: float | None) -> float | None:
    """La pendenza delle letture nell'ultima finestra del gradino, in unita' al minuto."""
    if not minutes or minutes <= 0 or len(readings) < 3:
        return None
    n = len(readings)
    times = (np.arange(n) + 0.5) * (minutes / n)
    values = np.asarray(readings, dtype=float)
    if window and window < minutes:
        keep = times >= minutes - window
        times, values = times[keep], values[keep]
    if len(values) < 3:
        return None
    return float(np.polyfit(times, values, 1)[0])


def method_uncertainty(procedure: dict[str, Any] | None) -> float:
    """I contributi del metodo in quadratura (sono gia' incertezze tipo)."""
    total = 0.0
    for entry in (procedure or {}).get("methodUncertainty") or []:
        u = entry.get("u")
        if isinstance(u, int | float) and u > 0:
            total += float(u) ** 2
    return math.sqrt(total)


def _matches(setpoint: float | None, point: float, tolerance: float) -> bool:
    return setpoint is not None and abs(float(setpoint) - point) <= tolerance


def check(procedure: dict[str, Any] | None, steps: list[dict[str, Any]], phase: str = "AS_FOUND") -> ProcedureReport:
    """Controlla i gradini di un esemplare per una fase contro la procedura."""
    procedure = procedure or {}
    report = ProcedureReport(u_method=method_uncertainty(procedure), hysteresis=bool(procedure.get("hysteresis")))
    ordered = sorted(steps, key=lambda s: s["stepIndex"])

    stability = procedure.get("stability") or {}
    slope_max = stability.get("slopeMax")
    window = minutes_of(stability.get("window"))
    previous: float | None = None
    for step in ordered:
        setpoint = step.get("setpoint")
        c = StepCheck(phase=phase, step_index=step["stepIndex"], setpoint=setpoint)
        if setpoint is not None and previous is not None:
            c.direction = "down" if float(setpoint) < previous else "up"
        elif setpoint is not None:
            c.direction = "up"
        previous = float(setpoint) if setpoint is not None else previous
        if slope_max is not None:
            c.slope_per_minute = slope_per_minute([float(r) for r in step.get("referenceReadings") or []], step.get("minutes"), window)
            c.stable = None if c.slope_per_minute is None else abs(c.slope_per_minute) <= float(slope_max)
        report.steps.append(c)

    # Punti della fase: quelli del fit per l'as-found, quelli indipendenti per l'as-left.
    points = procedure.get("points") if phase == "AS_FOUND" else (procedure.get("asLeft") or {}).get("points")
    points = [float(p) for p in points or [] if isinstance(p, int | float)]
    span = (max(points) - min(points)) if len(points) > 1 else 0.0
    tolerance = 1e-6 + 0.01 * span

    for c in report.steps:
        label = f"{c.setpoint:g}" if c.setpoint is not None else "?"
        covers_point = any(_matches(c.setpoint, p, tolerance) for p in points)
        if c.stable is False:
            same_point = [
                o for o in report.steps
                if o is not c and c.setpoint is not None and _matches(o.setpoint, float(c.setpoint), tolerance) and o.stable is not False
            ]
            if covers_point and not same_point:
                report.deviations.append(
                    f"gradino {c.step_index} ({label}) instabile: {c.slope_per_minute:.4g}/min oltre {slope_max}/min, "
                    "tenuto perche' e' l'unico per un punto della procedura"
                )
            else:
                c.excluded = True
                report.warnings.append(f"gradino {c.step_index} ({label}) instabile ({c.slope_per_minute:.4g}/min): escluso")
        elif c.stable is None and slope_max is not None:
            report.warnings.append(f"gradino {c.step_index}: stabilita' non valutabile (durata o letture insufficienti)")

    # Il punto d'inversione del ciclo (l'ultimo prima che la direzione cambi) si misura una volta
    # sola: e' insieme la fine della salita e l'inizio della discesa.
    turning = [
        float(a.setpoint)
        for a, b in zip(report.steps, report.steps[1:], strict=False)
        if a.setpoint is not None and a.direction and b.direction and a.direction != b.direction
    ]
    for p in points:
        if not any(_matches(c.setpoint, p, tolerance) for c in report.steps):
            report.deviations.append(f"punto {p:g} della procedura senza gradini {phase}")
        elif report.hysteresis and not any(abs(t - p) <= tolerance for t in turning):
            directions = {c.direction for c in report.steps if _matches(c.setpoint, p, tolerance)}
            if not {"up", "down"} <= directions:
                report.deviations.append(f"punto {p:g}: l'isteresi chiede salita e discesa, misurato solo in {', '.join(sorted(d for d in directions if d))}")

    readings = procedure.get("readings") or {}
    blocks, block_size = readings.get("blocks"), readings.get("blockSize")
    wanted = (blocks or 1) * (block_size or 1) if (blocks or block_size) else None
    for step in ordered:
        n_ref = len(step.get("referenceReadings") or [])
        n_raw = len(step.get("sensorSamples") or [])
        if wanted and n_ref < wanted:
            report.warnings.append(
                f"gradino {step['stepIndex']}: {n_ref} letture del campione, la procedura ne chiede {wanted} ({blocks} blocchi da {block_size})"
            )
        if n_ref and n_raw < n_ref:
            report.warnings.append(f"gradino {step['stepIndex']}: {n_raw} campioni del sensore per {n_ref} letture: ne serve almeno uno per lettura")
    return report


def hysteresis_pairs(steps: list[dict[str, Any]], checks: list[StepCheck]) -> dict[float, tuple[dict[str, Any], dict[str, Any]]]:
    """Per ogni setpoint misurato in salita e in discesa, la coppia di gradini (salita, discesa)."""
    direction = {c.step_index: c.direction for c in checks}
    by_point: dict[float, dict[str, dict[str, Any]]] = {}
    for step in steps:
        if step.get("setpoint") is None:
            continue
        d = direction.get(step["stepIndex"])
        if d in ("up", "down"):
            by_point.setdefault(float(step["setpoint"]), {}).setdefault(d, step)
    return {p: (v["up"], v["down"]) for p, v in by_point.items() if "up" in v and "down" in v}


def merge_hysteresis(steps: list[dict[str, Any]], checks: list[StepCheck]) -> list[dict[str, Any]]:
    """Salita e discesa dello stesso punto diventano un gradino solo, per il fit.

    La pipeline indicizza i gradini sul valore nominale: due gradini con lo stesso setpoint si
    sovrascriverebbero. Unendoli, il fit vede la media delle due direzioni.
    """
    pairs = hysteresis_pairs(steps, checks)
    if not pairs:
        return steps
    down_indexes = {down["stepIndex"] for _, down in pairs.values()}
    merged = []
    for step in steps:
        if step["stepIndex"] in down_indexes:
            continue
        pair = pairs.get(float(step["setpoint"])) if step.get("setpoint") is not None else None
        if pair and pair[0] is step:
            down = pair[1]
            merged.append(
                {
                    **step,
                    "referenceReadings": list(step.get("referenceReadings") or []) + list(down.get("referenceReadings") or []),
                    "sensorSamples": list(step.get("sensorSamples") or []) + list(down.get("sensorSamples") or []),
                    "minutes": (step.get("minutes") or 0) + (down.get("minutes") or 0),
                }
            )
        else:
            merged.append(step)
    return merged
