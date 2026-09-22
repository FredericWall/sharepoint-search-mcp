from unittest.mock import MagicMock, patch

import pytest

import mcp_server.main as main


def test_build_auth_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_DISABLED", "1")
    monkeypatch.delenv("VCAP_APPLICATION", raising=False)
    assert main._build_auth() is None


def test_build_auth_refuses_disabled_auth_in_cloud_foundry(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_DISABLED", "1")
    monkeypatch.setenv("VCAP_APPLICATION", "{}")
    monkeypatch.delenv("MCP_ALLOW_INSECURE_CF", raising=False)

    with pytest.raises(RuntimeError, match="Refusing to disable"):
        main._build_auth()


def test_build_auth_missing_env_raises(monkeypatch):
    # No MCP_AUTH_DISABLED and no OIDC_* → must raise a clear RuntimeError.
    monkeypatch.delenv("MCP_AUTH_DISABLED", raising=False)
    for k in ("OIDC_CONFIG_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "MCP_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError) as exc:
        main._build_auth()
    assert "missing env vars" in str(exc.value)


def test_build_auth_builds_oauthproxy(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("OIDC_CONFIG_URL", "https://idp.example.com/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "client-123")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret-xyz")
    monkeypatch.setenv("MCP_BASE_URL", "https://myserver.example.com")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VCAP_SERVICES", raising=False)

    fake_discovery = {
        "authorization_endpoint": "https://idp.example.com/oauth2/authorize",
        "token_endpoint": "https://idp.example.com/oauth2/token",
        "revocation_endpoint": "https://idp.example.com/oauth2/revoke",
        "jwks_uri": "https://idp.example.com/oauth2/certs",
        "issuer": "https://idp.example.com",
    }
    fake_resp = MagicMock()
    fake_resp.json.return_value = fake_discovery

    with patch("mcp_server.main.requests.get", return_value=fake_resp) as mock_get:
        auth = main._build_auth()

    mock_get.assert_called_once()
    fake_resp.raise_for_status.assert_called_once_with()
    # It must be an OAuthProxy instance built from the discovery endpoints.
    assert type(auth).__name__ == "OAuthProxy"


def test_build_auth_uses_postgres_client_storage(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("OIDC_CONFIG_URL", "https://idp.example.com/config")
    monkeypatch.setenv("OIDC_CLIENT_ID", "client-123")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret-xyz")
    monkeypatch.setenv("MCP_BASE_URL", "https://myserver.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql://bound")
    monkeypatch.setenv("INDEX_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_SCHEMA", "custom_index")
    fake_discovery = {
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "jwks_uri": "https://idp.example.com/certs",
        "issuer": "https://idp.example.com",
    }
    fake_resp = MagicMock()
    fake_resp.json.return_value = fake_discovery

    with patch("mcp_server.main.requests.get", return_value=fake_resp):
        auth = main._build_auth()

    assert isinstance(auth._client_storage, main.PostgresKVStorage)
    assert auth._client_storage._database_url == "postgresql://bound"
    assert auth._client_storage._schema == "custom_index"


def test_build_auth_defaults_audience_to_client_id(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    monkeypatch.setenv("OIDC_CONFIG_URL", "https://idp.example.com/config")
    monkeypatch.setenv("OIDC_CLIENT_ID", "expected-audience")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("INDEX_BACKEND", "sqlite")
    response = MagicMock()
    response.json.return_value = {
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "jwks_uri": "https://idp.example.com/certs",
        "issuer": "https://idp.example.com",
    }

    with (
        patch("mcp_server.main.requests.get", return_value=response),
        patch("mcp_server.main.JWTVerifier") as verifier,
        patch("mcp_server.main.OAuthProxy") as proxy,
    ):
        main._build_auth()

    assert verifier.call_args.kwargs["audience"] == "expected-audience"
    assert proxy.call_args.kwargs["base_url"] == "https://mcp.example.com"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("OIDC_CONFIG_URL", "http://idp.example.com/config"),
        ("MCP_BASE_URL", "https://mcp.example.com/mcp"),
        ("MCP_BASE_URL", "https://mcp.example.com/"),
    ],
)
def test_build_auth_rejects_unsafe_urls(monkeypatch, variable, value):
    monkeypatch.delenv("MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("OIDC_CONFIG_URL", "https://idp.example.com/config")
    monkeypatch.setenv("OIDC_CLIENT_ID", "client")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv(variable, value)

    with pytest.raises(RuntimeError, match=variable):
        main._build_auth()


def test_build_auth_rejects_unsafe_discovery_endpoint(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("OIDC_CONFIG_URL", "https://idp.example.com/config")
    monkeypatch.setenv("OIDC_CLIENT_ID", "client")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.example.com")
    response = MagicMock()
    response.json.return_value = {
        "authorization_endpoint": "http://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "jwks_uri": "https://idp.example.com/certs",
        "issuer": "https://idp.example.com",
    }

    with (
        patch("mcp_server.main.requests.get", return_value=response),
        pytest.raises(RuntimeError, match="authorization_endpoint"),
    ):
        main._build_auth()
