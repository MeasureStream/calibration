"""Il consumer dei job (PIANO_METROLOGIA 3.4, specifica § 6.3).

Un solo consumer, quindi un job alla volta. L'offset si conferma **dopo** aver pubblicato il
risultato: se il motore cade a meta', il job si ripete al riavvio, e metrology-service ignora
la seconda consegna dello stesso risultato perche' e' idempotente sull'id del job.

A ogni giro il consumer tocca un file: ``calibration-engine health`` ne controlla l'eta', ed e'
l'healthcheck di Docker. Un consumer bloccato smette di toccarlo e il container risulta malato.
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from typing import Any

from calibration_engine.contracts import JOBS_TOPIC, RESULTS_TOPIC, CalibrationJob
from calibration_engine.jobs import Metrology, execute
from calibration_engine.settings import EngineSettings

log = logging.getLogger(__name__)


def touch(settings: EngineSettings) -> None:
    path = Path(settings.heartbeat_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding="utf-8")


def healthy(settings: EngineSettings, now: float | None = None) -> bool:
    path = Path(settings.heartbeat_file)
    if not path.is_file():
        return False
    age = (now or time.time()) - path.stat().st_mtime
    return age <= settings.heartbeat_max_age_seconds


def handle(raw: bytes | None, client: Metrology, settings: EngineSettings) -> str | None:
    """Un messaggio di ``calibration-jobs`` → il JSON del risultato, o None se illeggibile."""
    if not raw:
        return None
    try:
        job = CalibrationJob.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001 - un messaggio rotto non deve fermare la coda
        log.error("Job illeggibile, scartato: %s", exc)
        return None
    log.info("Job %s %s (sessione %s)", job.job_id, job.kind, job.session_id)
    result = execute(job, client, settings)
    log.info("Job %s: %s%s", job.job_id, result.status, f" ({result.error.code})" if result.error else "")
    return result.to_json()


def run(settings: EngineSettings, client: Metrology) -> None:
    from confluent_kafka import Consumer, Producer

    # Il job piu' lungo deve stare dentro max.poll.interval, o Kafka riassegna la partizione a
    # meta' lavoro. Una sessione puo' avere fino a 12 sensori: si lascia margine.
    poll_interval_ms = (settings.job_timeout_seconds * 12 + 120) * 1000
    consumer = Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.consumer_group,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            "max.poll.interval.ms": poll_interval_ms,
        }
    )
    producer = Producer({"bootstrap.servers": settings.kafka_bootstrap_servers, "acks": "all"})

    stopping = {"now": False}

    def stop(*_: Any) -> None:
        stopping["now"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    consumer.subscribe([JOBS_TOPIC])
    log.info("In ascolto su %s (%s)", JOBS_TOPIC, settings.kafka_bootstrap_servers)
    try:
        while not stopping["now"]:
            touch(settings)
            message = consumer.poll(5.0)
            if message is None:
                continue
            if message.error():
                log.warning("Kafka: %s", message.error())
                continue
            result = handle(message.value(), client, settings)
            if result is not None:
                key = CalibrationJob.model_validate_json(message.value()).job_id
                producer.produce(RESULTS_TOPIC, key=str(key), value=result.encode("utf-8"))
                remaining = producer.flush(30)
                if remaining:
                    # Il risultato non e' partito: non si conferma l'offset, il job si ripetera'.
                    log.error("Risultato del job %s non consegnato: si riprovera' al riavvio", key)
                    break
            consumer.commit(message=message, asynchronous=False)
    finally:
        consumer.close()
