"""Il motore si presenta a metrology-service con un token di servizio.

Nessun utente e nessuna password: il client Keycloak ``calibration-engine`` ottiene un token con
il flusso *client credentials* e lo usa per le chiamate REST, attraverso il gateway. Il token
dura pochi minuti; qui lo si tiene finche' manca meno di mezzo minuto alla scadenza, e se il
server risponde 401 lo si richiede una volta sola.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from typing import Any

import httpx

from calibration_engine.settings import EngineSettings

#: Margine prima della scadenza oltre il quale il token si rinnova in anticipo.
_REFRESH_MARGIN_S = 30.0


class TokenError(RuntimeError):
    """Keycloak non ha rilasciato il token."""


class ServiceToken:
    """Token del service account, rinnovato quando serve."""

    def __init__(self, settings: EngineSettings, http: httpx.Client | None = None) -> None:
        if settings.client_secret is None:
            raise TokenError("manca il segreto del client: ENGINE_CLIENT_SECRET o /run/secrets/engine_client_secret")
        self._settings = settings
        self._http = http or httpx.Client(timeout=settings.http_timeout_seconds)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def get(self, *, force: bool = False) -> str:
        with self._lock:
            if force or self._token is None or time.monotonic() >= self._expires_at - _REFRESH_MARGIN_S:
                self._token, lifetime = self._request()
                self._expires_at = time.monotonic() + lifetime
            return self._token

    def _request(self) -> tuple[str, float]:
        settings = self._settings
        response = self._http.post(
            settings.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.client_id,
                "client_secret": settings.client_secret.get_secret_value(),  # type: ignore[union-attr]
            },
        )
        if response.status_code != 200:
            raise TokenError(f"Keycloak ha risposto {response.status_code}: {response.text[:200]}")
        body: dict[str, Any] = response.json()
        return body["access_token"], float(body.get("expires_in", 60))


class BearerAuth(httpx.Auth):
    """Aggiunge il token a ogni richiesta e lo rinnova una volta se il server risponde 401."""

    def __init__(self, token: ServiceToken) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token.get()}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self._token.get(force=True)}"
            yield request


class MetrologyClient:
    """Le chiamate REST del motore verso metrology-service."""

    def __init__(self, settings: EngineSettings, token: ServiceToken, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(
            base_url=settings.metrology_url,
            auth=BearerAuth(token),
            timeout=settings.http_timeout_seconds,
            transport=transport,
        )

    def whoami(self) -> dict[str, Any]:
        """Chi sono per metrology-service: la prova che token, gateway e ruoli sono a posto."""
        response = self._http.get("/API/metrology/whoami")
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._http.close()
