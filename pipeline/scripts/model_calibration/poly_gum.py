"""Fit polinomiale ai minimi quadrati con propagazione GUM, su regressori centrati e scalati.

Passo 3.5 del piano metrologico [S §6.2]. Il fit quadratico e quello cubico costruivano la
matrice di regressione sulle potenze del grezzo (D, D², D³ con D fino a 65535) e invertivano
XᵀX: sulla run 5 di riferimento cond(XᵀX) ≈ 1,3·10³⁰, ben oltre 1/eps ≈ 4,5·10¹⁵, cioè un
calcolo che nessuna garanzia numerica copre. Che lì i coefficienti escano comunque giusti
(2·10⁻¹³ dal fit stabile) dipende dal fatto che il mal condizionamento è quasi tutto di scala
tra le colonne; su una gamma stretta lontana dallo zero le colonne diventano quasi collineari
e l'inversione perde davvero le cifre: su 60000–60300 LSB le previsioni sbagliavano di decine
di gradi.

Qui il fit si fa nella variabile

    s = (D − c) / h,   c = centro dei punti,   h = semiampiezza dei punti,

così s ∈ [−1, 1] e cond(SᵀS) resta dell'ordine delle decine. Coefficienti e covarianza si
riportano poi alle potenze di D con la matrice J dello sviluppo binomiale:

    a = J b,   Σ_a = J Σ_b Jᵀ,   J[j, k] = C(k, j) · (−c)^(k−j) / h^k   per k ≥ j.

Le uscite storiche (a0…an sulle potenze di D) restano quindi con lo stesso significato: PDF e
DCC non cambiano. Su una gamma stretta, però, i coefficienti sulle potenze di D sono enormi e
fortemente correlati, e gᵀΣ_a g perde cifre per cancellazione (0,4 % su 30000–30600 LSB): per
questo il risultato espone anche la forma centrata (c, h, b, Σ_b), ed è quella che il motore
consegna al server (vedi ``calibration_engine.adapter``).

Trattare c e h come costanti nella propagazione è esatto e non un'approssimazione: il
polinomio dei minimi quadrati non dipende dalla scelta di c e h (lo spazio dei polinomi di
grado n è lo stesso), quindi la derivata dei coefficienti rispetto a un dato coincide con
quella calcolata a c e h fissati.

La propagazione è quella di Mario, con incertezza su entrambe le variabili: per ogni punto
la derivata dei coefficienti rispetto a y_i e a D_i (attraverso s_i, con ∂s/∂D = 1/h).
"""

from __future__ import annotations

from dataclasses import dataclass
from math import comb
from typing import Any, Dict, Tuple

import numpy as np


@dataclass(frozen=True)
class PolyFit:
    """Lo stesso polinomio in due forme: sulle potenze di D e sulla variabile centrata s."""

    degree: int
    center: float
    half_range: float
    b: np.ndarray
    cov_b: np.ndarray
    theta: np.ndarray
    cov_theta: np.ndarray

    @property
    def u_theta(self) -> np.ndarray:
        return np.sqrt(np.maximum(0.0, np.diag(self.cov_theta)))

    def scaled(self) -> Dict[str, Any]:
        """La forma centrata, da scrivere nel risultato accanto ai coefficienti storici."""
        return {
            "variable": "s = (D - center) / half_range",
            "center": float(self.center),
            "half_range": float(self.half_range),
            "b": [float(v) for v in self.b],
            "cov_b": [[float(v) for v in row] for row in self.cov_b],
        }


def scaling(x: np.ndarray) -> Tuple[float, float]:
    """Centro e semiampiezza dei punti; h = 1 se i punti coincidono (fit comunque degenere)."""
    x = np.asarray(x, dtype=float)
    lo, hi = float(np.min(x)), float(np.max(x))
    c = (hi + lo) / 2.0
    h = (hi - lo) / 2.0
    return c, (h if h > 0.0 else 1.0)


def back_transform(degree: int, c: float, h: float) -> np.ndarray:
    """La matrice J che porta i coefficienti in s ai coefficienti sulle potenze di D."""
    n = degree + 1
    J = np.zeros((n, n))
    for j in range(n):
        for k in range(j, n):
            J[j, k] = comb(k, j) * (-c) ** (k - j) / h**k
    return J


def _rows(s: np.ndarray, degree: int) -> Tuple[np.ndarray, np.ndarray]:
    """Le righe del modello in s e le loro derivate rispetto a s."""
    n = degree + 1
    S = np.vander(s, n, increasing=True)
    G = np.zeros_like(S)
    for k in range(1, n):
        G[:, k] = k * s ** (k - 1)
    return S, G


def poly_fit(
    x: np.ndarray,
    y: np.ndarray,
    u_x: np.ndarray,
    u_y: np.ndarray,
    degree: int,
) -> PolyFit:
    """Il polinomio di grado ``degree`` dei minimi quadrati, con la sua covarianza GUM."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    u_x = np.asarray(u_x, dtype=float)
    u_y = np.asarray(u_y, dtype=float)

    c, h = scaling(x)
    s = (x - c) / h
    S, G = _rows(s, degree)

    StS = S.T @ S
    try:
        StS_inv = np.linalg.inv(StS)
    except np.linalg.LinAlgError:
        StS_inv = np.linalg.pinv(StS)
    b = StS_inv @ S.T @ y

    n = degree + 1
    cov_b = np.zeros((n, n))
    for i in range(len(x)):
        s_row, g_row = S[i], G[i]
        db_dy = StS_inv @ s_row
        db_ds = StS_inv @ (g_row * y[i] - (np.outer(s_row, g_row) + np.outer(g_row, s_row)) @ b)
        cov_b += np.outer(db_dy, db_dy) * u_y[i] ** 2
        cov_b += np.outer(db_ds, db_ds) * (u_x[i] / h) ** 2

    J = back_transform(degree, c, h)
    cov_theta = J @ cov_b @ J.T
    return PolyFit(
        degree=degree,
        center=c,
        half_range=h,
        b=b,
        cov_b=cov_b,
        theta=J @ b,
        cov_theta=(cov_theta + cov_theta.T) / 2.0,
    )


def gum_polyfit(
    x: np.ndarray,
    y: np.ndarray,
    u_x: np.ndarray,
    u_y: np.ndarray,
    degree: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Coefficienti, incertezze tipo e covarianza sulle potenze di D (``theta[k]`` · D^k)."""
    fit = poly_fit(x, y, u_x, u_y, degree)
    return fit.theta, fit.u_theta, fit.cov_theta
