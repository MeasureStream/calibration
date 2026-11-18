"""Motore di taratura di MeasureStream.

Esegue i job che metrology-service mette sul topic ``calibration-jobs`` e pubblica l'esito su
``calibration-results``. Il calcolo metrologico e' la pipeline di Mario Attrovio, che vive in
``pipeline/`` cosi' come l'ha scritta; questo pacchetto e' tutto cio' che le sta attorno:
contratti dei messaggi, configurazione, consumer, client REST.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("calibration-engine")
except PackageNotFoundError:  # eseguito dai sorgenti senza installazione
    __version__ = "0.0.0+sorgenti"
