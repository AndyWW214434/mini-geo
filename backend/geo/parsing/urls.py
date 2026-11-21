"""Citation URL normalisation: unwrap redirects, drop tracking, derive the domain."""

from __future__ import annotations

import base64
import binascii
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import tldextract

# Offline: use the public suffix list bundled with tldextract, never fetch at runtime.
_extract = tldextract.TLDExtract(suffix_list_urls=())

TRACKING_PARAMS = {"gclid", "fbclid", "msclkid", "srsltid", "ref", "ref_src", "mc_cid", "mc_eid"}


def _unwrap_once(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    qs = parse_qs(parts.query)
    # Google: /url?q=<target> or /url?url=<target>
    if host.startswith("google.") and parts.path == "/url":
        for key in ("q", "url"):
            if qs.get(key) and qs[key][0].startswith("http"):
                return qs[key][0]
    # Bing: /ck/a?...&u=a1<base64url(target)>
    if host == "bing.com" and parts.path.startswith("/ck/") and qs.get("u"):
        token = qs["u"][0]
        if token.startswith("a1"):
            token = token[2:]
            try:
                decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
                if decoded.startswith("http"):
                    return decoded
            except (binascii.Error, UnicodeDecodeError):
                return None
    # DuckDuckGo: /l/?uddg=<target>
    if host == "duckduckgo.com" and qs.get("uddg"):
        return unquote(qs["uddg"][0])
    return None


def normalize_url(url: str) -> str | None:
    url = (url or "").strip()
    for _ in range(3):  # redirects can be nested
        inner = _unwrap_once(url)
        if not inner:
            break
        url = inner
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS])
    netloc = parts.hostname.lower()
    if parts.port and parts.port not in (80, 443):
        netloc += f":{parts.port}"
    return urlunsplit(("https", netloc, parts.path or "/", query, ""))


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for prefix in ("www.", "m."):
        host = host.removeprefix(prefix)
    return host


def domain_of(url: str) -> str:
    """Registrable domain: docs.stripe.com -> stripe.com, en.wikipedia.org -> wikipedia.org."""
    ext = _extract(url)
    return (ext.top_domain_under_public_suffix if hasattr(ext, "top_domain_under_public_suffix")
            else ext.registered_domain) or host_of(url)
