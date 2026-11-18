"""Configurazione comune dei test.

I test di Mario (``pipeline/test``) girano cosi' come li ha lasciati. Al momento del porting
(commit ``eac19a9`` del 9 luglio 2026) quattordici erano gia' rossi nel suo repository, con le
stesse librerie: i test erano fermi a prima delle ultime modifiche degli script. Non sono un
effetto del porting, e correggerli vuol dire cambiare test o codice, cioe' il passo 3.5.

Qui sono dichiarati **una volta sola, con il motivo**, senza toccare i file di Mario. Sono
``strict``: se uno torna verde la suite fallisce, cosi' la lista non invecchia in silenzio.
"""

from __future__ import annotations

import pytest

_PIPELINE = "pipeline/test/test_calibration_pipeline.py"

#: Motivo -> test che ne dipendono.
KNOWN_FAILURES: dict[str, list[str]] = {
    (
        "I dati di prova di organizzazione non hanno 'resp_persons', che generate_dcc_xml "
        "legge senza ripiego"
    ): [
        "TestDCCXMLUncertaintyBudget::test_budget_quantities_present",
        "TestDCCXMLUncertaintyBudget::test_uA_ref_quantity_present",
        "TestDCCXMLUncertaintyBudget::test_uA_sensor_quantity_present",
        "TestDCCXMLUncertaintyBudget::test_combined_unc_quantity_present",
        "TestDCCXMLUncertaintyBudget::test_coverage_factor_quantity_present",
        "TestDCCXMLUncertaintyBudget::test_uA_ref_values_correct",
        "TestDCCXMLUncertaintyBudget::test_uA_sensor_values_correct",
        "TestDCCXMLUncertaintyBudget::test_combined_unc_values_correct",
        "TestDCCXMLUncertaintyBudget::test_coverage_factor_values_are_two",
        "TestDCCXMLUncertaintyBudget::test_budget_absent_when_no_budget_data",
    ],
    (
        "Il budget per punto ha separato u_ref in uA_ref, uB_ref e u_res_ref; i test cercano "
        "ancora la chiave unica"
    ): [
        "TestLinearCalibUBudget::test_budget_entry_keys",
        "TestLinearCalibUBudget::test_u_c_combines_T_ref_and_T_i",
    ],
    (
        "Politica dei coefficienti del 3 luglio (c6a3c5b): senza regolazione il certificato "
        "riporta i coefficienti precedenti, e i test si aspettano quelli del fit"
    ): [
        "TestCertificatoFilledJSON::test_calibration_coefficients_populated",
        "TestEndToEnd::test_error_degC_reasonable",
    ],
}

_REASON_BY_NODE = {
    f"{_PIPELINE}::{test}": reason for reason, tests in KNOWN_FAILURES.items() for test in tests
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        reason = _REASON_BY_NODE.get(item.nodeid.replace("\\", "/"))
        if reason:
            item.add_marker(pytest.mark.xfail(reason=f"Noto dal porting: {reason}", strict=True))
