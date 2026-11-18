"""Il motore ottiene il token con client credentials e lo usa verso metrology-service."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from calibration_engine.auth import MetrologyClient, ServiceToken, TokenError
from calibration_engine.settings import EngineSettings

TOKEN_URL = "https://keycloak.test/realms/measurestream/protocol/openid-connect/token"


def _settings(**overrides) -> EngineSettings:
    values = {
        "token_url": TOKEN_URL,
        "client_id": "calibration-engine",
        "client_secret": SecretStr("segreto-di-prova"),
        "metrology_url": "http://gateway.test",
    }
    values.update(overrides)
    return EngineSettings(**values)


class _Keycloak:
    """Finto endpoint dei token: conta le richieste e rilascia token numerati."""

    def __init__(self) -> None:
        self.requests: list[dict[str, list[str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": f"token-{len(self.requests)}", "expires_in": 300})


def test_chiede_il_token_con_client_credentials():
    keycloak = _Keycloak()
    token = ServiceToken(_settings(), httpx.Client(transport=httpx.MockTransport(keycloak)))

    assert token.get() == "token-1"
    form = keycloak.requests[0]
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == ["calibration-engine"]
    assert form["client_secret"] == ["segreto-di-prova"]


def test_riusa_il_token_finche_e_valido():
    keycloak = _Keycloak()
    token = ServiceToken(_settings(), httpx.Client(transport=httpx.MockTransport(keycloak)))

    assert token.get() == token.get() == "token-1"
    assert len(keycloak.requests) == 1


def test_senza_segreto_non_parte():
    with pytest.raises(TokenError, match="segreto"):
        ServiceToken(_settings(client_secret=None))


def test_un_rifiuto_di_keycloak_e_un_errore_chiaro():
    refuse = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "unauthorized_client"}))
    token = ServiceToken(_settings(), httpx.Client(transport=refuse))
    with pytest.raises(TokenError, match="401"):
        token.get()


def test_whoami_porta_il_token_e_lo_rinnova_dopo_un_401():
    keycloak = _Keycloak()
    token = ServiceToken(_settings(), httpx.Client(transport=httpx.MockTransport(keycloak)))
    seen: list[str] = []

    def metrology(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        if len(seen) == 1:
            return httpx.Response(401)
        assert request.url.path == "/API/metrology/whoami"
        return httpx.Response(200, json={"subject": "service-account-calibration-engine", "roles": ["CALIBRATION_ENGINE"]})

    client = MetrologyClient(_settings(), token, transport=httpx.MockTransport(metrology))
    body = client.whoami()

    assert seen == ["Bearer token-1", "Bearer token-2"]
    assert body["roles"] == ["CALIBRATION_ENGINE"]
    assert json.dumps(body)  # serializzabile, e' cio' che stampa il comando


def test_il_segreto_si_legge_dalla_variabile_d_ambiente(monkeypatch):
    monkeypatch.setenv("ENGINE_CLIENT_SECRET", "dall-ambiente")
    monkeypatch.setenv("ENGINE_JOB_TIMEOUT_SECONDS", "120")
    settings = EngineSettings()
    assert settings.client_secret is not None
    assert settings.client_secret.get_secret_value() == "dall-ambiente"
    assert settings.job_timeout_seconds == 120
    assert "dall-ambiente" not in repr(settings)


def test_il_segreto_si_legge_dal_file_montato_da_compose(tmp_path, monkeypatch):
    """Compose monta il secret in /run/secrets/engine_client_secret: e' quel nome che conta."""
    monkeypatch.delenv("ENGINE_CLIENT_SECRET", raising=False)
    (tmp_path / "engine_client_secret").write_text("dal-file", encoding="utf-8")
    settings = EngineSettings(_secrets_dir=tmp_path)
    assert settings.client_secret is not None
    assert settings.client_secret.get_secret_value() == "dal-file"
