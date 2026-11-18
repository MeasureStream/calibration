"""Il porting non cambia i numeri.

Mario ha lasciato in ``pipeline/test_runs`` le uscite di alcune run complete. Rieseguire la
pipeline sugli stessi ingressi, con le librerie di questo progetto, deve restituire gli stessi
coefficienti, la stessa covarianza e le stesse incertezze estese. E' la prova che spostare il
codice e cambiare versioni di numpy e scipy non ha toccato il calcolo; quando il passo 3.5
cambiera' la pipeline di proposito, questo test dira' esattamente quali numeri si sono mossi.

La tolleranza relativa di 1e-9 lascia spazio solo alle differenze di arrotondamento fra
piattaforme (BLAS diverse su Windows e Linux), non a un cambiamento di metodo.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from calibration_engine.pipeline import command, pipeline_dir

PIPELINE = pipeline_dir()
REL = 1e-9

#: Run di riferimento ricostruibili: tutte sul payload simulato a 13 punti, con regolazione.
RUNS = {
    "run4_linear_13p": "linear",
    "run6_quadratic_13p": "quadratic",
    "run5_cubic_13p": "cubic",
}


def _run_pipeline(procedure: str, out: Path) -> dict:
    result_json = out / "last_calibration.json"
    completed = subprocess.run(
        command(
            "--input", str(PIPELINE / "data_in" / "simulated_13p.json"),
            "--sensor", str(PIPELINE / "models_in" / "sensors" / "ntc_temperature.json"),
            "--ref", str(PIPELINE / "models_in" / "references" / "fluke_9142.json"),
            "--procedure", procedure,
            "--update-parameters", "always",
            "--cert-output", str(out / "cert.json"),
            "--pdf", str(out / "cert.pdf"),
            "--xml", str(out / "cert.xml"),
            "--result-calibration", str(result_json),
            "--conformity-output", str(out / "conformity.json"),
            "--no-charts",
            "--no-verbose",
        ),
        cwd=PIPELINE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert result_json.is_file(), "la pipeline non ha scritto il risultato"
    return json.loads(result_json.read_text(encoding="utf-8"))


def _numbers(value):
    """Appiattisce dizionari e liste annidati in una lista di numeri, in ordine."""
    if isinstance(value, dict):
        return [n for key in sorted(value) for n in _numbers(value[key])]
    if isinstance(value, list):
        return [n for item in value for n in _numbers(item)]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)]
    return []


@pytest.mark.parametrize("run, procedure", list(RUNS.items()))
def test_la_pipeline_riproduce_la_run_di_riferimento(run: str, procedure: str, tmp_path: Path):
    expected = json.loads((PIPELINE / "test_runs" / run / "last_calibration.json").read_text(encoding="utf-8"))
    actual = _run_pipeline(procedure, tmp_path)

    assert actual["model"] == expected["model"]
    for key in ("coefficients", "expanded_uncertainties"):
        assert _numbers(actual[key]) == pytest.approx(_numbers(expected[key]), rel=REL), key
