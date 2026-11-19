"""Riempire una sessione di taratura senza il pozzetto (prova del passo 3.2).

``pipeline/simulate/send_calibration_messages.py`` di Mario manda i gradini di
``one_measure.json`` sul topic ``calibrations`` con il vecchio ``calib_id`` inventato
(``calib-65537-4-…``), che metrology-service ora scarta: una sessione si apre via REST e il suo
id *è* il ``calib_id``. Questo comando riusa gli stessi gradini registrati, ma li indirizza alla
sessione indicata, con la fase e l'indice del sensore che il Raspberry aggiornato dovra'
mettere nei messaggi [S §7]:

    calibration-engine simulate-session --session 42 --phase AS_FOUND --sensor-index 0

I messaggi passano direttamente da Kafka, come nello script di Mario: kafka-stream (MQTT →
Kafka) resta fuori dalla prova.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from calibration_engine.pipeline import pipeline_dir

CALIBRATIONS_TOPIC = "calibrations"


def default_file() -> Path:
    return pipeline_dir() / "simulate" / "one_measure.json"


def _list(value: Any) -> Any:
    """Nel file di Mario alcune liste sono scritte come stringhe Python (``"[{'target': …}]"``)."""
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return ast.literal_eval(value)
    return value


def _number(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return int(value) if value.strip().lstrip("-").isdigit() else float(value)
        except ValueError:
            return value
    return value


def rewrite(message: dict[str, Any], session_id: int, phase: str | None, sensor_index: int | None) -> dict[str, Any]:
    """Un gradino registrato, indirizzato a una sessione vera e normalizzato nei tipi."""
    out = dict(message)
    out["calib_id"] = str(session_id)
    if phase:
        out["phase"] = phase
    if sensor_index is not None:
        out["sensor_index"] = sensor_index
    for key in ("step_summary", "ref_readings"):
        if key in out:
            out[key] = _list(out[key])
    for key in ("target", "step_index", "sensor_sampling_freq"):
        if key in out:
            out[key] = _number(out[key])
    return out


def load(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else [data]


def send(bootstrap: str, messages: list[dict[str, Any]]) -> int:
    from confluent_kafka import Producer

    producer = Producer({"bootstrap.servers": bootstrap, "acks": "all"})
    for message in messages:
        producer.produce(CALIBRATIONS_TOPIC, value=json.dumps(message).encode("utf-8"))
        print(f"  gradino {message.get('step_index')} → sessione {message['calib_id']} ({message.get('phase', 'fase dedotta')})")
    remaining = producer.flush(30)
    return 0 if remaining == 0 else 1
