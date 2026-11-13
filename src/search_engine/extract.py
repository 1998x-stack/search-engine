"""Deterministic bounded HTML/plain-text extraction; no JavaScript or external URLs.

Conservative P0 extraction: retains headings, title, plain text and hints, removes
common boilerplate tags. XML/PDF/JS rendering are deliberately not supported.
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import urljoin, urlsplit

from .urls import URLRejected, normalize_url

VERSION = 'html-stdlib-v1'
SKIP = {'script', 'style', 'nav', 'footer', 'aside', 'form', 'noscript', 'header', 'template', 'svg'}
VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
BLOCK = {'p', 'div', 'section', 'article', 'br', 'li', 'ul', 'ol', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre', 'blockquote', 'tr'}


@dataclass(frozen=True)
class Extracted:
    title: str
    body: str
    normalized_body: str
    language: str
    description: str
    canonical_hint: str | None
    indexable: bool


class _HTMLExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []
        self.title_fragments: list[str] = []
        self.skip_depth = 0
        self.title_depth = 0
        self.head_depth = 0
        self.description = ''
        self.canonical_href: str | None = None
        self.robots: list[str] = []
        self.html_lang = ''

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {str(k).lower(): str(v) for k, v in attrs if v is not None}
        if tag == 'html':
            self.html_lang = attrs_map.get('lang', '').lower()[:32]
        if tag == 'meta':
            key = attrs_map.get('name', '').lower()
            if key == 'description':
                self.description = attrs_map.get('content', '')[:1000]
            elif key in ('robots', 'googlebot'):
                self.robots.append(attrs_map.get('content', ''))
        if tag == 'link' and 'canonical' in attrs_map.get('rel', '').lower().split():
            self.canonical_href = attrs_map.get('href')
        if tag == 'head':
            self.head_depth += 1
        if tag == 'title':
            self.title_depth += 1
        if self.skip_depth or tag in SKIP:
            if tag not in VOID:
                self.skip_depth += 1
            return
        if tag in BLOCK and not self.head_depth:
            self.fragments.append(' ')

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag == 'title':
            self.title_depth = max(0, self.title_depth - 1)
        if tag == 'head':
            self.head_depth = max(0, self.head_depth - 1)
        if tag in BLOCK and not self.head_depth:
            self.fragments.append(' ')

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.title_depth:
            self.title_fragments.append(data)
        elif not self.head_depth:
            self.fragments.append(data)


def whitespace(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def normalized(text: str) -> str:
    # Keep display text unchanged in title/description; normalization is for matching.
    return whitespace(unicodedata.normalize('NFKC', text)).casefold()


def _decode(raw: bytes, kind: str) -> str:
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16', errors='replace')
    if raw.startswith(b'\xef\xbb\xbf'):
        return raw.decode('utf-8-sig', errors='replace')
    encoding = 'utf-8'
    if kind in ('text/html', 'application/xhtml+xml'):
        m = re.search(rb'<meta\s+[^>]*charset\s*=\s*[\'\"]?([\w.-]+)', raw[:4096], re.I)
        if m:
            encoding = m.group(1).decode('ascii', errors='ignore').lower()
    if encoding not in ('utf-8', 'utf8', 'gbk', 'gb18030', 'big5', 'iso-8859-1', 'windows-1252'):
        encoding = 'utf-8'
    return raw.decode(encoding, errors='replace')


def _language(content: str, hint: str) -> str:
    if hint.startswith(('zh', 'en')):
        return 'zh' if hint.startswith('zh') else 'en'
    han = len(re.findall(r'[\u3400-\u9fff]', content))
    latin = len(re.findall(r'[A-Za-z]', content))
    if han and latin and min(han, latin) >= max(han, latin) // 4:
        return 'mixed'
    return 'zh' if han > latin else 'en' if latin else 'und'


def _noindex(value: str) -> bool:
    return bool(re.search(r'(?i)(?:^|[,;\s])noindex(?:$|[,;\s])', value))


def extract(raw: bytes, content_type: str, url: str, *, x_robots_tag: str = '',
            max_text_chars: int = 1_000_000) -> Extracted:
    if content_type not in ('text/html', 'application/xhtml+xml', 'text/plain'):
        raise ValueError(f'Unsupported document media type: {content_type}')
    text = _decode(raw, content_type)
    parser: _HTMLExtractor | None = None
    if content_type in ('text/html', 'application/xhtml+xml'):
        parser = _HTMLExtractor()
        parser.feed(text)
        title = whitespace(' '.join(parser.title_fragments))[:1000]
        body = whitespace(''.join(parser.fragments))[:max_text_chars]
        description = whitespace(parser.description)
        hint = parser.html_lang
        noindex = _noindex(' '.join(parser.robots)) or _noindex(x_robots_tag)
    else:
        title = ''
        body = whitespace(text)[:max_text_chars]
        description, hint = '', ''
        noindex = _noindex(x_robots_tag)
    canonical = None
    if parser and parser.canonical_href:
        try:
            candidate = normalize_url(urljoin(url, parser.canonical_href),
                                      allowed_ports={urlsplit(url).port or (443 if url.startswith('https:') else 80)})
            # Canonical tag is metadata, never the authoritative document identity.
            if urlsplit(candidate).hostname == urlsplit(url).hostname:
                canonical = candidate
        except URLRejected:
            pass
    return Extracted(title=title, body=body, normalized_body=normalized(body),
                     language=_language(body, hint), description=description,
                     canonical_hint=canonical, indexable=bool(body) and not noindex)
