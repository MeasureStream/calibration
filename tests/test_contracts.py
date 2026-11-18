"""Gli esempi dei contratti si leggono e si riscrivono identici.

Gli stessi file sono in metrology-service/contracts, e il test gemello in Kotlin fa la stessa
cosa: se i due lati smettono di capirsi, uno dei due test diventa rosso.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from calibration_engine.contracts import (
    CalibrationFunction,
    CalibrationJob,
    CalibrationResult,
    ItemResult,
    JobKind,
    JobStatus,
)

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"

EXAMPLES = {
    "calibration-job.example.json": CalibrationJob,
    "calibration-result.example.json": CalibrationResult,
    "calibration-result.failed.example.json": CalibrationResult,
}


@pytest.mark.parametrize("name, model", list(EXAMPLES.items()))
def test_l_esempio_fa_andata_e_ritorno_senza_perdite(name, model):
    wire = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    assert model.model_validate(wire).to_wire() == wire


def test_il_risultato_di_esempio_porta_la_formula_sotto_la_chiave_del_tipo():
    wire = json.loads((CONTRACTS / "calibration-result.example.json").read_text(encoding="utf-8"))
    result = CalibrationResult.model_validate(wire)

    function = result.items[0].function
    assert function is not None
    assert function.type == "cubic_lsb"
    assert function.formula == "c[0] + c[1] * x + c[2] * x**2 + c[3] * x**3"
    assert [c.id for c in function.c] == [0, 1, 2, 3]
    assert result.status is JobStatus.DONE
    assert result.kind is JobKind.SESSION_FIT


def test_una_funzione_senza_formula_e_rifiutata():
    with pytest.raises(ValidationError, match="manca la formula"):
        CalibrationFunction.model_validate({"type": "cubic_lsb", "input": ["x"], "c": []})


def test_la_funzione_none_non_chiede_formula():
    assert CalibrationFunction.model_validate({"type": "none"}).formula is None


def test_la_covarianza_deve_avere_la_dimensione_dei_coefficienti():
    function = {"type": "lin", "lin": "c[0] + c[1] * x", "c": [{"id": 0, "value": 1.0}, {"id": 1, "value": 2.0}]}
    with pytest.raises(ValidationError, match="2x2"):
        ItemResult.model_validate(
            {"calibratedItemId": 1, "muExtendedId": 1, "sensorIndex": 0, "function": function, "covariance": [[1.0]]}
        )


def test_un_job_fallito_deve_dire_perche():
    wire = json.loads((CONTRACTS / "calibration-result.failed.example.json").read_text(encoding="utf-8"))
    wire["error"] = None
    with pytest.raises(ValidationError, match="FAILED"):
        CalibrationResult.model_validate(wire)


def test_un_campo_sconosciuto_non_rompe_la_lettura():
    """Un campo nuovo aggiunto dall'altro lato prima di questo deve passare inosservato."""
    wire = json.loads((CONTRACTS / "calibration-job.example.json").read_text(encoding="utf-8"))
    wire["campoDelFuturo"] = 1
    assert CalibrationJob.model_validate(wire).kind is JobKind.SESSION_FIT
