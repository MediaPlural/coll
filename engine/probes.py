"""In-page probes — the JS the behaviour lane executes inside the debuggee.

Each probe is a single self-evaluating expression returning plain JSON data, so
it works with `Runtime.evaluate(returnByValue=true)` and needs no injection
step, no bundler, no extension. Nothing here mutates app state except the
`data-coll-id` bookkeeping attribute used to click a control deterministically.

Reading these probes top to bottom is reading the lane's definition of a
"screen": visible regions (nav/aside/main/dialog…), visible controls with the
region that owns them, the app's own storage keys, and the DOM's headings.
"""
from __future__ import annotations

import json

# Visible-element predicates shared by every probe (inlined per probe: each
# evaluation is independent, so no cross-call state to break).
_PRELUDE = r"""
  const __V = (el) => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1 && s.display !== 'none' &&
           s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0.05 &&
           el.getClientRects().length > 0;
  };
  const __B = (el) => { const r = el.getBoundingClientRect();
    return { x: Math.round(r.left), y: Math.round(r.top),
             w: Math.round(r.width), h: Math.round(r.height) }; };
  const __T = (el, n) => (el.innerText || el.textContent || '')
    .replace(/\s+/g, ' ').trim().slice(0, n);
  const __R = (el) => el.getAttribute('role') ||
    ({A:'link', BUTTON:'button', INPUT:'textbox', SELECT:'combobox',
      TEXTAREA:'textbox', H1:'heading', H2:'heading', H3:'heading'}[el.tagName] ||
     el.tagName.toLowerCase());
  const __N = (el) => {
    const a = el.getAttribute('aria-label') || el.getAttribute('title') ||
              el.getAttribute('placeholder') || el.getAttribute('alt') ||
              el.getAttribute('name');
    if (a && String(a).trim()) return String(a).trim().slice(0, 80);
    const lab = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
    if (lab) return __T(lab, 80);
    return __T(el, 60);
  };
  const __STORAGE = (which) => {
    try { return which === 'local' ? window.localStorage : window.sessionStorage; }
    catch (e) { return null; }   // opaque origin (about:blank) throws on access
  };
  const __REG_SEL = 'nav,aside,main,header,footer,dialog,form,' +
    '[role=navigation],[role=complementary],[role=main],[role=dialog],' +
    '[role=menu],[role=tablist],[role=tree],[role=listbox],[role=search],' +
    '[role=toolbar],[role=feed],[role=grid],[role=region]';
  const __CTL_SEL = 'a[href],button,input:not([type=hidden]),select,textarea,' +
    '[role=button],[role=link],[role=menuitem],[role=menuitemcheckbox],' +
    '[role=checkbox],[role=switch],[role=tab],[role=treeitem],[role=option],' +
    '[contenteditable="true"],[tabindex]:not([tabindex="-1"])';
"""


def _expr(body: str) -> str:
    return "(() => {" + _PRELUDE + body + "})()"


# A screen snapshot: what a human sees and can touch right now.
SNAPSHOT = _expr(r"""
  const regions = [];
  const seen = new Set();
  document.querySelectorAll(__REG_SEL).forEach((el) => {
    if (!__V(el)) return;
    const b = __B(el);
    if (b.w < 60 || b.h < 40) return;
    const key = __R(el) + '|' + (el.getAttribute('aria-label') || '') + '|' +
                el.tagName.toLowerCase();
    if (seen.has(key + b.x + b.y)) return;
    seen.add(key + b.x + b.y);
    regions.push({ key, tag: el.tagName.toLowerCase(), role: __R(el),
      label: (el.getAttribute('aria-label') || '').slice(0, 60),
      bbox: b, area: b.w * b.h, text_len: __T(el, 4000).length });
  });
  regions.sort((a, b) => b.area - a.area);
  const top = regions.slice(0, 24);

  const owner = (el) => {
    for (const r of top) {
      const hit = el.closest(__REG_SEL);
      if (hit && __R(hit) + '|' + (hit.getAttribute('aria-label') || '') + '|' +
                  hit.tagName.toLowerCase() === r.key) return r.key;
    }
    return null;
  };

  const controls = [];
  let i = 0;
  document.querySelectorAll(__CTL_SEL).forEach((el) => {
    if (!__V(el)) return;
    if (controls.length >= 220) return;
    const b = __B(el);
    if (b.w < 6 || b.h < 6) return;
    const id = 'c' + (i++);
    try { el.setAttribute('data-coll-id', id); } catch (e) { return; }
    controls.push({
      id, tag: el.tagName.toLowerCase(), role: __R(el), name: __N(el),
      bbox: b, owner: owner(el),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      expanded: el.getAttribute('aria-expanded'),
      editable: el.isContentEditable ||
                ['input', 'textarea'].includes(el.tagName.toLowerCase()),
      href: el.getAttribute('href') ?
            new URL(el.getAttribute('href'), location.href).pathname.slice(0, 80) : null,
    });
  });

  const storage = (store) => {
    const out = {};
    if (!store) return out;
    try {
      for (let k = 0; k < store.length && Object.keys(out).length < 60; k++) {
        const key = store.key(k);
        const v = store.getItem(key) || '';
        out[key.slice(0, 80)] = v.length;
      }
    } catch (e) {}
    return out;
  };

  return {
    url: location.href,
    origin: location.origin,
    path: location.pathname,
    title: document.title,
    ready_state: document.readyState,
    viewport: { w: innerWidth, h: innerHeight, scroll_y: Math.round(scrollY),
                dpr: devicePixelRatio },
    regions: top.map((r) => ({ key: r.key, tag: r.tag, role: r.role,
      label: r.label, bbox: r.bbox, area: r.area })),
    controls,
    headings: Array.from(document.querySelectorAll('h1,h2,h3'))
      .filter(__V).slice(0, 20).map((h) => __T(h, 80)),
    counts: { dom_nodes: document.querySelectorAll('*').length,
              controls: controls.length, regions: top.length,
              inputs: document.querySelectorAll('input,textarea').length,
              dialogs: Array.from(document.querySelectorAll('dialog[open],[role=dialog]'))
                .filter(__V).length },
    storage: { local: storage(__STORAGE('local')),
               session: storage(__STORAGE('session')) },
    scroll: { y: Math.round(scrollY),
              height: Math.round(document.documentElement.scrollHeight) },
  };
""")


# Arm the observers: DOM mutations, SPA history changes, and fetch/XHR calls
# made by the page's own JS (a second, page-level view of the API graph that
# survives even when CDP's network events miss a served-from-cache call).
ARM = _expr(r"""
  const st = window.__collState = window.__collState || {
    mutations: 0, added: 0, removed: 0, attrs: 0, nav: [], fetches: [], errors: [],
    armed: true,
  };
  if (!window.__collObserver) {
    window.__collObserver = new MutationObserver((recs) => {
      for (const rec of recs) {
        st.mutations++;
        st.added += rec.addedNodes ? rec.addedNodes.length : 0;
        st.removed += rec.removedNodes ? rec.removedNodes.length : 0;
        if (rec.type === 'attributes') st.attrs++;
      }
    });
    window.__collObserver.observe(document.documentElement, {
      subtree: true, childList: true, attributes: true, attributeOldValue: false,
    });
  }
  if (!window.__collNavHooked) {
    window.__collNavHooked = true;
    for (const fn of ['pushState', 'replaceState']) {
      const orig = history[fn];
      history[fn] = function () {
        const r = orig.apply(this, arguments);
        st.nav.push(String(arguments[2] || location.href));
        return r;
      };
    }
    addEventListener('popstate', () => st.nav.push('popstate:' + location.href));
  }
  if (!window.__collFetchHooked && window.fetch) {
    window.__collFetchHooked = true;
    const of = window.fetch;
    window.fetch = function (input, init) {
      const url = (typeof input === 'string') ? input : (input && input.url) || '';
      const method = (init && init.method) || (input && input.method) || 'GET';
      st.fetches.push({ url: String(url).slice(0, 400), method: String(method).toUpperCase() });
      return of.apply(this, arguments);
    };
  }
  return true;
""")


# Delta since the last read: cleared on read so each interaction gets its own.
DELTA = _expr(r"""
  const st = window.__collState || { mutations: 0, added: 0, removed: 0,
    attrs: 0, nav: [], fetches: [], errors: [] };
  const out = { mutations: st.mutations, added: st.added, removed: st.removed,
    attrs: st.attrs, nav: st.nav.slice(0, 20), fetches: st.fetches.slice(0, 40) };
  st.mutations = 0; st.added = 0; st.removed = 0; st.attrs = 0;
  st.nav = []; st.fetches = [];
  return out;
""")


# Resolve one data-coll-id to clickable viewport coordinates.
TARGET_BODY = r"""
  const id = __ID__;
  const el = document.querySelector('[data-coll-id="' + id + '"]');
  if (!el) return { found: false };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const r = el.getBoundingClientRect();
  const cx = Math.round(r.left + r.width / 2), cy = Math.round(r.top + r.height / 2);
  const top = document.elementFromPoint(cx, cy);
  return { found: true, x: cx, y: cy,
           w: Math.round(r.width), h: Math.round(r.height),
           covered: !!(top && top !== el && !el.contains(top) && !top.contains(el)),
           name: __N(el), role: __R(el), tag: el.tagName.toLowerCase(),
           disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true') };
"""


# Resolve a human-ish target spec: selector, text=, role=, or id=.
RESOLVE_BODY = r"""
  const spec = __SPEC__;
  const fail = (why) => ({ found: false, why });
  const visible = (el) => __V(el);
  let el = null;
  if (spec.selector) {
    el = document.querySelector(spec.selector);
    if (el && !(el.closest(__CTL_SEL) === el)) {
      el = el.closest(__CTL_SEL) || el;
    }
  } else if (spec.text) {
    const want = String(spec.text).toLowerCase();
    const area = (e) => {
      const r = e.getBoundingClientRect();
      return r.width * r.height;
    };
    // Match on the element's own accessible name, then climb to the nearest
    // thing a human can click. Real apps nest text inside <yt-formatted-string>
    // style custom elements, so "the deepest match" is usually not clickable.
    const hits = [];
    const all = document.querySelectorAll('*');
    for (const e of all) {
      if (!visible(e)) continue;
      if (!__N(e).toLowerCase().includes(want)) continue;
      const clickable = e.closest(__CTL_SEL);
      hits.push(clickable || e);
      if (hits.length > 400) break;
    }
    const uniq = Array.from(new Set(hits)).filter((e) => visible(e));
    uniq.sort((a, b) => area(a) - area(b));
    el = uniq[0] || null;
  } else if (spec.id) {
    el = document.querySelector('[data-coll-id="' + spec.id + '"]');
  } else if (spec.role) {
    const want = String(spec.role).toLowerCase();
    const nm = spec.name ? String(spec.name).toLowerCase() : null;
    el = Array.from(document.querySelectorAll(__CTL_SEL))
      .filter((e) => visible(e) && __R(e).toLowerCase() === want &&
                     (!nm || __N(e).toLowerCase().includes(nm)))[0];
  }
  if (!el) return fail('no element matched');
  const id = el.getAttribute('data-coll-id') ||
             ('t' + Math.random().toString(36).slice(2, 8));
  el.setAttribute('data-coll-id', id);
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const r = el.getBoundingClientRect();
  return { found: true, id, name: __N(el), role: __R(el),
           tag: el.tagName.toLowerCase(),
           x: Math.round(r.left + r.width / 2),
           y: Math.round(r.top + r.height / 2), text: __T(el, 120) };
"""


# Type into a field without CDP Input.insertText (some Electron builds ignore
# it). Uses the native value setter so React/Vue controlled inputs notice.
TYPE_BODY = r"""
  const v = __VALUE__;
  const id = __ID__;
  const el = id ? document.querySelector('[data-coll-id="' + id + '"]')
                : document.activeElement;
  if (!el) return { ok: false };
  el.focus();
  if (el.isContentEditable) {
    el.textContent = v;
  } else {
    const proto = el.tagName === 'TEXTAREA' ?
      HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, v);
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { ok: true,
           value: String(el.value !== undefined ? el.value : el.textContent).slice(0, 120) };
"""


# Page-level JSON storage shapes: which keys hold structured data (the app's
# own client-side model), by key name + top-level shape only — never values.
STORAGE_SHAPES = _expr(r"""
  const shape = (v) => {
    if (v === null) return 'null';
    if (Array.isArray(v)) return 'array[' + (v.length ? shape(v[0]) : '?') + ']';
    if (typeof v === 'object') {
      const keys = Object.keys(v).slice(0, 25);
      const out = {};
      for (const k of keys) out[k] = shape(v[k]);
      return out;
    }
    return typeof v;
  };
  const read = (store) => {
    const out = {};
    if (!store) return out;
    try {
      for (let i = 0; i < store.length && Object.keys(out).length < 40; i++) {
        const k = store.key(i);
        const raw = store.getItem(k) || '';
        let parsed = null, ok = false;
        if (/^[\[{]/.test(raw.trim())) { try { parsed = JSON.parse(raw); ok = true; } catch (e) {} }
        out[k.slice(0, 80)] = ok ? { json: true, bytes: raw.length,
          shape: shape(parsed) } : { json: false, bytes: raw.length };
      }
    } catch (e) {}
    return out;
  };
  return { local: read(__STORAGE('local')), session: read(__STORAGE('session')),
           cookies: (() => { try { return (document.cookie || '').split(';').map((c) =>
             c.split('=')[0].trim()).filter(Boolean).slice(0, 30); }
             catch (e) { return []; } })() };
""")


def target_probe(coll_id: str) -> str:
    """Resolve `data-coll-id` -> click coordinates in the live page."""
    return _expr("const __ID__ = %s;\n" % _js_str(coll_id) + TARGET_BODY)


def resolve_probe(spec: dict) -> str:
    """Resolve a step's target spec ({selector|text|role|id}) to an element."""
    return _expr("const __SPEC__ = %s;\n" % json.dumps(spec) + RESOLVE_BODY)


def type_probe(coll_id, value: str) -> str:
    """Set a field's value the way a user keystroke would register."""
    head = "const __ID__ = %s; const __VALUE__ = %s;\n" % (_js_str(coll_id),
                                                          json.dumps(value))
    return _expr(head + TYPE_BODY)


def _js_str(s) -> str:
    return json.dumps(s if s else None)
