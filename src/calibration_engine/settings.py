"""Configurazione del motore, da variabili d'ambiente con prefisso ``ENGINE_``.

Il segreto del client Keycloak non sta mai nell'immagine ne' nel repository: arriva da
``ENGINE_CLIENT_SECRET`` oppure, meglio, da un file montato in ``/run/secrets/engine_client_secret``
(i secret di Docker Compose finiscono li').
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_DOCKER_SECRETS = Path("/run/secrets")


class EngineSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENGINE_",
        secrets_dir=_DOCKER_SECRETS if _DOCKER_SECRETS.is_dir() else None,
        extra="ignore",
    )

    # --- Kafka (dal passo 3.4) ---
    kafka_bootstrap_servers: str = "kafka:29092"
    consumer_group: str = "calibration-engine"

    # --- REST verso metrology-service, attraverso il gateway ---
    metrology_url: str = "http://gateway-iam:8080"

    # --- Keycloak: client credentials ---
    token_url: str = "https://auth.christiandellisanti.uk/realms/measurestream/protocol/openid-connect/token"
    client_id: str = "calibration-engine"
    client_secret: SecretStr | None = None

    # --- Esecuzione ---
    job_timeout_seconds: int = 600
    #: Il consumer tocca questo file a ogni giro: l'healthcheck di Docker ne controlla l'eta'.
    heartbeat_file: str = "/tmp/calibration-engine.heartbeat"
    heartbeat_max_age_seconds: int = 180
    #: L'XSD del DCC (PTB 3.3.0). Se manca, il DCC si produce lo stesso ma si dichiara non validato.
    dcc_xsd_path: str | None = None
    http_timeout_seconds: float = 30.0

    # Versione del motore citata nel DCC: la imposta l'immagine Docker (ENGINE_GIT_SHA).
    git_sha: str = "sconosciuto"


@lru_cache(maxsize=1)
def get_settings() -> EngineSettings:
    return EngineSettings()
