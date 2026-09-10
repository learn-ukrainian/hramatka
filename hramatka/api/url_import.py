"""SSRF-safe server-side URL fetch and readable-text extraction for teacher anchors."""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections import defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

MAX_RESPONSE_BYTES = 1_048_576
MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 10.0
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 10

_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "text/plain"})
_ALLOWED_PORTS = frozenset({80, 443})
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})

_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    # IPv4 special/private/reserved/CGN/multicast/benchmark (per review + RFCs)
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGN (carrier-grade NAT)
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("192.88.99.0/24"),  # 6to4 anycast (deprecated)
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved / future use
    # IPv6
    ipaddress.ip_network("::/128"),  # unspecified
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),  # link-local
    ipaddress.ip_network("fc00::/7"),  # unique local
    ipaddress.ip_network("ff00::/8"),  # multicast
)

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.IGNORECASE,
)

# IPv6 transition formats that embed an IPv4 address (SSRF bypass vectors if unchecked).
_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")
_NAT64_ALT_NETWORK = ipaddress.ip_network("64:ff9b:1::/48")
_6TO4_NETWORK = ipaddress.ip_network("2002::/16")
_IPV4_COMPAT_NETWORK = ipaddress.ip_network("::/96")
_IPV4_MAPPED_NETWORK = ipaddress.ip_network("::ffff:0:0/96")
_IPV4_TRANSLATED_NETWORK = ipaddress.ip_network("::ffff:0:0:0/96")
_ISATAP_NETWORK = ipaddress.ip_network("2001:0:5efe::/96")


class UrlImportError(Exception):
    """Raised when a teacher URL import request must be rejected."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class UrlImportResult:
    text: str
    source_url: str


class _RateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, list[float]] = defaultdict(list)

    def check(self, teacher_id: str) -> None:
        now = time.monotonic()
        window_start = now - RATE_LIMIT_WINDOW_SECONDS
        events = [stamp for stamp in self._events[teacher_id] if stamp >= window_start]
        if len(events) >= RATE_LIMIT_MAX_REQUESTS:
            raise UrlImportError(
                "url_rate_limited",
                "Забагато запитів на отримання тексту. Спробуйте, будь ласка, пізніше.",
                retryable=True,
            )
        events.append(now)
        self._events[teacher_id] = events


_rate_limiter = _RateLimiter()


class _ReadableTextExtractor(HTMLParser):
    _SKIP_TAGS = frozenset({"script", "style", "nav", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def text(self) -> str:
        joined = " ".join(chunk.strip() for chunk in self._chunks if chunk.strip())
        return re.sub(r"\s+", " ", joined).strip()


def _is_ipv4_blocked(address: ipaddress.IPv4Address) -> bool:
    return any(
        address in net for net in _BLOCKED_NETWORKS if isinstance(net, ipaddress.IPv4Network)
    )


def _embedded_ipv4_from_ipv6(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Extract embedded IPv4 from transition/coexistence IPv6 formats, if present."""
    # 1. IPv4-Mapped (::ffff:0:0/96)
    mapped = address.ipv4_mapped
    if mapped is not None:
        return mapped

    # 2. IPv4-Translated (::ffff:0:0:0/96)
    if address in _IPV4_TRANSLATED_NETWORK:
        return ipaddress.IPv4Address(int(address) & 0xFFFF_FFFF)

    # 3. NAT64 Well-Known Prefix (/96)
    if address in _NAT64_NETWORK:
        return ipaddress.IPv4Address(int(address) & 0xFFFF_FFFF)

    # 4. NAT64 Custom / Enterprise Prefix (/48)
    if address in _NAT64_ALT_NETWORK:
        # RFC 6052: bits 48-63 (high 16 bits of IPv4) and bits 72-87 (low 16 bits of IPv4)
        high_16 = (int(address) >> 64) & 0xFFFF
        low_16 = (int(address) >> 40) & 0xFFFF
        return ipaddress.IPv4Address((high_16 << 16) | low_16)

    # 5. 6to4 (/16)
    if address in _6TO4_NETWORK:
        return ipaddress.IPv4Address(address.packed[2:6])

    # 6. ISATAP (Well-Known /96 or Interface Identifier)
    if address in _ISATAP_NETWORK:
        return ipaddress.IPv4Address(int(address) & 0xFFFF_FFFF)

    packed = address.packed
    if packed[10:12] == b"\x5e\xfe" and packed[8:10] in (b"\x00\x00", b"\x02\x00"):
        return ipaddress.IPv4Address(packed[12:16])

    # 7. Teredo (2001:0::/32)
    if address in ipaddress.ip_network("2001:0::/32"):
        server_v4 = ipaddress.IPv4Address((int(address) >> 64) & 0xFFFF_FFFF)
        client_v4 = ipaddress.IPv4Address((int(address) & 0xFFFF_FFFF) ^ 0xFFFF_FFFF)
        # Check both; return the blocked one if either is blocked, otherwise client_v4
        if _is_ipv4_blocked(server_v4):
            return server_v4
        return client_v4

    # 8. IPv4-Compatible (/96)
    if address in _IPV4_COMPAT_NETWORK:
        return ipaddress.IPv4Address(int(address) & 0xFFFF_FFFF)

    return None


def _is_blocked_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_ipv4_from_ipv6(address)
        if embedded is not None and _is_ipv4_blocked(embedded):
            return True
    return any(address in network for network in _BLOCKED_NETWORKS)


def _resolve_host(hostname: str) -> str:
    try:
        infos = socket.getaddrinfo(
            hostname,
            None,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise UrlImportError(
            "url_fetch_failed",
            "Не вдалося отримати текст із цієї адреси. Перевірте посилання.",
            retryable=True,
        ) from error
    if not infos:
        raise UrlImportError(
            "url_blocked",
            "Ця адреса недоступна для імпорту.",
        )
    for _, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_blocked_ip(ip):
            raise UrlImportError(
                "url_blocked",
                "Ця адреса недоступна для імпорту.",
            )
    return infos[0][4][0]


def _validate_target_url(url: str) -> tuple[str, str, int, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UrlImportError(
            "url_invalid",
            "Потрібна адреса, що починається з https://.",
        )
    hostname = parsed.hostname
    if not hostname or "%" in hostname:
        raise UrlImportError(
            "url_invalid",
            "Потрібна коректна https-адреса.",
        )
    if hostname.lower() in {"localhost"} or hostname.endswith(".local"):
        raise UrlImportError(
            "url_blocked",
            "Ця адреса недоступна для імпорту.",
        )
    # Unified IP literal check (canonical + non-canonical IPv4 like 0177.0.0.1,
    # bare-decimal, hex, IPv6). Fail closed on ValueError. IP literals rejected
    # unless blocked (url_blocked); else url_invalid.
    try:
        ip = ipaddress.ip_address(hostname)
        if _is_blocked_ip(ip):
            raise UrlImportError(
                "url_blocked",
                "Ця адреса недоступна для імпорту.",
            )
        raise UrlImportError(
            "url_invalid",
            "Потрібна адреса з доменним ім’ям, а не IP.",
        )
    except ValueError:
        # Not a parseable IP literal (non-canonical forms that raise, or real domain names)
        pass
    if not _HOSTNAME_RE.fullmatch(hostname):
        raise UrlImportError(
            "url_invalid",
            "Потрібна коректна https-адреса.",
        )
    port = parsed.port or 443
    if port not in _ALLOWED_PORTS:
        raise UrlImportError(
            "url_invalid",
            "Дозволені лише стандартні https-порти.",
        )
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return hostname, path, port, parsed.geturl()


def _content_type_allowed(value: str | None) -> bool:
    if not value:
        return False
    mime = value.split(";", maxsplit=1)[0].strip().lower()
    return mime in _ALLOWED_CONTENT_TYPES


def _extract_text(body: bytes, content_type: str | None) -> str:
    mime = (content_type or "").split(";", maxsplit=1)[0].strip().lower()
    charset = "utf-8"
    if content_type and "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", maxsplit=1)[1].split(";", 1)[0].strip()
    try:
        decoded = body.decode(charset, errors="replace")
    except LookupError:
        decoded = body.decode("utf-8", errors="replace")
    if mime == "text/plain":
        return decoded.strip()
    parser = _ReadableTextExtractor()
    parser.feed(decoded)
    parser.close()
    return parser.text()


def _read_limited_body(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise UrlImportError(
                "url_too_large",
                "Сторінка занадто велика для імпорту.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_url_text(
    url: str,
    *,
    teacher_id: str,
    client: httpx.Client | None = None,
) -> UrlImportResult:
    """Fetch readable text from an HTTPS page with SSRF defenses on every hop."""
    _rate_limiter.check(teacher_id)
    current = url.strip()
    if not current:
        raise UrlImportError(
            "url_invalid",
            "Вставте адресу сторінки — і ми дістанемо з неї текст.",
        )

    owns_client = client is None
    client = client or httpx.Client(
        follow_redirects=False,
        timeout=FETCH_TIMEOUT_SECONDS,
        headers={"User-Agent": "HramatkaTeacherPilot/1.0"},
    )
    try:
        for hop in range(MAX_REDIRECTS + 1):
            hostname, _, port, normalized = _validate_target_url(current)
            # Resolve once per hop + validate all addrs. Connect to pinned IP (no re-DNS).
            # sni_hostname ext + Host header preserve SNI / virtual host (closes rebind window).
            pinned_ip = _resolve_host(hostname)
            # Build connect target with IP literal (bracket v6). Non-443 port if needed.
            if ":" in pinned_ip and not pinned_ip.startswith("["):
                ip_literal = f"[{pinned_ip}]"
            else:
                ip_literal = pinned_ip
            netloc = f"{ip_literal}:{port}" if port != 443 else ip_literal
            parsed_norm = urlparse(normalized)
            connect_url = parsed_norm._replace(netloc=netloc).geturl()

            try:
                response = client.get(
                    connect_url,
                    headers={"Host": hostname},
                    extensions={"sni_hostname": hostname},
                )
            except httpx.TimeoutException as error:
                raise UrlImportError(
                    "url_fetch_failed",
                    "Не вдалося отримати текст із цієї адреси. Перевірте посилання.",
                    retryable=True,
                ) from error
            except httpx.HTTPError as error:
                raise UrlImportError(
                    "url_fetch_failed",
                    "Не вдалося отримати текст із цієї адреси. Перевірте посилання.",
                    retryable=True,
                ) from error

            if response.status_code in _REDIRECT_STATUS_CODES:
                if hop >= MAX_REDIRECTS:
                    raise UrlImportError(
                        "url_redirect_blocked",
                        "Перенаправлення на цю адресу недоступне для імпорту.",
                    )
                location = response.headers.get("Location")
                if not location:
                    raise UrlImportError(
                        "url_fetch_failed",
                        "Не вдалося отримати текст із цієї адреси. Перевірте посилання.",
                        retryable=True,
                    )
                current = urljoin(normalized, location)
                continue

            if response.status_code >= 400:
                raise UrlImportError(
                    "url_fetch_failed",
                    "Не вдалося отримати текст із цієї адреси. Перевірте посилання.",
                    retryable=True,
                )

            content_type = response.headers.get("Content-Type")
            if not _content_type_allowed(content_type):
                raise UrlImportError(
                    "url_content_type",
                    "Сторінка має бути звичайним текстом або HTML.",
                )

            body = _read_limited_body(response)
            text = _extract_text(body, content_type)
            if not text:
                raise UrlImportError(
                    "url_empty",
                    "На сторінці не знайдено читабельного тексту.",
                )
            if len(text) > 100_000:
                raise UrlImportError(
                    "url_too_large",
                    "Сторінка занадто велика для імпорту.",
                )
            return UrlImportResult(text=text, source_url=normalized)
        raise UrlImportError(
            "url_redirect_blocked",
            "Перенаправлення на цю адресу недоступне для імпорту.",
        )
    finally:
        if owns_client:
            client.close()


def reset_rate_limits_for_tests() -> None:
    """Clear in-memory rate counters — test-only seam."""
    _rate_limiter._events.clear()
