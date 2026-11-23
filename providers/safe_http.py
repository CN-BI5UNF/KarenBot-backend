"""HTTP helpers for requests to user-supplied public provider URLs."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import httpx


class UnsafeProviderTarget(ValueError):
    """A user-supplied provider URL resolved to a non-public address."""


def _public_ips(
    host: str, port: int
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeProviderTarget("无法安全解析 Provider 地址") from exc
        addresses = [
            ipaddress.ip_address(record[4][0].split("%", 1)[0])
            for record in records
        ]
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeProviderTarget("未保存的 Provider 不能探测内网、回环或保留地址")
    return list(dict.fromkeys(addresses))


def pin_public_url(
    url: str,
) -> tuple[str, str, str]:
    """Resolve a public URL once and return an IP-pinned URL, authority and SNI."""
    parsed = httpx.URL(url)
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise UnsafeProviderTarget("Base URL 必须是有效的 HTTP/HTTPS 地址")
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port
    if port == 0:
        raise UnsafeProviderTarget("Base URL 端口无效")
    port = default_port if port is None else port
    addresses = _public_ips(parsed.host, port)
    address = addresses[0]
    pinned = parsed.copy_with(host=address.compressed)
    authority_host = f"[{parsed.host}]" if ":" in parsed.host else parsed.host
    authority = (
        f"{authority_host}:{port}"
        if port != default_port
        else authority_host
    )
    return str(pinned), authority, parsed.host


async def send_provider_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: Any = None,
    public_only: bool = False,
) -> httpx.Response:
    """Send a provider request without redirects, pinning untrusted public hosts."""
    if not public_only:
        return await client.request(
            method,
            url,
            headers=headers,
            json=json_body,
            follow_redirects=False,
        )

    parsed = httpx.URL(url)
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise UnsafeProviderTarget("Base URL 必须是有效的 HTTP/HTTPS 地址")
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port
    if port == 0:
        raise UnsafeProviderTarget("Base URL 端口无效")
    port = default_port if port is None else port
    addresses = _public_ips(parsed.host, port)
    hostname = parsed.host
    authority_host = f"[{hostname}]" if ":" in hostname else hostname
    authority = (
        f"{authority_host}:{port}" if port != default_port else authority_host
    )
    pinned_headers = httpx.Headers(headers)
    pinned_headers["Host"] = authority
    last_error: httpx.ConnectError | httpx.ConnectTimeout | None = None
    for address in addresses:
        pinned_url = parsed.copy_with(host=address.compressed)
        request = client.build_request(
            method,
            pinned_url,
            headers=pinned_headers,
            json=json_body,
            extensions={"sni_hostname": hostname},
        )
        try:
            return await client.send(request, follow_redirects=False)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise UnsafeProviderTarget("无法安全解析 Provider 地址")
