"""Prova di memoria: quanto pesa un job vero sulla macchina che lo esegue.

La specifica rimanda alle misure la scelta fra la Pi e un'altra macchina [S §6.3]. Questa prova
esegue la pipeline nel caso piu' pesante che il motore fara' davvero — fit cubico su 13 punti,
grafici, PDF e DCC XML — in un processo figlio, come lo eseguira' il consumer, e riporta il picco
di memoria residente del figlio e la durata.

Il picco si legge con ``getrusage(RUSAGE_CHILDREN)``, che esiste solo su Linux e macOS: e' la
misura che serve, perche' e' sulla Pi (o sulla macchina di destinazione) che va fatta.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from calibration_engine.pipeline import command, pipeline_dir


@dataclass(frozen=True)
class ProbeResult:
    seconds: float
    peak_rss_mib: float | None
    returncode: int


def run_probe(procedure: str = "cubic", charts: bool = True) -> ProbeResult:
    pipeline = pipeline_dir()
    with tempfile.TemporaryDirectory(prefix="memory-probe-") as tmp:
        out = Path(tmp)
        args = [
            "--input", str(pipeline / "data_in" / "simulated_13p.json"),
            "--sensor", str(pipeline / "models_in" / "sensors" / "ntc_temperature.json"),
            "--ref", str(pipeline / "models_in" / "references" / "fluke_9142.json"),
            "--procedure", procedure,
            "--update-parameters", "always",
            "--cert-output", str(out / "cert.json"),
            "--pdf", str(out / "cert.pdf"),
            "--xml", str(out / "cert.xml"),
            "--result-calibration", str(out / "result.json"),
            "--conformity-output", str(out / "conformity.json"),
            "--images-dir", str(out / "images"),
            "--charts" if charts else "--no-charts",
            "--no-verbose",
        ]
        started = time.perf_counter()
        completed = subprocess.run(command(*args), cwd=pipeline, capture_output=True, timeout=600)
        elapsed = time.perf_counter() - started

    return ProbeResult(seconds=elapsed, peak_rss_mib=_children_peak_rss_mib(), returncode=completed.returncode)


def _children_peak_rss_mib() -> float | None:
    try:
        import resource
    except ImportError:  # Windows
        return None
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # Linux riporta kibibyte, macOS byte.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def main() -> int:
    result = run_probe()
    print(f"esito            {'ok' if result.returncode == 0 else f'errore {result.returncode}'}")
    print(f"durata           {result.seconds:.1f} s")
    if result.peak_rss_mib is None:
        print("picco di memoria non misurabile su questo sistema (serve Linux)")
    else:
        print(f"picco di memoria {result.peak_rss_mib:.0f} MiB (processo della pipeline)")
    return result.returncode
