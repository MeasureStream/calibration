"""Il campione di riferimento: correzioni e incertezza dal suo certificato in vigore.

Passo 3.5 del piano, criticita' M2 [S §6.2]. Nella pipeline di Mario il campione era un file
JSON con un'incertezza di targa scritta a mano; ora e' un esemplare con un certificato, e la
sessione cita quel certificato. Qui si fanno tre cose con i suoi punti:

- **validita'**: un certificato scaduto alla data dei gradini ferma il job
  (``REFERENCE_EXPIRED``). metrology-service lo controlla gia' all'apertura della sessione; il
  motore lo ricontrolla sui dati, perche' una sessione puo' restare aperta oltre la scadenza;
- **correzione**: ogni lettura del campione si corregge con la correzione del certificato,
  interpolata linearmente sul valore indicato. Fuori dal campo tarato non si estrapola: oltre
  un margine del 2 % dell'ampiezza del campo il job si ferma (``REFERENCE_OUT_OF_RANGE``), entro
  il margine si usa il punto estremo;
- **incertezza**: l'incertezza tipo del campione e' U/k del certificato nel punto, presa come
  il massimo dei due punti che lo racchiudono (scelta conservativa: l'interpolazione lineare
  di un'incertezza la sottostimerebbe dove il laboratorio non ha misurato).

Dei punti del certificato si usano quelli AS_LEFT, cioe' lo stato del campione dopo la sua
taratura; se il certificato non distingue le fasi, tutti.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

#: Margine oltre il campo tarato del campione, come frazione della sua ampiezza.
RANGE_MARGIN = 0.02


class ReferenceStandardError(Exception):
    """Il campione non si puo' usare per questi dati."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReferencePoint:
    indicated: float
    correction: float
    u: float | None


def certificate_points(certificate: dict[str, Any]) -> list[ReferencePoint]:
    """I punti del certificato come (indicato, correzione, u), ordinati sull'indicato."""
    points = certificate.get("points") or []
    as_left = [p for p in points if p.get("phase") == "AS_LEFT"]
    out: list[ReferencePoint] = []
    for p in as_left or points:
        indicated, reference = p.get("indicatedValue"), p.get("referenceValue")
        correction = p.get("correction")
        if correction is None and p.get("error") is not None:
            correction = -float(p["error"])
        if correction is None and indicated is not None and reference is not None:
            correction = float(reference) - float(indicated)
        if indicated is None and reference is not None and correction is not None:
            indicated = float(reference) - float(correction)
        if indicated is None or correction is None:
            continue
        expanded, k = p.get("expandedU"), p.get("k") or 2.0
        out.append(ReferencePoint(float(indicated), float(correction), float(expanded) / float(k) if expanded is not None else None))
    return sorted(out, key=lambda r: r.indicated)


def correction_at(points: list[ReferencePoint], reading: float) -> tuple[float, float | None]:
    """Correzione e incertezza tipo del campione per una lettura."""
    if len(points) < 2:
        raise ReferenceStandardError("REFERENCE_CERTIFICATE_POINTS", "il certificato del campione ha meno di due punti: non si puo' interpolare")
    low, high = points[0].indicated, points[-1].indicated
    margin = RANGE_MARGIN * (high - low)
    if reading < low - margin or reading > high + margin:
        raise ReferenceStandardError(
            "REFERENCE_OUT_OF_RANGE",
            f"lettura del campione {reading:g} fuori dal campo del suo certificato [{low:g}, {high:g}]",
        )
    if reading <= low:
        return points[0].correction, points[0].u
    if reading >= high:
        return points[-1].correction, points[-1].u
    for a, b in zip(points, points[1:], strict=False):
        if a.indicated <= reading <= b.indicated:
            span = b.indicated - a.indicated
            w = (reading - a.indicated) / span if span > 0 else 0.0
            us = [u for u in (a.u, b.u) if u is not None]
            return (1 - w) * a.correction + w * b.correction, (max(us) if us else None)
    raise AssertionError("punti non ordinati")  # pragma: no cover


def _instant(value: Any, end_of_day: bool = False) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if len(text) == 10:
        # Solo la data: una scadenza vale fino alla fine di quel giorno.
        try:
            return datetime.combine(date.fromisoformat(text), datetime.max.time() if end_of_day else datetime.min.time(), tzinfo=UTC)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def check_valid(certificate: dict[str, Any], steps: list[dict[str, Any]]) -> None:
    """Ferma il job se il certificato del campione era scaduto quando si e' misurato."""
    valid_until = _instant(certificate.get("validUntil"), end_of_day=True)
    if valid_until is None:
        return
    measured = [t for t in (_instant(s.get("startedAt")) for s in steps) if t is not None]
    if measured and max(measured) > valid_until:
        raise ReferenceStandardError(
            "REFERENCE_EXPIRED",
            f"il certificato {certificate.get('number')} del campione e' scaduto il {valid_until.date()}, "
            f"prima dell'ultimo gradino ({max(measured).date()})",
        )


def correct_steps(steps: list[dict[str, Any]], certificate: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """I gradini con le letture del campione corrette, e il riepilogo per il budget.

    Senza certificato i gradini restano come sono e il riepilogo lo dice: e' il caso delle run
    di Mario e di un campione registrato senza certificato, che metrology-service segnala.
    """
    if not certificate:
        return steps, {"fromCertificate": False}
    check_valid(certificate, steps)
    points = certificate_points(certificate)
    corrected: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    for step in steps:
        readings = [float(r) for r in step.get("referenceReadings") or []]
        if not readings:
            corrected.append(step)
            continue
        mean = sum(readings) / len(readings)
        corrections = [correction_at(points, r)[0] for r in readings]
        _, u = correction_at(points, mean)
        corrected.append({**step, "referenceReadings": [r + c for r, c in zip(readings, corrections, strict=True)], "referenceU": u})
        applied.append(
            {
                "phase": step.get("phase"),
                "stepIndex": step.get("stepIndex"),
                "setpoint": step.get("setpoint"),
                "meanReading": mean,
                "correction": sum(corrections) / len(corrections),
                "uCertificate": u,
            }
        )
    return corrected, {
        "fromCertificate": True,
        "certificate": {k: certificate.get(k) for k in ("id", "number", "revision", "issuer", "calibrationDate", "validUntil")},
        "corrections": applied,
    }
