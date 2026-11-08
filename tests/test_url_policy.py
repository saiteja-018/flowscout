from __future__ import annotations

import socket

import pytest

from app.url_policy import (
    URLValidationError,
    normalize_url,
    require_public_url,
    resolve_public_host,
    validate_public_url,
)


def _resolver(*addresses: str):
    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 443))
            for address in addresses
        ]

    return fake_getaddrinfo


def test_normalize_url_adds_https_and_strips_fragment() -> None:
    assert normalize_url("example.com/path#frag") == "https://example.com/path"


def test_public_host_allowed_in_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    decision = validate_public_url("https://example.com/pricing")
    assert decision.allowed is True
    assert decision.url == "https://example.com/pricing"


def test_loopback_rejected_for_live_mode() -> None:
    decision = validate_public_url("http://127.0.0.1:8099/shop/")
    assert decision.allowed is False
    assert "Port 8099" in decision.reason or "not allowed" in decision.reason


def test_loopback_allowed_only_with_explicit_local_mode() -> None:
    allowed = validate_public_url("http://127.0.0.1:8099/shop/", allow_private=True)
    assert allowed.allowed is True
    assert allowed.url == "http://127.0.0.1:8099/shop/"


def test_localhost_allowed_only_with_explicit_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("::1", "127.0.0.1"))
    assert validate_public_url("http://localhost:8099/shop/").allowed is False
    assert validate_public_url("http://localhost:8099/shop/", allow_private=True).allowed is True


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/",
        "http://100.100.100.200/latest/meta-data/",
    ],
)
def test_metadata_endpoints_are_blocked_even_in_local_mode(url: str) -> None:
    decision = validate_public_url(url, allow_private=True)
    assert decision.allowed is False


def test_credentials_in_url_rejected() -> None:
    decision = validate_public_url("https://user:secret@example.com/", allow_private=True)
    assert decision.allowed is False
    assert "Credentials" in decision.reason


def test_non_http_scheme_rejected() -> None:
    assert validate_public_url("javascript:alert(1)", allow_private=True).allowed is False
    assert validate_public_url("file:///etc/passwd", allow_private=True).allowed is False


def test_live_mode_restricts_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    decision = validate_public_url("https://example.com:8443/")
    assert decision.allowed is False
    assert "Port 8443" in decision.reason


def test_allowlist_rejects_other_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    decision = validate_public_url("https://example.com/", allowed_hosts={"allowed.example"})
    assert decision.allowed is False
    assert "ALLOWLIST" in decision.reason


def test_any_private_dns_answer_rejects_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34", "10.0.0.5"))
    decision = validate_public_url("https://mixed.example/")
    assert decision.allowed is False
    assert "non-public" in decision.reason


def test_resolve_public_host_rejects_private_literal() -> None:
    with pytest.raises(URLValidationError):
        resolve_public_host("10.0.0.7", 443)


def test_require_public_url_returns_normalized_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    assert require_public_url("example.com") == "https://example.com/"
