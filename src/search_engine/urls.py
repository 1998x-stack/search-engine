"""Conservative URL canonicalization, scope handling and SSRF-resistant DNS selection."""
from __future__ import annotations
import ipaddress
import re
import socket
from urllib.parse import quote, urlsplit, urlunsplit

class URLRejected(ValueError):
    pass

def normalize_url(value: str, *, allowed_ports: set[int] | None = None) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise URLRejected('Invalid or oversized URL')
    try:
        url = urlsplit(value)
        if url.scheme.lower() not in ('http', 'https') or not url.hostname:
            raise URLRejected('Only absolute HTTP(S) URLs are allowed')
        if url.username is not None or url.password is not None:
            raise URLRejected('Embedded credentials are forbidden')
        host = url.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        if '%' in host or '..' in host or host.startswith('-'):
            raise URLRejected('Invalid hostname')
        port = url.port or (443 if url.scheme.lower() == 'https' else 80)
    except (ValueError, UnicodeError) as exc:
        raise URLRejected('Malformed hostname/port') from exc
    if port not in (allowed_ports or {80, 443}):
        raise URLRejected('Port not approved')
    if (host.isdecimal() or re.fullmatch(r'0x[0-9a-f]+',host) or
            re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', host)):
        try:
            address = ipaddress.IPv4Address(host)
            if str(address) != host:
                raise ValueError('Non-canonical IPv4 representation')
        except ValueError as exc:
            raise URLRejected('Non-canonical numeric IP hostname') from exc
    # Preserve query parameter order and semantics; strip fragment only.
    path = quote(url.path or '/', safe="/%:@!$&'()*+,;=-._~")
    query = quote(url.query, safe="%:@!$&'()*+,;=/?-._~")
    default = (url.scheme.lower() == 'http' and port == 80) or (url.scheme.lower() == 'https' and port == 443)
    authority = f'[{host}]' if ':' in host else host
    if not default:
        authority += f':{port}'
    return urlunsplit((url.scheme.lower(), authority, path, query, ''))

def origin(url: str) -> str:
    p = urlsplit(url)
    host = f'[{p.hostname}]' if ':' in (p.hostname or '') else p.hostname
    port = p.port or (443 if p.scheme == 'https' else 80)
    default = 443 if p.scheme == 'https' else 80
    return f'{p.scheme}://{host}{":"+str(port) if port!=default else ""}'

def approved_host(url: str, allowed_hosts: set[str]) -> bool:
    return (urlsplit(url).hostname or '').lower() in allowed_hosts

def resolve_public_ips(hostname: str, port: int, *, allow_private: bool = False) -> list[str]:
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise URLRejected('DNS resolution failed') from exc
    ips = sorted({entry[4][0] for entry in addresses})
    if not ips:
        raise URLRejected('DNS result empty')
    if not allow_private and any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise URLRejected('DNS resolved to private/non-global address')
    return ips
