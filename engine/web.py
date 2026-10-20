"""Lane W — web app / SPA surface inspection.

Static-only: fetch the HTML and its own JS/CSS bundles over HTTP, then
fingerprint the framework, libraries, routes, API endpoints, auth methods,
config/env surface and analytics — all from bytes we actually retrieved.

Nothing on the target is executed; JS is read as text, never run. Every
finding carries its exact `curl` reproduction command and a blake2b digest.
"""
from __future__ import annotations

import re
import shlex
import urllib.request
from urllib.parse import urljoin, urlparse

from .shell import slug

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# third-party tag hosts we LIST (analytics) but never deep-analyze as app code
_TAG_HOSTS = (
    "googletagmanager", "google-analytics", "doubleclick", "clarity.ms",
    "hs-analytics", "hs-scripts", "hsforms", "hubspot", "snap.licdn",
    "licdn.com", "facebook.net", "connect.facebook", "pendo.io", "hotjar",
    "segment.io", "amplitude", "mixpanel", "intercom", "fullstory",
    "mouseflow", "heap.io", "reddit", "quora", "outbrain", "taboola",
)

# framework -> (marker substrings, version regex)
_FRAMEWORKS = [
    ("nextjs", ["__NEXT_DATA__", "_next/static", "next/dist"], re.compile(r"next@(\d+\.\d+\.\d+)")),
    ("nuxt", ["__NUXT__", "_nuxt/"], re.compile(r"nuxt@(\d+\.\d+\.\d+)")),
    ("vue", ["__vue__", "vendor-vue", "new Vue", "Vue.component", "vue@"], re.compile(r"[Vv]ue(?:\.js)?\s*v?(\d+\.\d+\.\d+)")),
    ("react", ["react.production", "react-dom", "react@", "data-reactroot"], re.compile(r"react(?:-dom)?@(\d+\.\d+\.\d+)")),
    ("angular", ["@angular/core", "ng-version"], re.compile(r"ng-version=[\"']?(\d+\.\d+\.\d+)")),
    ("svelte", ["svelte/internal", "svelte@"], re.compile(r"svelte@(\d+\.\d+\.\d+)")),
]

# well-known libraries signalled by vendor-chunk names or content markers
_LIB_MARKERS = {
    "lodash": ["vendor-lodash", "lodash@", "lodash/core"],
    "moment": ["vendor-moment", "moment@", "moment.min"],
    "libphonenumber": ["vendor-libphonenumber", "libphonenumber"],
    "axios": ["axios@", "axios/dist"],
    "redux": ["redux@", "redux/dist"],
    "vuex": ["vuex@", "vuex/dist"],
    "vue-router": ["vue-router@", "vue-router/dist"],
    "tailwind": ["tailwindcss", "tailwind.config"],
    "bootstrap": ["bootstrap@", "bootstrap/dist", "mdbootstrap"],
    "jquery": ["jquery@", "jquery.min"],
    "d3": ["d3@", "d3.min", "d3-selection"],
    "three": ["three@", "three.min", "three.module"],
    "mapbox": ["mapbox-gl", "mapbox.com"],
    "leaflet": ["leaflet@", "leaflet/dist"],
}

_URL_RE = re.compile(r"https?://[a-z0-9.-]+\.[a-z]{2,}[^\s\"'<>)]*")
_ROUTE_RE = re.compile(r"""["'](/[a-zA-Z][a-zA-Z0-9/_.-]{1,60})["']""")
_API_RE = re.compile(r"""["'](/api/[a-zA-Z0-9/_.${}-]{1,80})["']""")
_ENV_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,40})\s*[:=]")
_OAUTH_RE = re.compile(
    r"\b(oauth|oauth2|openid|signin|sign-in|magic.?link|passwordless|sso|saml|"
    r"google_sign_in|appleid|facebook_login|github_login|totp|2fa)\b", re.I)

_SCRIPT_SRC = re.compile(r"""<script[^>]+src=["']([^"']+)["']""")
_LINK_HREF = re.compile(r"""<link[^>]+href=["']([^"']+)["']""")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_META_RE = re.compile(r"""<meta[^>]+(?:name|property)=["']([^"']+)["'][^>]+content=["']([^"']*)["']""", re.I)

MAX_BUNDLES = 14
BUNDLE_CAP = 350_000  # bytes per bundle artifact


def _fetch(url: str):
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.geturl(), r.read()
    except Exception as e:  # noqa: BLE001 — boundary, never crash the lane
        return None, url, ("__FETCH_ERROR__ %s" % e).encode()


def _curl(url: str) -> str:
    return "curl -sL %s" % shlex.quote(url)


def _is_tag(url: str) -> bool:
    h = urlparse(url).hostname or ""
    return any(t in h for t in _TAG_HOSTS)


def _extract_env_keys(text: str, known: set):
    """Config/env surface: UPPER_SNAKE keys that look like injected config."""
    for m in _ENV_KEY_RE.finditer(text):
        k = m.group(1)
        if len(k) >= 6 and any(c.isalpha() for c in k):
            known.add(k)


def inspect_web(target: str, bank) -> dict:
    target = target.strip()
    report = {"target": target, "lane": "web"}

    # 1. HTML
    status, final_url, html = _fetch(target)
    if status is None:
        report["error"] = html.decode("utf-8", "replace")
        return report
    report["url"] = final_url
    report["http_status"] = status
    bank.add("web", target, "html (raw)", "curl", _curl(target), html,
             {"bytes": len(html)}, limitation="rendered DOM differs from static HTML; SPA content lives in JS")

    text = html.decode("utf-8", "replace")
    tm = _TITLE_RE.search(text)
    report["title"] = tm.group(1).strip() if tm else None
    metas = {}
    for m in _META_RE.finditer(text):
        metas[m.group(1).lower()] = m.group(2)
    report["meta"] = {k: v for k, v in list(metas.items())[:20]}

    script_urls = [m for m in _SCRIPT_SRC.findall(text)]
    link_urls = [m for m in _LINK_HREF.findall(text)]
    scripts = [urljoin(final_url, s) for s in script_urls if s]
    styles = [urljoin(final_url, s) for s in link_urls if ".css" in s.lower()]
    report["script_count"] = len(scripts)
    report["styles"] = styles[:20]

    # 2. classify scripts: analytics/tags vs app bundles
    tags, app_bundles = [], []
    for s in scripts:
        (tags if _is_tag(s) else app_bundles).append(s)
    report["analytics"] = tags

    # 3. fetch app bundles (bounded) and analyze as text
    lib_hits = set()
    frameworks = {}
    routes = set()
    apis = set()
    env_keys = set()
    domains = set()
    auth_signals = set()
    analyzed = 0
    for b in app_bundles[:MAX_BUNDLES]:
        bstatus, bfinal, raw = _fetch(b)
        if bstatus is None:
            continue
        analyzed += 1
        btext = raw.decode("utf-8", "replace")
        bank.add("web", b, "bundle (bounded)", "curl", _curl(b), raw[:BUNDLE_CAP],
                 {"bytes": len(raw), "bounded": len(raw) > BUNDLE_CAP},
                 limitation="minified bundle; symbol names are heuristically recovered, not resolved")
        low = btext.lower()
        for name, markers, vrx in _FRAMEWORKS:
            if any(m.lower() in low or m.lower() in b for m in markers):
                vm = vrx.search(btext)
                frameworks[name] = {"version": vm.group(1) if vm else None}
        for lib, markers in _LIB_MARKERS.items():
            if any(m.lower() in low for m in markers):
                lib_hits.add(lib)
        for r in _ROUTE_RE.findall(btext):
            if not r.startswith("/node_modules"):
                routes.add(r)
        for a in _API_RE.findall(btext):
            apis.add(a)
        _extract_env_keys(btext, env_keys)
        for d in _URL_RE.findall(btext):
            h = urlparse(d).hostname
            if h and h not in ("www.w3.org", "schema.org"):
                domains.add(h)
        for am in _OAUTH_RE.findall(btext):
            auth_signals.add(am.lower())

    # also scan the HTML itself for env + auth + routes
    _extract_env_keys(text, env_keys)
    for r in _ROUTE_RE.findall(text):
        routes.add(r)
    for a in _API_RE.findall(text):
        apis.add(a)
    for d in _URL_RE.findall(text):
        h = urlparse(d).hostname
        if h:
            domains.add(h)
    for am in _OAUTH_RE.findall(text):
        auth_signals.add(am.lower())

    report["bundles_analyzed"] = analyzed
    report["framework"] = frameworks or None
    report["libraries"] = sorted(lib_hits)
    report["routes"] = sorted(routes)[:60]
    report["api_endpoints"] = sorted(apis)[:60]
    report["env_config_keys"] = sorted(env_keys)[:80]
    report["domains"] = sorted(domains)[:60]
    report["auth_signals"] = sorted(auth_signals)[:20]

    return report
