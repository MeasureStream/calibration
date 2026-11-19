"""Le formule di taratura come le valuta sensor-manager, e le funzioni che il motore costruisce.

Il valutatore e' il gemello di ``template/FormulaEvaluator.kt`` di sensor-manager: stessa
grammatica chiusa (numeri, la variabile d'ingresso, ``c[i]``, le quattro operazioni, ``**`` e
``^`` associativi a destra, il meno unario che lega piu' forte della potenza, le funzioni
``ln``, ``log``, ``exp``, ``sqrt``, ``abs``). Serve al motore per due cose: provare che la
funzione consegnata al server dia i valori del fit, e valutare una funzione di taratura sui
punti di verifica (job VERIFICATION) o su quelli di un certificato esterno (CERT_FIT).

Anche il gradiente rispetto ai coefficienti e' quello del server: differenze centrali con passo
relativo al coefficiente, cosi' gᵀΣg calcolata qui e' quella che sensor-manager calcolera'.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

_FUNCTIONS: dict[str, Callable[[float], float]] = {
    "ln": math.log,
    "log": math.log10,
    "exp": math.exp,
    "sqrt": math.sqrt,
    "abs": abs,
}


class FormulaError(ValueError):
    """La formula non e' valutabile: sintassi, funzione sconosciuta, coefficiente mancante."""


class _Parser:
    def __init__(self, text: str, x: float, c: Sequence[float], variable: str) -> None:
        self.text, self.x, self.c, self.variable = text, x, c, variable
        self.pos = 0

    def parse(self) -> float:
        value = self.expression()
        self.skip()
        if self.pos < len(self.text):
            raise FormulaError(f"carattere inatteso a {self.pos}: '{self.text[self.pos]}'")
        return value

    def expression(self) -> float:
        value = self.term()
        while True:
            if self.consume("+"):
                value = value + self.term()
            elif self.consume("-"):
                value = value - self.term()
            else:
                return value

    def term(self) -> float:
        value = self.power()
        while True:
            self.skip()
            if self.peek("*") and not self.text.startswith("**", self.pos):
                self.pos += 1
                value = value * self.power()
            elif self.peek("/"):
                self.pos += 1
                value = value / self.power()
            else:
                return value

    def power(self) -> float:
        base = self.unary()
        self.skip()
        if self.text.startswith("**", self.pos):
            self.pos += 2
            return _pow(base, self.power())
        if self.consume("^"):
            return _pow(base, self.power())
        return base

    def unary(self) -> float:
        if self.consume("-"):
            return -self.unary()
        if self.consume("+"):
            return self.unary()
        return self.primary()

    def primary(self) -> float:
        self.skip()
        if self.pos >= len(self.text):
            raise FormulaError("formula troncata")
        if self.consume("("):
            value = self.expression()
            if not self.consume(")"):
                raise FormulaError("parentesi non chiusa")
            return value
        ch = self.text[self.pos]
        if ch.isdigit() or ch == ".":
            return self.number()
        if ch.isalpha() or ch == "_":
            return self.identifier()
        raise FormulaError(f"carattere non ammesso: '{ch}'")

    def number(self) -> float:
        start = self.pos
        while self.pos < len(self.text) and (self.text[self.pos].isdigit() or self.text[self.pos] == "."):
            self.pos += 1
        if self.pos < len(self.text) and self.text[self.pos] in "eE":
            mark = self.pos
            self.pos += 1
            if self.pos < len(self.text) and self.text[self.pos] in "+-":
                self.pos += 1
            if self.pos < len(self.text) and self.text[self.pos].isdigit():
                while self.pos < len(self.text) and self.text[self.pos].isdigit():
                    self.pos += 1
            else:
                self.pos = mark
        literal = self.text[start : self.pos]
        try:
            return float(literal)
        except ValueError as e:
            raise FormulaError(f"numero non valido: {literal}") from e

    def identifier(self) -> float:
        start = self.pos
        while self.pos < len(self.text) and (self.text[self.pos].isalnum() or self.text[self.pos] == "_"):
            self.pos += 1
        name = self.text[start : self.pos]
        self.skip()
        if name == "c":
            if not self.consume("["):
                raise FormulaError("atteso '[' dopo c")
            index = int(self.expression())
            if not self.consume("]"):
                raise FormulaError("parentesi quadra non chiusa")
            if not 0 <= index < len(self.c):
                raise FormulaError(f"coefficiente c[{index}] non dichiarato")
            return float(self.c[index])
        if name == self.variable:
            return self.x
        function = _FUNCTIONS.get(name)
        if function is None:
            raise FormulaError(f"funzione o variabile sconosciuta: {name}")
        if not self.consume("("):
            raise FormulaError(f"attesa '(' dopo {name}")
        argument = self.expression()
        if not self.consume(")"):
            raise FormulaError(f"parentesi non chiusa dopo {name}")
        try:
            return function(argument)
        except (ValueError, OverflowError):
            return math.nan

    def peek(self, expected: str) -> bool:
        return self.pos < len(self.text) and self.text[self.pos] == expected

    def consume(self, expected: str) -> bool:
        self.skip()
        if self.peek(expected):
            self.pos += 1
            return True
        return False

    def skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1


def _pow(base: float, exponent: float) -> float:
    try:
        result = base**exponent
    except (OverflowError, ZeroDivisionError):
        return math.nan
    return result if isinstance(result, float | int) else math.nan


def evaluate(formula: str, x: float, c: Sequence[float], variable: str = "x") -> float | None:
    """Il valore della formula; None se non e' un numero finito, come sul server."""
    try:
        result = _Parser(formula, float(x), c, variable).parse()
    except ZeroDivisionError:
        return None
    return result if math.isfinite(result) else None


def gradient(formula: str, x: float, c: Sequence[float], variable: str = "x") -> list[float] | None:
    """∂f/∂c_i per differenze centrali, con il passo relativo al coefficiente del server."""
    out = []
    for i, ci in enumerate(c):
        h = abs(ci) * 1e-6 if ci != 0.0 else 1e-12
        up = list(c)
        down = list(c)
        up[i] = ci + h
        down[i] = ci - h
        f_up = evaluate(formula, x, up, variable)
        f_down = evaluate(formula, x, down, variable)
        if f_up is None or f_down is None:
            return None
        out.append((f_up - f_down) / (2 * h))
    return out


def standard_uncertainty(formula: str, x: float, c: Sequence[float], covariance: Sequence[Sequence[float]], variable: str = "x") -> float | None:
    """u della funzione per i soli coefficienti, √(gᵀΣg): il contributo «taratura» del server."""
    g = gradient(formula, x, c, variable)
    if g is None:
        return None
    n = len(g)
    value = sum(g[i] * covariance[i][j] * g[j] for i in range(n) for j in range(n))
    return math.sqrt(max(0.0, value))


def _literal(value: float) -> str:
    """Un numero che il valutatore del server rilegge identico (repr e' la forma piu' corta)."""
    return repr(float(value))


def centered_polynomial(degree: int, center: float, half_range: float, variable: str = "x") -> str:
    """``c[0] + c[1] * s + … + c[n] * s**n`` con ``s = ((x - centro) / semiampiezza)``.

    Centro e semiampiezza sono costanti della formula, non coefficienti: non hanno incertezza
    e non entrano nella covarianza, che e' quella dei soli c[i] nella variabile centrata.
    """
    if half_range <= 0:
        raise ValueError("la semiampiezza deve essere positiva")
    sign, magnitude = ("-", center) if center >= 0 else ("+", -center)
    s = f"(({variable} {sign} {_literal(magnitude)}) / {_literal(half_range)})"
    terms = ["c[0]"]
    for k in range(1, degree + 1):
        terms.append(f"c[{k}] * {s}" if k == 1 else f"c[{k}] * {s}**{k}")
    return " + ".join(terms)
