"""Punto d'ingresso: ``python -m calibration_engine`` o ``calibration-engine``.

Il comando predefinito dell'immagine e' ``consume``: il consumer dei job (passo 3.4). Gli
altri servono a provare l'immagine e la sua configurazione:

- ``consume``: ascolta ``calibration-jobs`` e pubblica i risultati su ``calibration-results``;
- ``health``: l'healthcheck di Docker, vero se il consumer ha fatto un giro di recente;
- ``version``: versione del pacchetto e commit da cui e' costruita l'immagine;
- ``check``: carica le dipendenze scientifiche e verifica che la pipeline sia al suo posto;
- ``memory-probe``: esegue un job tipico e misura picco di memoria e durata;
- ``whoami``: ottiene un token dal client Keycloak e chiama metrology-service.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from calibration_engine import __version__
from calibration_engine.pipeline import orchestrator, pipeline_dir

#: Le librerie senza le quali il motore non parte.
REQUIRED_MODULES = (
    "numpy", "scipy", "matplotlib", "reportlab", "pint", "xmlschema", "pydantic", "httpx", "confluent_kafka",
)


def _check() -> int:
    problems: list[str] = []
    for name in REQUIRED_MODULES:
        try:
            module = importlib.import_module(name)
            print(f"  {name:<16} {getattr(module, '__version__', '?')}")
        except Exception as exc:  # noqa: BLE001 - qui si vuole vedere qualunque errore di import
            problems.append(f"{name}: {exc}")

    if not orchestrator().is_file():
        problems.append(f"pipeline non trovata: {orchestrator()}")
    else:
        print(f"  pipeline         {pipeline_dir()}")

    for problem in problems:
        print(f"ERRORE {problem}", file=sys.stderr)
    return 1 if problems else 0


def _version() -> int:
    from calibration_engine.settings import get_settings

    print(f"{__version__}+{get_settings().git_sha}")
    return 0


def _whoami() -> int:
    from calibration_engine.auth import MetrologyClient, ServiceToken, TokenError
    from calibration_engine.settings import get_settings

    settings = get_settings()
    try:
        client = MetrologyClient(settings, ServiceToken(settings))
    except TokenError as exc:
        print(f"ERRORE {exc}", file=sys.stderr)
        return 1
    try:
        print(json.dumps(client.whoami(), indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - diagnostica: si mostra l'errore cosi' com'e'
        print(f"ERRORE {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


def _consume() -> int:
    import logging

    from calibration_engine.auth import MetrologyClient, ServiceToken, TokenError
    from calibration_engine.consumer import run
    from calibration_engine.settings import get_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    try:
        client = MetrologyClient(settings, ServiceToken(settings))
    except TokenError as exc:
        print(f"ERRORE {exc}", file=sys.stderr)
        return 1
    try:
        run(settings, client)
        return 0
    finally:
        client.close()


def _health() -> int:
    from calibration_engine.consumer import healthy
    from calibration_engine.settings import get_settings

    return 0 if healthy(get_settings()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="calibration-engine", description="Motore di taratura di MeasureStream")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="versione del motore")
    sub.add_parser("check", help="verifica dipendenze e pipeline")
    sub.add_parser("memory-probe", help="esegue un job tipico e ne misura memoria e durata")
    sub.add_parser("whoami", help="si autentica e chiede a metrology-service chi e'")
    sub.add_parser("consume", help="consuma i job di taratura da Kafka")
    sub.add_parser("health", help="healthcheck: il consumer e' vivo?")
    args = parser.parse_args(argv)

    if args.command == "consume":
        return _consume()
    if args.command == "health":
        return _health()
    if args.command == "version":
        return _version()
    if args.command == "memory-probe":
        from calibration_engine.probe import main as probe

        return probe()
    if args.command == "whoami":
        return _whoami()
    return _check()


if __name__ == "__main__":
    sys.exit(main())
