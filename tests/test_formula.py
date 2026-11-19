"""Il valutatore delle formule e' il gemello di quello di sensor-manager.

Gli stessi casi di ``SensorConversionTest`` di sensor-manager: se qui e la' una formula desse
due valori diversi, il motore proverebbe una funzione e il server ne applicherebbe un'altra.
"""

from __future__ import annotations

import math

import pytest

from calibration_engine.formula import FormulaError, centered_polynomial, evaluate, gradient, standard_uncertainty


def test_precedenza_e_potenza():
    assert evaluate("1 + 2 * 3", 0.0, []) == 7.0
    assert evaluate("(1 + 2) * 3", 0.0, []) == 9.0
    assert evaluate("2 ** 3", 0.0, []) == 8.0
    assert evaluate("2 ^ 3", 0.0, []) == 8.0
    assert evaluate("-2 ** 3", 0.0, []) == -8.0
    assert evaluate("2 ** 3 ** 2", 0.0, []) == 512.0, "associativa a destra"
    assert evaluate("1 / 2", 0.0, []) == 0.5


def test_il_meno_unario_lega_piu_della_potenza_come_sul_server():
    """In Python -2**2 vale -4; nella grammatica del server vale 4. Conta quella del server."""
    assert evaluate("-2 ** 2", 0.0, []) == 4.0


def test_variabile_coefficienti_e_funzioni():
    assert evaluate("c[0] + c[1] * x", 10.0, [1.0, 2.0]) == 21.0
    assert evaluate("c[0] + c[1] * d_in", 10.0, [1.0, 2.0], variable="d_in") == 21.0
    ntc = "1.0 / (c[0] + c[1] * ln(4095.0 / x - 1.0) + c[2] * ln(4095.0 / x - 1.0)**3)"
    c = [1.1e-3, 2.4e-4, 7.0e-8]
    expected = 1.0 / (c[0] + c[1] * math.log(4095.0 / 2000.0 - 1.0) + c[2] * math.log(4095.0 / 2000.0 - 1.0) ** 3)
    assert evaluate(ntc, 2000.0, c) == pytest.approx(expected, rel=1e-15)


def test_risultati_non_finiti_e_formule_sbagliate():
    assert evaluate("1 / x", 0.0, []) is None
    assert evaluate("ln(x)", -1.0, []) is None
    with pytest.raises(FormulaError):
        evaluate("c[3]", 0.0, [1.0])
    with pytest.raises(FormulaError):
        evaluate("os.system(1)", 0.0, [])
    with pytest.raises(FormulaError):
        evaluate("(1 + 2", 0.0, [])


def test_la_funzione_centrata():
    formula = centered_polynomial(3, 60150.0, 150.0)
    assert formula == (
        "c[0] + c[1] * ((x - 60150.0) / 150.0) + c[2] * ((x - 60150.0) / 150.0)**2"
        " + c[3] * ((x - 60150.0) / 150.0)**3"
    ), "la stessa stringa che SensorConversionTest valuta in sensor-manager"
    c = [25.0, 10.0, -0.8, 0.3]
    assert evaluate(formula, 60300.0, c) == pytest.approx(34.5, abs=1e-12)
    assert evaluate(formula, 60000.0, c) == pytest.approx(13.9, abs=1e-12)
    assert gradient(formula, 60225.0, c) == pytest.approx([1.0, 0.5, 0.25, 0.125], rel=1e-6)
    assert centered_polynomial(1, -1500.0, 0.25) == "c[0] + c[1] * ((x + 1500.0) / 0.25)"


def test_l_incertezza_dei_soli_coefficienti():
    formula = "c[0] + c[1] * x"
    cov = [[0.04, 0.0], [0.0, 1e-6]]
    assert standard_uncertainty(formula, 100.0, [1.0, 2.0], cov) == pytest.approx(math.sqrt(0.04 + 1e-6 * 100.0**2), rel=1e-6)
