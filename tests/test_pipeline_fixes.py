"""Le correzioni della pipeline di Mario del passo 3.5 [S §6.2], ciascuna con il suo test.

I test della pipeline di Mario restano in ``pipeline/test`` cosi' come sono; qui stanno i test
delle correzioni, che di quella pipeline cambiano il comportamento di proposito.
"""

from __future__ import annotations

import json
import sys

import numpy as np
import pytest
from numpy.polynomial import Polynomial

from calibration_engine.adapter import function_of
from calibration_engine.formula import evaluate, standard_uncertainty
from calibration_engine.pipeline import pipeline_dir

SCRIPTS = str(pipeline_dir() / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from model_calibration.poly_gum import back_transform, gum_polyfit, poly_fit, scaling  # noqa: E402

# --------------------------------------------------------------------------------------------
# Regressori centrati e scalati nel fit polinomiale
# --------------------------------------------------------------------------------------------


def _dataset(lo: float, hi: float, n: int = 13, noise: float = 0.01):
    """Una curva cubica qualunque sul grezzo [lo, hi], con un po' di rumore riproducibile."""
    x = np.linspace(lo, hi, n)
    s = (x - (lo + hi) / 2) / ((hi - lo) / 2)
    y = 25 + 10 * s - 0.8 * s**2 + 0.3 * s**3 + np.random.default_rng(1).normal(0, noise, n)
    return x, y, np.full(n, 2.0), np.full(n, noise)


def _normal_equations(x: np.ndarray, y: np.ndarray, degree: int) -> np.ndarray:
    """Il fit com'era prima del passo 3.5: potenze del grezzo e inversione di XᵀX."""
    X = np.vander(x, degree + 1, increasing=True)
    return np.linalg.inv(X.T @ X) @ X.T @ y


def _predict(theta: np.ndarray, d: np.ndarray) -> np.ndarray:
    return np.vander(np.atleast_1d(d), len(theta), increasing=True) @ theta


@pytest.mark.parametrize("lo, hi", [(2666, 58668), (30000, 30600), (40000, 41000), (60000, 60300)])
def test_il_fit_centrato_coincide_con_il_fit_stabile(lo: float, hi: float):
    """Su tutta la scala come su una gamma stretta lontana dallo zero."""
    x, y, ux, uy = _dataset(lo, hi)
    theta, _, _ = gum_polyfit(x, y, ux, uy, 3)
    # Polynomial.fit lavora anch'esso su [-1, 1] e risolve con la SVD: e' il riferimento.
    reference = Polynomial.fit(x, y, 3)(x)
    # Sulla gamma piu' stretta a0 vale circa -2e7 e la valutazione sulle potenze di D perde
    # sette cifre delle sedici del double: 1e-7 resta comunque mille volte sotto il rumore.
    assert np.max(np.abs(_predict(theta, x) - reference)) < 1e-7


def test_le_equazioni_normali_sul_grezzo_sbagliavano_su_una_gamma_stretta():
    """Il difetto che il passo 3.5 corregge, documentato: cond(XᵀX) ≈ 4e32 su 60000–60300 LSB."""
    x, y, ux, uy = _dataset(60000, 60300)
    reference = Polynomial.fit(x, y, 3)(x)
    old = _normal_equations(x, y, 3)
    assert np.max(np.abs(_predict(old, x) - reference)) > 1e-3, "le equazioni normali sul grezzo perdono le cifre"
    theta, _, _ = gum_polyfit(x, y, ux, uy, 3)
    assert np.max(np.abs(_predict(theta, x) - reference)) < 1e-7


def test_la_trasformazione_inversa_e_lo_sviluppo_binomiale():
    c, h = scaling(np.array([100.0, 300.0]))
    assert (c, h) == (200.0, 100.0)
    b = np.array([1.0, 2.0, 3.0, 4.0])
    d = np.linspace(80, 320, 7)
    s = (d - c) / h
    assert _predict(back_transform(3, c, h) @ b, d) == pytest.approx(_predict(b, s), rel=1e-12)


def _numerical_covariance(x, y, ux, uy, degree, quantity):
    """Propagazione per differenze finite centrate, con il fit stabile di numpy."""
    def q(xx, yy):
        return quantity(Polynomial.fit(xx, yy, degree))

    rows = []
    for i in range(len(x)):
        dy = 1e-4
        yp, ym = y.copy(), y.copy()
        yp[i] += dy
        ym[i] -= dy
        rows.append(((q(x, yp) - q(x, ym)) / (2 * dy), uy[i]))
        dx = 1e-3
        xp, xm = x.copy(), x.copy()
        xp[i] += dx
        xm[i] -= dx
        rows.append(((q(xp, y) - q(xm, y)) / (2 * dx), ux[i]))
    return sum(np.outer(g, g) * u**2 for g, u in rows)


def test_la_covarianza_e_la_propagazione_dei_dati():
    """Su tutta la scala: la covarianza dei coefficienti, elemento per elemento."""
    x, y, ux, uy = _dataset(2666, 58668)
    _, u_theta, cov = gum_polyfit(x, y, ux, uy, 3)
    expected = _numerical_covariance(x, y, ux, uy, 3, lambda p: p.convert().coef)
    assert cov == pytest.approx(expected, rel=1e-5)
    assert u_theta == pytest.approx(np.sqrt(np.diag(expected)), rel=1e-5)


def _delivered(x, y, ux, uy):
    """La funzione come la consegna il motore: dal risultato della pipeline, con l'adattatore."""
    fit = poly_fit(x, y, ux, uy, 3)
    result = {"model": "cubic", "scaled_fit": fit.scaled(), "coefficients": {f"a{i}": v for i, v in enumerate(fit.theta)}}
    return fit, function_of(result)


@pytest.mark.parametrize("lo, hi", [(2666, 58668), (30000, 30600), (60000, 60300)])
def test_la_funzione_consegnata_da_valori_e_incertezza_giusti(lo: float, hi: float):
    """Valutata come la valuta sensor-manager (formula e gradiente per differenze centrali),
    la funzione centrata da' il polinomio del fit e l'incertezza della propagazione dei dati."""
    x, y, ux, uy = _dataset(lo, hi)
    fit, (formula, c, cov) = _delivered(x, y, ux, uy)
    points = np.linspace(lo, hi, 5)

    values = [evaluate(formula, d, c) for d in points]
    assert values == pytest.approx(Polynomial.fit(x, y, 3)(points), abs=1e-9)

    u = [standard_uncertainty(formula, d, c, cov) for d in points]
    expected = np.sqrt(np.diag(_numerical_covariance(x, y, ux, uy, 3, lambda p: p(points))))
    assert u == pytest.approx(expected, rel=1e-5)


def test_sulle_potenze_del_grezzo_la_covarianza_non_basta_su_una_gamma_stretta():
    """Perche' il motore consegna la forma centrata: su 60000–60300 LSB, gᵀΣg con la
    covarianza sulle potenze di D sbaglia l'incertezza di un ordine di grandezza (0,7 °C
    invece di 0,06 al centro) per pura cancellazione numerica, a fit giusto."""
    x, y, ux, uy = _dataset(60000, 60300)
    fit, (formula, c, cov) = _delivered(x, y, ux, uy)
    centre = 60150.0
    g = np.vander([centre], 4, increasing=True)[0]
    u_raw = np.sqrt(max(0.0, g @ fit.cov_theta @ g))
    u_centred = standard_uncertainty(formula, centre, c, cov)
    assert abs(u_raw - u_centred) / u_centred > 0.01, "la cancellazione sul grezzo si vede"


def test_l_adattatore_legge_il_lineare_di_mario():
    """Mario scrive y = A·D + B: nel blocco del template c[0] e' B e c[1] e' A. Prima del passo
    3.5 l'adattatore cercava a0 e a1, che il lineare non ha."""
    result = json.loads((pipeline_dir() / "test_runs" / "run4_linear_13p" / "last_calibration.json").read_text(encoding="utf-8"))
    k = result["coefficients"]
    formula, c, cov = function_of(result)
    assert formula == "c[0] + c[1] * x"
    assert c == [k["B"], k["A"]]
    assert cov == [[k["u_B"] ** 2, k["cov_AB"]], [k["cov_AB"], k["u_A"] ** 2]]
    assert evaluate(formula, 30000.0, c) == pytest.approx(k["A"] * 30000.0 + k["B"], rel=1e-12)


def test_una_run_archiviata_senza_forma_centrata_resta_sulle_potenze_del_grezzo():
    result = json.loads((pipeline_dir() / "test_runs" / "run5_cubic_13p" / "last_calibration.json").read_text(encoding="utf-8"))
    formula, c, cov = function_of(result)
    assert formula == "c[0] + c[1] * x + c[2] * x**2 + c[3] * x**3"
    assert c == [result["coefficients"][f"a{i}"] for i in range(4)]
    assert cov == result["coefficients"]["cov_theta"]
