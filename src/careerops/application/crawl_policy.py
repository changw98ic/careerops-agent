"""Crawl policy: determines whether a URL may be fetched (M1 safety)."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from careerops.domain.crawl import CrawlDecision, CrawlPolicyDecision, CrawlPolicyInput

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_METADATA_IPS = {"169.254.169.254", "metadata.google.internal"}

_ALLOWED_SCHEMES = frozenset({"http", "https"})

_BLOCKED_PATTERNS = [
    re.compile(r"^localhost$", re.IGNORECASE),
    re.compile(r"\.local$", re.IGNORECASE),
    re.compile(r"^metadata\.google\.internal$", re.IGNORECASE),
]


def evaluate_crawl_policy(input: CrawlPolicyInput) -> CrawlPolicyDecision:
    """Evaluate whether a crawl is allowed based on safety rules."""
    if input.terms_status == "blocked":
        return CrawlPolicyDecision(
            decision=CrawlDecision.DENY_BLOCKED,
            reason="Company terms explicitly block crawling",
        )

    if input.is_rate_limited:
        return CrawlPolicyDecision(
            decision=CrawlDecision.DENY_RATE_LIMITED,
            reason="Domain is currently rate-limited",
        )

    ssrf_check = check_ssrf(input.source_url)
    if ssrf_check is not None:
        return ssrf_check

    if input.terms_status == "unknown":
        return CrawlPolicyDecision(
            decision=CrawlDecision.DENY_TERMS_UNKNOWN,
            reason="Terms status unknown; only one discovery request allowed",
        )

    return CrawlPolicyDecision(
        decision=CrawlDecision.ALLOW,
        reason="Crawl allowed",
    )


def check_ssrf(url: str) -> CrawlPolicyDecision | None:
    """Check URL for SSRF risks. Returns a deny decision if risky, None if safe."""
    parts = urlsplit(url)

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return CrawlPolicyDecision(
            decision=CrawlDecision.DENY_SSRF,
            reason=f"Scheme '{parts.scheme}' not in allowlist",
        )

    hostname = parts.hostname or ""
    if not hostname:
        return CrawlPolicyDecision(
            decision=CrawlDecision.DENY_SSRF,
            reason="No hostname in URL",
        )

    for pattern in _BLOCKED_PATTERNS:
        if pattern.match(hostname):
            return CrawlPolicyDecision(
                decision=CrawlDecision.DENY_SSRF,
                reason=f"Hostname '{hostname}' matches blocked pattern",
            )

    if hostname in _METADATA_IPS:
        return CrawlPolicyDecision(
            decision=CrawlDecision.DENY_SSRF,
            reason="Metadata endpoint blocked",
        )

    try:
        addr = ipaddress.ip_address(hostname)
        for network in _PRIVATE_NETWORKS:
            if addr in network:
                return CrawlPolicyDecision(
                    decision=CrawlDecision.DENY_SSRF,
                    reason=f"IP {hostname} is in private/reserved range",
                )
    except ValueError:
        pass

    return None
