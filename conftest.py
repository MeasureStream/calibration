"""Configurazione comune dei test.

I test di Mario (``pipeline/test``) girano cosi' come li ha lasciati. Al momento del porting
(commit ``eac19a9`` del 9 luglio 2026) quattordici erano gia' rossi nel suo repository, con le
stesse librerie: i test erano fermi a prima delle ultime modifiche degli script. Sono stati
corretti nel passo 3.5; qui resta il meccanismo per dichiararne altri, con il motivo.

Qui sono dichiarati **una volta sola, con il motivo**, senza toccare i file di Mario. Sono
``strict``: se uno torna verde la suite fallisce, cosi' la lista non invecchia in silenzio.
"""

from __future__ import annotations

import pytest

_PIPELINE = "pipeline/test/test_calibration_pipeline.py"

#: Motivo -> test che ne dipendono. Vuoto dal 19 novembre 2026: i quattordici fallimenti
#: ereditati sono corretti nel passo 3.5 (resp_persons con ripiego in generate_dcc_xml, chiavi
#: del budget per punto aggiornate nei test, fixture che chiedono la regolazione). Il
#: meccanismo resta per il prossimo test che dovesse fallire per un motivo noto e scritto.
KNOWN_FAILURES: dict[str, list[str]] = {}

_REASON_BY_NODE = {
    f"{_PIPELINE}::{test}": reason for reason, tests in KNOWN_FAILURES.items() for test in tests
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        reason = _REASON_BY_NODE.get(item.nodeid.replace("\\", "/"))
        if reason:
            item.add_marker(pytest.mark.xfail(reason=f"Noto dal porting: {reason}", strict=True))
