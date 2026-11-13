"""Conservative HTTP/HTTPS fetcher, DNS-pinned to a validated resolved IP.

No browser execution, cookie jar, credential forwarding, proxy evasion or auto-redirect.
"""
from __future__ import annotations
from dataclasses import dataclass
import http.client
import socket
import ssl
from urllib.parse import urlsplit
import zlib
from .urls import normalize_url, resolve_public_ips, URLRejected

class FetchError(RuntimeError):
    pass

@dataclass(frozen=True)
class FetchResponse:
    status: int
    url: str
    headers: dict[str, str]
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get('content-type', '').split(';', 1)[0].lower()

class SafeFetcher:
    def __init__(self, *, user_agent: str,
                 timeout_s: float = 8.0, max_wire_bytes: int = 2_000_000,
                 max_body_bytes: int = 5_000_000,
                 allow_private_ips: bool = False, allowed_ports: set[int] | None = None):
        if not user_agent or user_agent.lower().startswith('mozilla/'):
            raise ValueError('Provide an honest named crawler User-Agent')
        if timeout_s <= 0 or max_wire_bytes <= 0 or max_body_bytes <= 0:
            raise ValueError('Invalid fetch limits')
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        self.max_wire_bytes = max_wire_bytes
        self.max_body_bytes = max_body_bytes
        self.allow_private_ips = allow_private_ips
        self.allowed_ports = allowed_ports or {80, 443}

    def fetch(self, url: str, *, etag: str | None = None,
              last_modified: str | None = None) -> FetchResponse:
        url = normalize_url(url, allowed_ports=self.allowed_ports)
        parsed = urlsplit(url)
        host = parsed.hostname
        assert host is not None
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        try:
            vetted = resolve_public_ips(host, port, allow_private=self.allow_private_ips)
        except URLRejected as exc:
            raise FetchError(str(exc)) from exc
        # The chosen IP is used for the socket, avoiding DNS-rebinding after validation.
        ip = vetted[0]
        conn = (http.client.HTTPSConnection(host, port, timeout=self.timeout_s,
                                            context=ssl.create_default_context())
                if parsed.scheme == 'https'
                else http.client.HTTPConnection(host, port, timeout=self.timeout_s))
        try:
            raw_sock = socket.create_connection((ip, port), timeout=self.timeout_s)
            raw_sock.settimeout(self.timeout_s)
            conn.sock = (ssl.create_default_context().wrap_socket(raw_sock, server_hostname=host)
                         if parsed.scheme == 'https' else raw_sock)
            target = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
            headers = {
                'User-Agent': self.user_agent,
                'Host': parsed.netloc,
                'Accept': 'text/html,application/xhtml+xml,text/plain,application/xml;q=0.8,*/*;q=0.1',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'close',
            }
            if etag:
                headers['If-None-Match'] = etag[:512]
            if last_modified:
                headers['If-Modified-Since'] = last_modified[:128]
            conn.request('GET', target, headers=headers)
            response = conn.getresponse()
            response_headers = {name.lower(): value for name, value in response.getheaders()}
            compressed = bytearray()
            while True:
                block = response.read(min(65536, self.max_wire_bytes + 1 - len(compressed)))
                if not block:
                    break
                compressed.extend(block)
                if len(compressed) > self.max_wire_bytes:
                    raise FetchError('Compressed response exceeds wire byte limit')
            encoding = response_headers.get('content-encoding', '').lower().strip()
            if encoding in ('gzip', 'deflate') and compressed:
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == 'gzip' else zlib.MAX_WBITS)
                try:
                    content = decoder.decompress(bytes(compressed), self.max_body_bytes + 1)
                    if decoder.unconsumed_tail or not decoder.eof:
                        raise FetchError('Encoded body exceeds limits or is incomplete')
                    content += decoder.flush(self.max_body_bytes + 1 - len(content))
                except zlib.error as exc:
                    raise FetchError('Malformed encoded response') from exc
            elif encoding in ('', 'identity'):
                content = bytes(compressed)
            else:
                raise FetchError('Unsupported content encoding')
            if len(content) > self.max_body_bytes:
                raise FetchError('Decoded body exceeds limit')
            return FetchResponse(status=response.status, url=url,
                                 headers=response_headers, body=content)
        except (OSError, http.client.HTTPException, TimeoutError, ssl.SSLError) as exc:
            raise FetchError(f'HTTP transport failed: {type(exc).__name__}') from exc
        finally:
            conn.close()
