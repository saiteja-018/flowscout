"""Target URL validation and SSRF protection for public website QA runs.

The QA console accepts arbitrary public URLs.  Every URL must pass this module
before a browser is pointed at it.  The checks are deliberately conservative:

* only ``http`` and ``https``;
* no embedded credentials, fragments are removed;
* no loopback, private, link-local, multicast, reserved, or unspecified IPs;
* DNS is resolved and *every* answer must be public;
* known cloud metadata hostnames are rejected before resolution;
* live public runs only allow the standard web ports.

DNS rebinding is reduced by validating both the literal host and every
resolved address.  The in-guest browser additionally refuses to navigate to
private address literals.  No URL policy can make an untrusted browser fully
safe; this module is one layer of a fail-closed workflow.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "metadata.azure.internal",
    "instance-data",
    "0.0.0.0",
}
BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
    ".lan",
    ".intranet",
    ".corp",
)
METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("100.100.100.200"),
}
DEFAULT_LIVE_PORTS = {80, 443}
LOCAL_PORTS = set(range(1, 65536))


class URLValidationError(ValueError):
    """The target URL is not safe or not well-formed."""


@dataclass(frozen=True)
class URLDecision:
    allowed: bool
    url: str
    reason: str = ""


def normalize_url(raw: str, *, default_scheme: str = "https") -> str:
    """Return a normalized absolute URL or raise ``URLValidationError``."""
    if not isinstance(raw, str):
        raise URLValidationError("URL must be text")
    value = raw.strip()
    if not value:
        raise URLValidationError("Enter a website URL")
    if len(value) > 2048:
        raise URLValidationError("URL is too long (maximum 2048 characters)")
    if "://" not in value:
        value = f"{default_scheme}://{value}"
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise URLValidationError(f"Malformed URL: {exc}") from None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise URLValidationError("Only http:// and https:// URLs are supported")
    if not parsed.hostname:
        raise URLValidationError("URL has no hostname")
    if parsed.username or parsed.password:
        raise URLValidationError("Credentials in URLs are not allowed")
    host = parsed.hostname.lower().rstrip(".")
    if not host:
        raise URLValidationError("URL has no hostname")
    if host in BLOCKED_HOSTNAMES or any(host.endswith(suffix) for suffix in BLOCKED_SUFFIXES):
        # ``localhost`` is meaningful only for the explicit local fixture mode;
        # metadata/internal hostnames are rejected here regardless.
        if not (host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost")):
            raise URLValidationError(f"Host {host!r} is not a public website")
    if " " in host or "\x00" in host:
        raise URLValidationError("Hostname contains invalid characters")
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise URLValidationError("Port is out of range")
    path = parsed.path or "/"
    return urlunsplit((scheme, parsed.netloc, path, parsed.query, ""))


def _address_is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip in METADATA_ADDRESSES:
        return False
    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _address_is_local_dev(address: str) -> bool:
    """Loopback/private addresses are allowed only in explicit local mode.

    Link-local addresses (including cloud metadata endpoints), multicast,
    reserved, and unspecified addresses are always rejected. This keeps the
    bundled ShopDemo usable without turning mock mode into an SSRF bypass.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip in METADATA_ADDRESSES:
        return False
    if ip.is_loopback:
        return True
    if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return bool(ip.is_private)


def _address_allowed(address: str, *, allow_private: bool) -> bool:
    if _address_is_public(address):
        return True
    return allow_private and _address_is_local_dev(address)


def _literal_ip(host: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def resolve_public_host(
    host: str, port: int | None, *, allow_private: bool = False
) -> list[str]:
    """Resolve ``host`` and return addresses, raising when any is unsafe."""
    literal = _literal_ip(host)
    if literal is not None:
        if not _address_allowed(str(literal), allow_private=allow_private):
            raise URLValidationError(f"Address {host} is not an allowed internet address")
        return [str(literal)]
    try:
        infos = socket.getaddrinfo(
            host,
            port or 443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise URLValidationError(f"Could not resolve {host!r}: {exc.strerror or exc}") from None
    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses:
        raise URLValidationError(f"Host {host!r} did not resolve to an address")
    for address in addresses:
        if not _address_allowed(address, allow_private=allow_private):
            reason = "non-public" if not allow_private else "not an allowed local"
            raise URLValidationError(
                f"Host {host!r} resolves to a {reason} address ({address})"
            )
    return addresses


def validate_public_url(
    raw: str,
    *,
    allow_private: bool = False,
    allowed_hosts: set[str] | None = None,
    allowed_ports: set[int] | None = None,
) -> URLDecision:
    """Validate a URL for a QA run.

    ``allow_private`` exists for the bundled ShopDemo fixture and for explicit
    local development.  It must never be enabled for a public live run.
    """
    try:
        url = normalize_url(raw)
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if allowed_hosts is not None and host not in allowed_hosts:
            return URLDecision(False, url, f"Host {host!r} is not in FLOWSCOUT_TARGET_ALLOWLIST")
        if not allow_private:
            ports = allowed_ports if allowed_ports is not None else DEFAULT_LIVE_PORTS
            if port not in ports:
                return URLDecision(
                    False,
                    url,
                    f"Port {port} is not allowed for public live runs (use 80 or 443)",
                )
        resolve_public_host(host, port, allow_private=allow_private)
        return URLDecision(True, url)
    except URLValidationError as exc:
        return URLDecision(False, raw, str(exc))
    except Exception as exc:  # fail closed on any unexpected parser/resolver state
        return URLDecision(False, raw, f"URL validation failed: {type(exc).__name__}")


def require_public_url(
    raw: str,
    *,
    allow_private: bool = False,
    allowed_hosts: set[str] | None = None,
    allowed_ports: set[int] | None = None,
) -> str:
    decision = validate_public_url(
        raw,
        allow_private=allow_private,
        allowed_hosts=allowed_hosts,
        allowed_ports=allowed_ports,
    )
    if not decision.allowed:
        raise URLValidationError(decision.reason)
    return decision.url


def allowlist_from_env() -> set[str] | None:
    raw = os.getenv("FLOWSCOUT_TARGET_ALLOWLIST", "").strip()
    if not raw:
        return None
    hosts = {item.strip().lower().rstrip(".") for item in raw.split(",") if item.strip()}
    return hosts or None


def is_same_origin(url: str, origin: str) -> bool:
    try:
        left = urlsplit(url)
        right = urlsplit(origin)
    except ValueError:
        return False
    left_port = left.port or (443 if left.scheme == "https" else 80)
    right_port = right.port or (443 if right.scheme == "https" else 80)
    return (
        left.scheme.lower() == right.scheme.lower()
        and (left.hostname or "").lower() == (right.hostname or "").lower()
        and left_port == right_port
    )


def origin_of(url: str) -> str:
    parsed = urlsplit(url)
    port = parsed.port
    default = 443 if parsed.scheme == "https" else 80
    netloc = parsed.hostname or ""
    if port and port != default:
        netloc = f"{netloc}:{port}"
    return f"{parsed.scheme}://{netloc}"


def path_of(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.path or "/"
