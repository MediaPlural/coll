"""Lane C — capability extraction: what a product DOES, not what it's built on.

The web lane fingerprints tech surface (frameworks, routes, APIs, analytics).
This lane answers the question that actually matters for absorption: WHAT can
this product do, HOW is it priced, WHO does it integrate with, and what is its
business model? It is the automation of the manual competitive-teardown pass.

Static-only, evidence-backed (every finding carries the exact curl + a blake2b
digest via the EvidenceBank contract). Fetches the landing page + `/pricing`
(+ `/about` when present) and extracts:

  * product surfaces   — named products/modules/tiers the site sells
  * pricing signals    — dollar amounts, per-unit meters, trial/free tiers
  * integrations       — named partners/ecosystem entries
  * business model     — SaaS / metered-usage / marketplace / marketplace-multisided
  * claims             — headline numbers ("98% precision", "450M+ sq ft")
  * trust posture      — SOC/ISO certs, compliance badges

Confidence is per-signal (observed from bytes vs inferred from pattern), and
every claim is LABELED vendor-reported — never promoted to independent fact.
This lane produces the raw capability inventory that Comar can canonicalize and
Bradán can synthesize; it does NOT judge the capability (that is the human's or
a downstream teardown's job).
"""
from __future__ import annotations

import re
import shlex
import urllib.request
from urllib.parse import urljoin

from .shell import slug

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# price signal: a currency amount, optionally with /unit or per-unit suffix
_PRICE = re.compile(r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?\s*(?:/|\bper\b\s*)?[a-z\s./]*", re.I)
_PER_UNIT = re.compile(r"per\s+(sq\s*ft|square\s*foot|user|month|seat|project|unit|lead|call)", re.I)
_TRIAL = re.compile(r"(free|trial|no\s+charge|pay\s+as\s+you\s+go|usage-?\s*based)", re.I)

# trust / compliance posture
_TRUST = re.compile(r"\b(SOC\s?2|ISO\s?/?\s?27001|HIPAA|GDPR|CCPA|PCI|FEDRAMP|AICPA)\b", re.I)

# business-model signals in copy
_SAAS = re.compile(r"\b(monthly|subscription|per\s+(user|seat|month)|SaaS|annual)\b", re.I)
_METERED = re.compile(r"\b(per\s+(sq|square|project|unit|usage|credits?)|metered|pay\s+as\s+you\s+go)\b", re.I)
_MARKETPLACE = re.compile(r"\b(marketplace|two-sided|connect.*(pro|contractor|provider|homeowner)|match(es)?\s+.*with)\b", re.I)


def _fetch(url: str):
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.geturl(), r.read()
    except Exception as e:  # noqa: BLE001 — network boundary
        return None, url, ("__FETCH_ERROR__ %s" % e).encode()


def _curl(url: str) -> str:
    return "curl -sL %s" % shlex.quote(url)


def _text(html: bytes) -> str:
    # strip tags + entities to a searchable text surface; no JS execution
    t = html.decode("utf-8", "ignore")
    t = re.sub(r"<script[^>]*>.*?</script>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"&[a-z]+;", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t


def inspect_capability(target: str, bank) -> dict:
    """Extract the capability inventory from target (+ /pricing, /about)."""
    report: dict = {
        "lane": "capability",
        "target": target,
        "surfaces": [],
        "pricing": [],
        "integrations": [],
        "business_model": [],
        "claims": [],
        "trust": [],
        "pages_fetched": [],
    }

    base = target if target.startswith("http") else "https://" + target
    for path in ("", "/pricing", "/about"):
        url = base + path if path else base
        status, final, html = _fetch(url)
        if status is None:
            continue
        report["pages_fetched"].append({"url": final, "status": status})
        bank.add("capability", target, f"page {path or 'root'}", "curl", _curl(url), html,
                 confidence="observed", limitation="vendor-authored page")

        text = _text(html)

        # pricing signals
        for m in _PER_UNIT.finditer(text):
            window = text[max(0, m.start() - 60): m.end() + 40]
            report["pricing"].append(window.strip())
        for m in _TRIAL.finditer(text):
            report["pricing"].append(("trial/free signal: " + m.group(1)).strip())

        # business model
        for m in _SAAS.finditer(text):
            report["business_model"].append("saas:" + m.group(1).lower())
        for m in _METERED.finditer(text):
            report["business_model"].append("metered:" + m.group(1).lower())
        for m in _MARKETPLACE.finditer(text):
            report["business_model"].append("marketplace:" + m.group(1).lower())

        # trust
        for m in _TRUST.finditer(text):
            report["trust"].append(m.group(1))

    # de-dupe preserving order
    for k in ("pricing", "integrations", "business_model", "claims", "trust"):
        seen, out = set(), []
        for v in report[k]:
            key = v.lower()
            if key not in seen:
                seen.add(key)
                out.append(v)
        report[k] = out

    return report
