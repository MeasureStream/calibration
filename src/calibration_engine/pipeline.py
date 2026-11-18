"""Dove sta la pipeline di Mario e come la si invoca.

La pipeline e' un programma a riga di comando (``pipeline/scripts/analisi_calib_data.py``) che
legge file JSON e scrive file JSON, PDF e XML. In questa fase la si usa cosi' com'e', in un
processo figlio: e' anche la forma che serve al motore, perche' un job bloccato si termina
uccidendo il processo, senza lasciare stato sporco nel consumer [S §6.3].
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Variabile d'ambiente che indica la cartella ``pipeline`` (nell'immagine Docker: /app/pipeline).
PIPELINE_DIR_ENV = "CALIBRATION_PIPELINE_DIR"


def pipeline_dir() -> Path:
    """La cartella della pipeline: dalla variabile d'ambiente, o accanto ai sorgenti."""
    configured = os.environ.get(PIPELINE_DIR_ENV)
    if configured:
        return Path(configured).resolve()
    # src/calibration_engine/pipeline.py -> radice del repository -> pipeline/
    return Path(__file__).resolve().parents[2] / "pipeline"


def orchestrator() -> Path:
    """Lo script che esegue fit, budget, conformita', PDF e DCC XML."""
    return pipeline_dir() / "scripts" / "analisi_calib_data.py"


def command(*args: str) -> list[str]:
    """La riga di comando per eseguire l'orchestratore con l'interprete corrente."""
    return [sys.executable, str(orchestrator()), *args]
