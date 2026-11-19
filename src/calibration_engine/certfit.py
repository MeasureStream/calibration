"""La funzione di un esemplare dai punti di un certificato esterno: il job CERT_FIT (passo 5.3).

Un laboratorio esterno tara lo strumento cosi' com'e': confronta l'**indicazione** (il valore che
lo strumento mostra, cioe' il grezzo convertito con la funzione che allora era in vigore, di
norma la formula nominale del modello) con il **riferimento**. Il certificato da' quindi coppie
(indicazione y, riferimento r), non il grezzo. La funzione dell'esemplare si costruisce sopra
quella nominale:

    f(x) = F(x) + c0 + c1·F(x) [+ c2·F(x)²]

dove F e' la formula nominale con i suoi coefficienti scritti come costanti, e la correzione
r − y = c0 + c1·y (+ c2·y²) si stima ai minimi quadrati pesati con le incertezze dei punti.
I coefficienti c[] della funzione sono quindi solo quelli della correzione, e la loro covarianza
e' quella che sensor-manager propaga con gᵀΣg.

Il grado: lineare fino a quattro punti (una correzione quadratica su pochi punti insegue il
rumore), quadratico da cinque. Si usano i punti AS_LEFT del certificato, cioe' lo stato in cui
lo strumento e' uscito dal laboratorio; se il certificato non distingue le fasi, tutti.
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np

_COEFFICIENT = re.compile(r"c\[(\d+)\]")


class CertFitError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def inline(formula: str, coefficients: list[float]) -> str:
    """La formula con ogni ``c[i]`` sostituito dal suo valore, fra parentesi."""

    def value(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(coefficients):
            raise CertFitError("NOMINAL_FORMULA", f"la formula nominale cita c[{index}], che il template non dichiara")
        return f"({float(coefficients[index])!r})"

    return _COEFFICIENT.sub(value, formula)


def certificate_pairs(points: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """Indicazioni, riferimenti e incertezze tipo dei punti utilizzabili."""
    as_left = [p for p in points if p.get("phase") == "AS_LEFT"]
    chosen = [p for p in (as_left or points) if p.get("indicatedValue") is not None and p.get("referenceValue") is not None]
    if len(chosen) < 2:
        raise CertFitError("TOO_FEW_POINTS", "servono almeno due punti con indicazione e riferimento")
    y = np.array([float(p["indicatedValue"]) for p in chosen])
    r = np.array([float(p["referenceValue"]) for p in chosen])
    us = [float(p["expandedU"]) / float(p.get("k") or 2.0) if p.get("expandedU") else None for p in chosen]
    u = np.array(us, dtype=float) if all(v is not None and v > 0 for v in us) else None
    return y, r, u


def fit_correction(y: np.ndarray, r: np.ndarray, u: np.ndarray | None, degree: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Coefficienti, covarianza e residui della correzione r − y = Σ c_k·y^k.

    Con le incertezze dei punti la covarianza e' (XᵀWX)⁻¹ con W = 1/u² (GUM: le incertezze sono
    date, non stimate dai residui). Senza, i punti pesano uguale e la covarianza si stima dai
    residui, se restano gradi di liberta'; altrimenti non c'e'.
    """
    X = np.vander(y, degree + 1, increasing=True)
    target = r - y
    weights = 1.0 / u**2 if u is not None else np.ones_like(y)
    XtW = X.T * weights
    try:
        inverse = np.linalg.inv(XtW @ X)
    except np.linalg.LinAlgError as e:
        raise CertFitError("DEGENERATE_POINTS", "i punti del certificato non bastano a stimare la correzione (indicazioni coincidenti?)") from e
    c = inverse @ XtW @ target
    residuals = target - X @ c
    if u is None:
        dof = len(y) - (degree + 1)
        if dof <= 0:
            return c, np.full((degree + 1, degree + 1), np.nan), residuals
        inverse = inverse * float(residuals @ residuals) / dof
    return c, inverse, residuals


def function_block(nominal: dict[str, Any], c: np.ndarray, dsi: str | None) -> dict[str, Any]:
    """Il blocco ``calibration`` della funzione dell'esemplare, sopra la formula nominale."""
    kind = nominal.get("type")
    formula = nominal.get(kind) if kind else None
    if not isinstance(formula, str):
        raise CertFitError("NO_NOMINAL", "il template non ha una formula nominale su cui costruire la correzione")
    declared = nominal.get("c") or [{"id": i, "value": v} for i, v in enumerate(nominal.get("coefficients") or [])]
    values = [float(e.get("value", 0.0)) for e in sorted(declared, key=lambda e: e.get("id", 0))]
    F = f"({inline(formula, values)})"
    terms = [F, "c[0]", f"c[1] * {F}"]
    if len(c) > 2:
        terms.append(f"c[2] * {F}**2")
    name = f"certfit_{kind}"
    return {
        "type": name,
        name: " + ".join(terms),
        "input": nominal.get("input") or ["x"],
        "c": [{"id": i, "value": float(v), "dsi": dsi} for i, v in enumerate(c)],
    }


def degree_for(points: int) -> int:
    return 1 if points < 5 else 2
