// The browser's DOM and Web APIs, written in JavaScript on top of one native
// function (__native) that browser/js.py provides. This file runs first in
// every page's QuickJS context; it removes __native from the global scope so
// page scripts can only use the API defined here.
(function () {
'use strict';
const G = globalThis;
const N = G.__native;
delete G.__native;

const J = (s) => (typeof s === 'string') ? JSON.parse(s) : (s === undefined ? null : s);
const str = (v) => String(v);
const now0 = Date.now();

class DOMException extends Error {
  constructor(message, name) { super(message === undefined ? '' : String(message)); this.name = name || 'Error'; }
}

// Every native call answers {"v": value} or {"e": "ErrorName: message"}.
function native(...args) {
  const r = JSON.parse(N(...args));
  if (!('e' in r)) return r.v;
  const m = /^(\w+Error): ?([\s\S]*)$/.exec(r.e);
  if (m && m[1] === 'TypeError') throw new TypeError(m[2]);
  if (m && m[1] === 'RangeError') throw new RangeError(m[2]);
  if (m) throw new DOMException(m[2], m[1]);
  throw new Error(r.e);
}

function report(e) {
  let msg;
  try { msg = (e && e.stack) ? String(e) + '\n' + String(e.stack) : String(e); } catch (_) { msg = 'error'; }
  try { N('log', 'error', 'Uncaught ' + msg); } catch (_) {}
}

// ---------------------------------------------------------------- wrappers
const H = new WeakMap();   // wrapper object -> node handle
const INFO = new WeakMap(); // wrapper object -> [nodeType, localName] (never change)
const W = new Map();       // node handle -> wrapper object (one wrapper per node)
const TAGCLASS = Object.create(null);

function hOf(w) {
  const h = H.get(w);
  if (h === undefined) throw new TypeError("parameter is not of type 'Node'");
  return h;
}
const hOrNull = (w) => (w === null || w === undefined) ? null : hOf(w);

// Nodes arrive as [handle, nodeType, tag] (or a bare handle).
function wrap(ref) {
  if (ref === null || ref === undefined) return null;
  const h = Array.isArray(ref) ? ref[0] : ref;
  let w = W.get(h);
  if (w) return w;
  const [t, tag] = Array.isArray(ref) ? [ref[1], ref[2]] : native('node_info', h);
  let proto;
  if (t === 9) proto = Document.prototype;
  else if (t === 3) proto = Text.prototype;
  else if (t === 8) proto = Comment.prototype;
  else if (t === 11) proto = DocumentFragment.prototype;
  else proto = (TAGCLASS[tag] || HTMLElement).prototype;
  w = Object.create(proto);
  H.set(w, h);
  INFO.set(w, [t, tag]);
  W.set(h, w);
  return w;
}
function nodeList(arr) {
  Object.defineProperty(arr, 'item', { value: (i) => arr[i] === undefined ? null : arr[i] });
  Object.defineProperty(arr, 'namedItem', { value: (n) => arr.find((e) => e.id === n || e.getAttribute('name') === n) || null });
  return arr;
}
const list = (s) => nodeList(J(s).map(wrap));
function toNode(x) {
  return (x !== null && typeof x === 'object' && H.has(x)) ? x : document.createTextNode(str(x));
}

// ------------------------------------------------------------------ events
const LISTENERS = new WeakMap();   // target -> {type: [listener]}
const ONPROPS = new WeakMap();     // target -> {onclick: fn}
const COMPILED = new WeakMap();    // target -> {type: [source, fn]}

class Event {
  constructor(type, init) {
    if (arguments.length === 0) throw new TypeError('Event requires a type');
    init = init || {};
    this.type = str(type);
    this.bubbles = !!init.bubbles;
    this.cancelable = !!init.cancelable;
    this.composed = !!init.composed;
    this.defaultPrevented = false;
    this.target = null;
    this.currentTarget = null;
    this.eventPhase = 0;
    this.isTrusted = false;
    this.timeStamp = Date.now() - now0;
    Object.defineProperty(this, '_s', { value: { stop: false, imm: false, passive: false, path: [] }, writable: true });
  }
  get srcElement() { return this.target; }
  get cancelBubble() { return this._s.stop; }
  set cancelBubble(v) { if (v) this._s.stop = true; }
  get returnValue() { return !this.defaultPrevented; }
  set returnValue(v) { if (!v) this.preventDefault(); }
  preventDefault() { if (this.cancelable && !this._s.passive) this.defaultPrevented = true; }
  stopPropagation() { this._s.stop = true; }
  stopImmediatePropagation() { this._s.stop = true; this._s.imm = true; }
  composedPath() { return this._s.path.slice(); }
  initEvent(type, bubbles, cancelable) { this.type = str(type); this.bubbles = !!bubbles; this.cancelable = !!cancelable; }
}
Object.assign(Event, { NONE: 0, CAPTURING_PHASE: 1, AT_TARGET: 2, BUBBLING_PHASE: 3 });
class CustomEvent extends Event {
  constructor(type, init) { super(type, init); this.detail = init && init.detail !== undefined ? init.detail : null; }
  initCustomEvent(type, b, c, detail) { this.initEvent(type, b, c); this.detail = detail; }
}
class UIEvent extends Event {
  constructor(type, init) { super(type, init); this.view = G; this.detail = (init && init.detail) || 0; }
}
const MOUSE_KEYS = ['clientX', 'clientY', 'screenX', 'screenY', 'pageX', 'pageY', 'offsetX', 'offsetY', 'movementX', 'movementY', 'button', 'buttons'];
const MOD_KEYS = ['ctrlKey', 'shiftKey', 'altKey', 'metaKey'];
class MouseEvent extends UIEvent {
  constructor(type, init) {
    super(type, init); init = init || {};
    for (const k of MOUSE_KEYS) this[k] = +init[k] || 0;
    for (const k of MOD_KEYS) this[k] = !!init[k];
    this.x = this.clientX; this.y = this.clientY;
    this.relatedTarget = init.relatedTarget || null;
  }
  getModifierState(k) { return !!this[k.toLowerCase() + 'Key']; }
}
class PointerEvent extends MouseEvent {
  constructor(type, init) { super(type, init); this.pointerId = 1; this.pointerType = 'mouse'; this.isPrimary = true; }
}
class KeyboardEvent extends UIEvent {
  constructor(type, init) {
    super(type, init); init = init || {};
    this.key = str(init.key || ''); this.code = str(init.code || '');
    this.keyCode = this.which = +init.keyCode || 0; this.charCode = +init.charCode || 0;
    this.repeat = !!init.repeat; this.location = 0; this.isComposing = false;
    for (const k of MOD_KEYS) this[k] = !!init[k];
  }
  getModifierState(k) { return !!this[k.toLowerCase() + 'Key']; }
}
class FocusEvent extends UIEvent {
  constructor(type, init) { super(type, init); this.relatedTarget = (init && init.relatedTarget) || null; }
}
class InputEvent extends UIEvent {
  constructor(type, init) { super(type, init); init = init || {}; this.data = init.data === undefined ? null : init.data; this.inputType = str(init.inputType || ''); this.isComposing = false; }
}
class SubmitEvent extends Event {
  constructor(type, init) { super(type, init); this.submitter = (init && init.submitter) || null; }
}
class ProgressEvent extends Event {
  constructor(type, init) { super(type, init); init = init || {}; this.lengthComputable = !!init.lengthComputable; this.loaded = +init.loaded || 0; this.total = +init.total || 0; }
}
class ErrorEvent extends Event {
  constructor(type, init) { super(type, init); init = init || {}; this.message = str(init.message || ''); this.error = init.error || null; }
}

function listenerOptions(opts) {
  if (typeof opts === 'boolean') return { capture: opts, once: false, passive: false };
  opts = opts || {};
  return { capture: !!opts.capture, once: !!opts.once, passive: !!opts.passive, signal: opts.signal };
}
function addListener(target, type, fn, opts) {
  if (fn === null || fn === undefined) return;
  if (typeof fn !== 'function' && !(typeof fn === 'object')) return;
  const o = listenerOptions(opts);
  if (o.signal && o.signal.aborted) return;
  let m = LISTENERS.get(target);
  if (!m) { m = Object.create(null); LISTENERS.set(target, m); }
  const arr = m[type] || (m[type] = []);
  if (arr.some((l) => l.fn === fn && l.capture === o.capture)) return;
  const l = { fn, capture: o.capture, once: o.once, passive: o.passive, removed: false };
  arr.push(l);
  if (o.signal && typeof o.signal.addEventListener === 'function')
    o.signal.addEventListener('abort', () => removeListener(target, type, fn, o.capture));
}
function removeListener(target, type, fn, capture) {
  const m = LISTENERS.get(target);
  const arr = m && m[type];
  if (!arr) return;
  const i = arr.findIndex((l) => l.fn === fn && l.capture === !!capture);
  if (i >= 0) { arr[i].removed = true; arr.splice(i, 1); }
}

function inlineHandler(t, type) {
  const props = ONPROPS.get(t);
  if (props && ('on' + type) in props) return props['on' + type];
  let h = H.get(t), src = null;
  if (t === G && (type === 'load' || type === 'unload' || type === 'beforeunload')) {
    const body = document.body;
    if (body) { h = H.get(body); }
  }
  if (h === undefined || h === 0) return null;
  if (INFO.get(wrap(h))[0] !== 1) return null;
  src = native('get_attr', h, 'on' + type);
  if (src === null) return null;
  let c = COMPILED.get(t);
  if (!c) { c = Object.create(null); COMPILED.set(t, c); }
  if (c[type] && c[type][0] === src) return c[type][1];
  let fn = null;
  try { fn = new Function('event', src); } catch (e) { report(e); }
  c[type] = [src, fn];
  return fn;
}

function callListener(fn, thisArg, ev) {
  try {
    const r = typeof fn === 'function' ? fn.call(thisArg, ev) : fn.handleEvent(ev);
    return r;
  } catch (e) { report(e); }
  return undefined;
}

function invoke(t, ev, phase) {
  ev.currentTarget = t;
  ev.eventPhase = phase;
  if (phase !== 1) {
    const f = inlineHandler(t, ev.type);
    if (typeof f === 'function') {
      const r = callListener(f, t, ev);
      if (r === false) ev.preventDefault();
      if (ev._s.imm) return;
    }
  }
  const m = LISTENERS.get(t);
  const arr = m && m[ev.type];
  if (!arr) return;
  for (const l of arr.slice()) {
    if (l.removed) continue;
    if (phase === 1 && !l.capture) continue;
    if (phase === 3 && l.capture) continue;
    if (l.once) removeListener(t, ev.type, l.fn, l.capture);
    ev._s.passive = l.passive;
    callListener(l.fn, t, ev);
    ev._s.passive = false;
    if (ev._s.imm) break;
  }
}

function eventParent(n, ev) {
  if (n === G) return null;
  if (n === document) return ev.type === 'load' ? null : G;
  const h = H.get(n);
  if (h === undefined) return null;
  return wrap(native('parent', h));
}

function dispatch(target, ev) {
  if (!(ev instanceof Event)) throw new TypeError("parameter 1 is not of type 'Event'");
  ev.target = target;
  ev._s.stop = false; ev._s.imm = false;
  const path = [];
  for (let n = target; n && path.length < 10000; n = eventParent(n, ev)) path.push(n);
  ev._s.path = path;
  for (let i = path.length - 1; i > 0 && !ev._s.stop; i--) invoke(path[i], ev, 1);
  if (!ev._s.stop) invoke(path[0], ev, 2);
  if (ev.bubbles) for (let i = 1; i < path.length && !ev._s.stop; i++) invoke(path[i], ev, 3);
  ev.currentTarget = null;
  ev.eventPhase = 0;
  return !ev.defaultPrevented;
}

class EventTarget {
  addEventListener(type, fn, opts) { addListener(this == null ? G : this, str(type), fn, opts); }
  removeEventListener(type, fn, opts) { removeListener(this == null ? G : this, str(type), fn, listenerOptions(opts).capture); }
  dispatchEvent(ev) { return dispatch(this == null ? G : this, ev); }
}

const EVENT_NAMES = ['abort', 'blur', 'change', 'click', 'close', 'contextmenu', 'dblclick', 'error', 'focus',
  'focusin', 'focusout', 'input', 'invalid', 'keydown', 'keypress', 'keyup', 'load', 'mousedown', 'mouseenter',
  'mouseleave', 'mousemove', 'mouseout', 'mouseover', 'mouseup', 'pointerdown', 'pointerup', 'pointermove',
  'reset', 'resize', 'scroll', 'select', 'submit', 'toggle', 'wheel', 'touchstart', 'touchend', 'touchmove',
  'animationend', 'transitionend', 'beforeinput'];
const WINDOW_EVENTS = ['load', 'unload', 'beforeunload', 'resize', 'scroll', 'error', 'hashchange', 'popstate',
  'message', 'online', 'offline', 'pageshow', 'pagehide', 'storage', 'focus', 'blur', 'keydown', 'keyup', 'click'];
function defineOnProps(obj, names) {
  for (const name of names) {
    Object.defineProperty(obj, 'on' + name, {
      configurable: true, enumerable: false,
      get() { const p = ONPROPS.get(this == null ? G : this); return p && ('on' + name) in p ? p['on' + name] : (H.has(this) ? inlineHandler(this, name) : null); },
      set(fn) {
        const t = this == null ? G : this;
        let p = ONPROPS.get(t);
        if (!p) { p = Object.create(null); ONPROPS.set(t, p); }
        p['on' + name] = typeof fn === 'function' ? fn : null;
      },
    });
  }
}

// -------------------------------------------------------------------- nodes
function illegal() { throw new TypeError('Illegal constructor'); }

class Node extends EventTarget {
  constructor() { super(); illegal(); }
  get nodeType() { hOf(this); return INFO.get(this)[0]; }
  get nodeName() {
    switch (this.nodeType) {
      case 1: return this.tagName;
      case 3: return '#text';
      case 8: return '#comment';
      case 9: return '#document';
      default: return '#document-fragment';
    }
  }
  get parentNode() { return wrap(native('parent', hOf(this))); }
  get parentElement() { const p = this.parentNode; return p && p.nodeType === 1 ? p : null; }
  get childNodes() { return list(native('children', hOf(this), false)); }
  get firstChild() { const c = J(native('children', hOf(this), false)); return c.length ? wrap(c[0]) : null; }
  get lastChild() { const c = J(native('children', hOf(this), false)); return c.length ? wrap(c[c.length - 1]) : null; }
  get nextSibling() { return this === document ? null : wrap(native('sibling', hOf(this), 1, false)); }
  get previousSibling() { return this === document ? null : wrap(native('sibling', hOf(this), -1, false)); }
  hasChildNodes() { return J(native('children', hOf(this), false)).length > 0; }
  appendChild(c) { native('insert', hOf(this), hOf(c), null); return c; }
  insertBefore(c, ref) { native('insert', hOf(this), hOf(c), hOrNull(ref)); return c; }
  removeChild(c) { native('remove', hOf(this), hOf(c)); return c; }
  replaceChild(n, old) {
    if (n === old) return old;
    native('insert', hOf(this), hOf(n), hOf(old));
    native('remove', hOf(this), hOf(old));
    return old;
  }
  contains(o) { return o === null || o === undefined ? false : native('contains', hOf(this), hOf(o)); }
  cloneNode(deep) { return wrap(native('clone', hOf(this), !!deep)); }
  get textContent() { return native('text_get', hOf(this)); }
  set textContent(v) { if (this !== document) native('text_set', hOf(this), v === null || v === undefined ? '' : str(v)); }
  get nodeValue() { const t = this.nodeType; return t === 3 || t === 8 ? this.textContent : null; }
  set nodeValue(v) { const t = this.nodeType; if (t === 3 || t === 8) this.textContent = v; }
  get ownerDocument() { return this === document ? null : document; }
  get isConnected() { return native('connected', hOf(this)); }
  getRootNode() { let n = this; for (let p = n.parentNode; p; p = p.parentNode) n = p; return n; }
  isSameNode(o) { return o === this; }
  isEqualNode(o) { return !!o && o.nodeType === this.nodeType && (o.outerHTML || o.textContent) === (this.outerHTML || this.textContent); }
  compareDocumentPosition(o) {
    if (o === this) return 0;
    if (this.contains(o)) return 20;   // CONTAINED_BY | FOLLOWING
    if (o.contains(this)) return 10;   // CONTAINS | PRECEDING
    const all = document.getElementsByTagName('*');
    const a = all.indexOf(this), b = all.indexOf(o);
    if (a < 0 || b < 0) return 1;
    return a < b ? 4 : 2;
  }
  normalize() {}
}
Object.assign(Node, {
  ELEMENT_NODE: 1, ATTRIBUTE_NODE: 2, TEXT_NODE: 3, CDATA_SECTION_NODE: 4, PROCESSING_INSTRUCTION_NODE: 7,
  COMMENT_NODE: 8, DOCUMENT_NODE: 9, DOCUMENT_TYPE_NODE: 10, DOCUMENT_FRAGMENT_NODE: 11,
  DOCUMENT_POSITION_DISCONNECTED: 1, DOCUMENT_POSITION_PRECEDING: 2, DOCUMENT_POSITION_FOLLOWING: 4,
  DOCUMENT_POSITION_CONTAINS: 8, DOCUMENT_POSITION_CONTAINED_BY: 16,
});
Object.assign(Node.prototype, {
  ELEMENT_NODE: 1, TEXT_NODE: 3, COMMENT_NODE: 8, DOCUMENT_NODE: 9, DOCUMENT_FRAGMENT_NODE: 11,
});

class CharacterData extends Node {
  get data() { return this.textContent; }
  set data(v) { this.textContent = v; }
  get length() { return this.textContent.length; }
  appendData(s) { this.data = this.data + str(s); }
  substringData(o, n) { return this.data.substr(o, n); }
  get wholeText() { return this.data; }
  splitText(off) {
    const d = this.data, rest = document.createTextNode(d.slice(off));
    this.data = d.slice(0, off);
    if (this.parentNode) this.parentNode.insertBefore(rest, this.nextSibling);
    return rest;
  }
}
class Text extends CharacterData {}
class Comment extends CharacterData {}

// ParentNode / ChildNode mixins
const ParentNode = {
  get children() { return list(native('children', hOf(this), true)); },
  get childElementCount() { return J(native('children', hOf(this), true)).length; },
  get firstElementChild() { const c = J(native('children', hOf(this), true)); return c.length ? wrap(c[0]) : null; },
  get lastElementChild() { const c = J(native('children', hOf(this), true)); return c.length ? wrap(c[c.length - 1]) : null; },
  querySelector(sel) { const r = J(native('query', hOf(this), str(sel), false)); return r.length ? wrap(r[0]) : null; },
  querySelectorAll(sel) { return list(native('query', hOf(this), str(sel), true)); },
  getElementsByTagName(tag) { return list(native('by_tag', hOf(this), str(tag))); },
  getElementsByClassName(names) { return list(native('by_class', hOf(this), str(names))); },
  append(...nodes) { for (const n of nodes) this.appendChild(toNode(n)); },
  prepend(...nodes) { const first = this.firstChild; for (const n of nodes) this.insertBefore(toNode(n), first); },
  replaceChildren(...nodes) { this.textContent = ''; this.append(...nodes); },
};
const ChildNode = {
  remove() { const p = this.parentNode; if (p) p.removeChild(this); },
  before(...nodes) { const p = this.parentNode; if (p) for (const n of nodes) p.insertBefore(toNode(n), this); },
  after(...nodes) { const p = this.parentNode; if (!p) return; let ref = this.nextSibling; for (const n of nodes) p.insertBefore(toNode(n), ref); },
  replaceWith(...nodes) { const p = this.parentNode; if (!p) return; const ref = this.nextSibling; p.removeChild(this); for (const n of nodes) p.insertBefore(toNode(n), ref); },
  get nextElementSibling() { return wrap(native('sibling', hOf(this), 1, true)); },
  get previousElementSibling() { return wrap(native('sibling', hOf(this), -1, true)); },
};
function mixin(cls, obj) {
  for (const k of Object.getOwnPropertyNames(obj))
    Object.defineProperty(cls.prototype, k, Object.getOwnPropertyDescriptor(obj, k));
}
mixin(CharacterData, ChildNode);

// ------------------------------------------------------------------ helpers
function kebab(name) {
  if (name.startsWith('--')) return name;
  if (name === 'cssFloat') return 'float';
  let s = name.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());
  if (/^(webkit|moz|ms)-/.test(s)) s = '-' + s;
  return s;
}
function camel(name) { return name.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }

function parseDecls(text) {
  const out = Object.create(null);
  let depth = 0, quote = null, cur = '';
  const flush = () => {
    const i = cur.indexOf(':');
    if (i > 0) { const k = cur.slice(0, i).trim().toLowerCase(); const v = cur.slice(i + 1).trim(); if (k) out[k] = v; }
    cur = '';
  };
  for (const ch of text) {
    if (quote) { if (ch === quote) quote = null; cur += ch; continue; }
    if (ch === '"' || ch === "'") quote = ch;
    else if (ch === '(') depth++;
    else if (ch === ')') depth = Math.max(0, depth - 1);
    else if (ch === ';' && depth === 0) { flush(); continue; }
    cur += ch;
  }
  flush();
  return out;
}

const STYLES = new WeakMap();
function styleOf(el) {
  let s = STYLES.get(el);
  if (s) return s;
  const read = () => parseDecls(el.getAttribute('style') || '');
  const write = (d) => {
    const text = Object.keys(d).map((k) => k + ': ' + d[k]).join('; ');
    if (text) el.setAttribute('style', text); else el.removeAttribute('style');
  };
  const decl = {
    getPropertyValue(p) { const v = read()[str(p).toLowerCase()]; return v === undefined ? '' : v.replace(/\s*!important$/i, ''); },
    getPropertyPriority(p) { return /!important$/i.test(read()[str(p).toLowerCase()] || '') ? 'important' : ''; },
    setProperty(p, v, prio) {
      const d = read(); p = str(p).toLowerCase();
      if (v === null || v === undefined || str(v) === '') delete d[p];
      else d[p] = str(v) + (prio === 'important' ? ' !important' : '');
      write(d);
    },
    removeProperty(p) { const d = read(); p = str(p).toLowerCase(); const old = d[p] || ''; delete d[p]; write(d); return old; },
    item(i) { return Object.keys(read())[i] || ''; },
    get length() { return Object.keys(read()).length; },
    get cssText() { return el.getAttribute('style') || ''; },
    set cssText(v) { el.setAttribute('style', str(v)); },
    get parentRule() { return null; },
  };
  s = new Proxy(decl, {
    get(t, k) {
      if (typeof k !== 'string' || k in t) return Reflect.get(t, k);
      if (/^\d+$/.test(k)) return t.item(+k);
      return t.getPropertyValue(kebab(k));
    },
    set(t, k, v) {
      if (k === 'cssText') { t.cssText = v; return true; }
      if (typeof k === 'string' && !(k in t)) t.setProperty(kebab(k), v);
      return true;
    },
    has(t, k) { return typeof k === 'string'; },
  });
  STYLES.set(el, s);
  return s;
}

function computedStyle(el) {
  const d = J(native('computed', hOf(el))) || {};
  const decl = {
    getPropertyValue(p) { const v = d[str(p).toLowerCase()]; return v === undefined ? '' : v; },
    getPropertyPriority() { return ''; },
    item(i) { return Object.keys(d)[i] || ''; },
    get length() { return Object.keys(d).length; },
    get cssText() { return ''; },
    setProperty() { throw new DOMException('computed style is read-only', 'NoModificationAllowedError'); },
    removeProperty() { throw new DOMException('computed style is read-only', 'NoModificationAllowedError'); },
  };
  return new Proxy(decl, {
    get(t, k) {
      if (typeof k !== 'string' || k in t) return Reflect.get(t, k);
      return t.getPropertyValue(kebab(k));
    },
    set() { return true; },
  });
}

const TOKENS = new WeakMap();
class DOMTokenList {
  constructor() { illegal(); }
  get _list() { const v = TOKENS.get(this); return (v.el.getAttribute(v.attr) || '').split(/\s+/).filter(Boolean); }
  _set(arr) { const v = TOKENS.get(this); v.el.setAttribute(v.attr, Array.from(new Set(arr)).join(' ')); }
  get length() { return this._list.length; }
  get value() { const v = TOKENS.get(this); return v.el.getAttribute(v.attr) || ''; }
  set value(s) { const v = TOKENS.get(this); v.el.setAttribute(v.attr, str(s)); }
  item(i) { const l = this._list; return i < l.length ? l[i] : null; }
  contains(t) { return this._list.includes(str(t)); }
  add(...ts) {
    const l = this._list;
    for (const t of ts) { if (/\s/.test(t) || t === '') throw new DOMException('bad token', 'InvalidCharacterError'); if (!l.includes(str(t))) l.push(str(t)); }
    this._set(l);
  }
  remove(...ts) { const r = ts.map(str); const l = this._list; if (l.some((x) => r.includes(x))) this._set(l.filter((x) => !r.includes(x))); }
  toggle(t, force) {
    t = str(t);
    const has = this.contains(t);
    const want = force === undefined ? !has : !!force;
    if (want && !has) this.add(t);
    if (!want && has) this.remove(t);
    return want;
  }
  replace(a, b) { const l = this._list; const i = l.indexOf(str(a)); if (i < 0) return false; l[i] = str(b); this._set(l); return true; }
  supports() { return true; }
  forEach(fn, thisArg) { this._list.forEach((t, i) => fn.call(thisArg, t, i, this)); }
  entries() { return this._list.entries(); }
  keys() { return this._list.keys(); }
  values() { return this._list.values(); }
  [Symbol.iterator]() { return this._list[Symbol.iterator](); }
  toString() { return this.value; }
}
function tokenList(el, attr) {
  const t = Object.create(DOMTokenList.prototype);
  TOKENS.set(t, { el, attr });
  return t;
}
const CLASSLISTS = new WeakMap();

function datasetOf(el) {
  const name = (k) => 'data-' + kebab(str(k));
  return new Proxy({}, {
    get(t, k) { if (typeof k !== 'string') return undefined; const v = el.getAttribute(name(k)); return v === null ? undefined : v; },
    set(t, k, v) { if (typeof k === 'string') el.setAttribute(name(k), str(v)); return true; },
    has(t, k) { return typeof k === 'string' && el.hasAttribute(name(k)); },
    deleteProperty(t, k) { if (typeof k === 'string') el.removeAttribute(name(k)); return true; },
    ownKeys() { return el.getAttributeNames().filter((n) => n.startsWith('data-')).map((n) => camel(n.slice(5))); },
    getOwnPropertyDescriptor(t, k) {
      const v = typeof k === 'string' ? el.getAttribute(name(k)) : null;
      return v === null ? undefined : { value: v, writable: true, enumerable: true, configurable: true };
    },
  });
}

class DOMRect {
  constructor(x, y, w, h) { this.x = +x || 0; this.y = +y || 0; this.width = +w || 0; this.height = +h || 0; }
  get left() { return Math.min(this.x, this.x + this.width); }
  get top() { return Math.min(this.y, this.y + this.height); }
  get right() { return Math.max(this.x, this.x + this.width); }
  get bottom() { return Math.max(this.y, this.y + this.height); }
  toJSON() { return { x: this.x, y: this.y, width: this.width, height: this.height, left: this.left, top: this.top, right: this.right, bottom: this.bottom }; }
}
function rectOf(el) { return J(native('rect', hOf(el))); }   // [x, viewportY, width, height, documentY] or null

// ----------------------------------------------------------------- elements
class Element extends Node {
  get tagName() { hOf(this); return INFO.get(this)[1].toUpperCase(); }
  get localName() { hOf(this); return INFO.get(this)[1]; }
  get namespaceURI() { return 'http://www.w3.org/1999/xhtml'; }
  get prefix() { return null; }
  get id() { return this.getAttribute('id') || ''; }
  set id(v) { this.setAttribute('id', v); }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }
  get classList() { let l = CLASSLISTS.get(this); if (!l) { l = tokenList(this, 'class'); CLASSLISTS.set(this, l); } return l; }
  set classList(v) { this.setAttribute('class', v); }
  getAttribute(n) { return native('get_attr', hOf(this), str(n)); }
  getAttributeNS(ns, n) { return this.getAttribute(n); }
  setAttribute(n, v) { native('set_attr', hOf(this), str(n), str(v)); }
  setAttributeNS(ns, n, v) { this.setAttribute(str(n).replace(/^.*:/, ''), v); }
  removeAttribute(n) { native('remove_attr', hOf(this), str(n)); }
  removeAttributeNS(ns, n) { this.removeAttribute(n); }
  hasAttribute(n) { return native('has_attr', hOf(this), str(n)); }
  hasAttributeNS(ns, n) { return this.hasAttribute(n); }
  hasAttributes() { return this.getAttributeNames().length > 0; }
  toggleAttribute(n, force) {
    const has = this.hasAttribute(n);
    const want = force === undefined ? !has : !!force;
    if (want && !has) this.setAttribute(n, '');
    if (!want && has) this.removeAttribute(n);
    return want;
  }
  getAttributeNames() { return J(native('attr_names', hOf(this))); }
  get attributes() {
    const el = this;
    const arr = this.getAttributeNames().map((n) => ({ name: n, localName: n, nodeName: n, value: el.getAttribute(n), nodeValue: el.getAttribute(n), specified: true, ownerElement: el }));
    Object.defineProperty(arr, 'item', { value: (i) => arr[i] || null });
    Object.defineProperty(arr, 'getNamedItem', { value: (n) => arr.find((a) => a.name === str(n).toLowerCase()) || null });
    for (const a of arr) if (!(a.name in arr)) Object.defineProperty(arr, a.name, { value: a });
    return arr;
  }
  get innerHTML() { return native('html_get', hOf(this), false); }
  set innerHTML(v) { native('html_set', hOf(this), v === null || v === undefined ? '' : str(v)); }
  get outerHTML() { return native('html_get', hOf(this), true); }
  set outerHTML(v) { native('outer_set', hOf(this), str(v)); }
  insertAdjacentHTML(where, html) { native('adjacent_html', hOf(this), str(where).toLowerCase(), str(html)); }
  insertAdjacentElement(where, el) {
    where = str(where).toLowerCase();
    if (where === 'beforebegin') { if (!this.parentNode) return null; this.parentNode.insertBefore(el, this); }
    else if (where === 'afterbegin') this.insertBefore(el, this.firstChild);
    else if (where === 'beforeend') this.appendChild(el);
    else if (where === 'afterend') { if (!this.parentNode) return null; this.parentNode.insertBefore(el, this.nextSibling); }
    else throw new DOMException('bad position', 'SyntaxError');
    return el;
  }
  insertAdjacentText(where, text) { this.insertAdjacentElement(where, document.createTextNode(str(text))); }
  matches(sel) { return native('matches', hOf(this), str(sel)); }
  webkitMatchesSelector(sel) { return this.matches(sel); }
  msMatchesSelector(sel) { return this.matches(sel); }
  closest(sel) { return wrap(native('closest', hOf(this), str(sel))); }
  getBoundingClientRect() { const r = rectOf(this); return r ? new DOMRect(r[0], r[1], r[2], r[3]) : new DOMRect(0, 0, 0, 0); }
  getClientRects() { const r = rectOf(this); return nodeList(r ? [new DOMRect(r[0], r[1], r[2], r[3])] : []); }
  get clientWidth() { if (this === document.documentElement) return viewport().width; const r = rectOf(this); return r ? Math.round(r[2]) : 0; }
  get clientHeight() { if (this === document.documentElement) return viewport().height; const r = rectOf(this); return r ? Math.round(r[3]) : 0; }
  get clientTop() { return 0; }
  get clientLeft() { return 0; }
  get scrollWidth() { return this.clientWidth; }
  get scrollHeight() { return this.clientHeight; }
  get scrollTop() { return 0; }
  set scrollTop(v) {}
  get scrollLeft() { return 0; }
  set scrollLeft(v) {}
  scrollIntoView() {}
  scrollTo() {}
  scrollBy() {}
  scroll() {}
  get shadowRoot() { return null; }
  attachShadow() { throw new DOMException('Shadow DOM is not supported', 'NotSupportedError'); }
  animate() { return { finished: Promise.resolve(), cancel() {}, play() {}, pause() {}, onfinish: null }; }
  getAnimations() { return []; }
  requestFullscreen() { return Promise.reject(new DOMException('not supported', 'NotSupportedError')); }
  setPointerCapture() {}
  releasePointerCapture() {}
  hasPointerCapture() { return false; }
}
mixin(Element, ParentNode);
mixin(Element, ChildNode);

class HTMLElement extends Element {
  get style() { return styleOf(this); }
  set style(v) { this.setAttribute('style', str(v)); }
  get dataset() { return datasetOf(this); }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(v) { this.toggleAttribute('hidden', !!v); }
  get title() { return this.getAttribute('title') || ''; }
  set title(v) { this.setAttribute('title', v); }
  get lang() { return this.getAttribute('lang') || ''; }
  set lang(v) { this.setAttribute('lang', v); }
  get dir() { return this.getAttribute('dir') || ''; }
  set dir(v) { this.setAttribute('dir', v); }
  get tabIndex() { const v = parseInt(this.getAttribute('tabindex'), 10); return isNaN(v) ? -1 : v; }
  set tabIndex(v) { this.setAttribute('tabindex', v); }
  get accessKey() { return this.getAttribute('accesskey') || ''; }
  get draggable() { return this.getAttribute('draggable') === 'true'; }
  get contentEditable() { return this.getAttribute('contenteditable') || 'inherit'; }
  get isContentEditable() { return false; }
  get innerText() { return this.textContent; }
  set innerText(v) { this.textContent = v; }
  get outerText() { return this.textContent; }
  get offsetWidth() { const r = rectOf(this); return r ? Math.round(r[2]) : 0; }
  get offsetHeight() { const r = rectOf(this); return r ? Math.round(r[3]) : 0; }
  get offsetTop() { const r = rectOf(this); return r ? Math.round(r[4]) : 0; }
  get offsetLeft() { const r = rectOf(this); return r ? Math.round(r[0]) : 0; }
  get offsetParent() { return rectOf(this) ? document.body : null; }
  focus() { native('focus', hOf(this)); }
  blur() {}
  click() {
    const t = this.localName === 'input' ? (this.getAttribute('type') || '').toLowerCase() : '';
    if (t === 'checkbox') this.checked = !this.checked;
    else if (t === 'radio') this.checked = true;
    const ok = this.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: G }));
    if (ok && (t === 'checkbox' || t === 'radio')) {
      this.dispatchEvent(new Event('input', { bubbles: true }));
      this.dispatchEvent(new Event('change', { bubbles: true }));
    }
    if (ok && (t === 'submit' || (this.localName === 'button' && (this.getAttribute('type') || 'submit').toLowerCase() === 'submit'))) {
      const f = this.closest('form');
      if (f) f.requestSubmit(this);
    }
  }
}
defineOnProps(HTMLElement.prototype, EVENT_NAMES);

function reflect(cls, attrs) {
  for (const a of attrs) {
    const prop = camel(a);
    Object.defineProperty(cls.prototype, prop, {
      configurable: true,
      get() { return this.getAttribute(a) || ''; },
      set(v) { this.setAttribute(a, v); },
    });
  }
}
function reflectBool(cls, attrs) {
  for (const a of attrs) {
    Object.defineProperty(cls.prototype, camel(a), {
      configurable: true,
      get() { return this.hasAttribute(a); },
      set(v) { this.toggleAttribute(a, !!v); },
    });
  }
}
function reflectURL(cls, attrs) {
  for (const a of attrs) {
    Object.defineProperty(cls.prototype, a, {
      configurable: true,
      get() { const v = this.getAttribute(a); return v === null ? '' : (native('resolve', v, null) || v); },
      set(v) { this.setAttribute(a, v); },
    });
  }
}

const formValue = {
  get value() { return native('form_get', hOf(this), 'value'); },
  set value(v) { native('form_set', hOf(this), 'value', v === null || v === undefined ? '' : str(v)); },
  get form() { return this.closest('form'); },
  get name() { return this.getAttribute('name') || ''; },
  set name(v) { this.setAttribute('name', v); },
  get labels() { return this.id ? document.querySelectorAll('label[for="' + this.id + '"]') : nodeList([]); },
  get validity() { return { valid: true, valueMissing: false, typeMismatch: false }; },
  get validationMessage() { return ''; },
  get willValidate() { return true; },
  checkValidity() { return true; },
  reportValidity() { return true; },
  setCustomValidity() {},
  select() {},
  setSelectionRange() {},
};

class HTMLInputElement extends HTMLElement {
  get type() { return (this.getAttribute('type') || 'text').toLowerCase(); }
  set type(v) { this.setAttribute('type', v); }
  get checked() { return native('form_get', hOf(this), 'checked'); }
  set checked(v) { native('form_set', hOf(this), 'checked', !!v); }
  get defaultValue() { return this.getAttribute('value') || ''; }
  set defaultValue(v) { this.setAttribute('value', v); }
  get defaultChecked() { return this.hasAttribute('checked'); }
  get valueAsNumber() { return parseFloat(this.value); }
  set valueAsNumber(v) { this.value = str(v); }
  get files() { return nodeList([]); }
}
mixin(HTMLInputElement, formValue);
reflect(HTMLInputElement, ['placeholder', 'autocomplete', 'min', 'max', 'step', 'pattern', 'accept', 'alt', 'inputmode']);
reflectBool(HTMLInputElement, ['disabled', 'required', 'readonly', 'multiple', 'autofocus']);
Object.defineProperty(HTMLInputElement.prototype, 'readOnly', Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'readonly'));

class HTMLTextAreaElement extends HTMLElement {
  get type() { return 'textarea'; }
  get defaultValue() { return this.textContent; }
  set defaultValue(v) { this.textContent = v; }
}
mixin(HTMLTextAreaElement, formValue);
reflect(HTMLTextAreaElement, ['placeholder', 'rows', 'cols', 'wrap']);
reflectBool(HTMLTextAreaElement, ['disabled', 'required', 'readonly']);

class HTMLSelectElement extends HTMLElement {
  get type() { return this.hasAttribute('multiple') ? 'select-multiple' : 'select-one'; }
  get options() { return this.getElementsByTagName('option'); }
  get length() { return this.options.length; }
  get selectedIndex() { return native('form_get', hOf(this), 'selectedIndex'); }
  set selectedIndex(i) { native('form_set', hOf(this), 'selectedIndex', +i); }
  get selectedOptions() { const o = this.options[this.selectedIndex]; return nodeList(o ? [o] : []); }
  item(i) { return this.options[i] || null; }
  add(opt, before) { this.insertBefore(opt, typeof before === 'number' ? this.options[before] || null : before || null); }
}
mixin(HTMLSelectElement, formValue);
reflectBool(HTMLSelectElement, ['disabled', 'required', 'multiple']);

class HTMLOptionElement extends HTMLElement {
  get value() { return native('form_get', hOf(this), 'value'); }
  set value(v) { this.setAttribute('value', v); }
  get text() { return this.textContent.trim(); }
  set text(v) { this.textContent = v; }
  get label() { return this.getAttribute('label') || this.text; }
  get selected() { const s = this.closest('select'); return s ? s.options[s.selectedIndex] === this : this.hasAttribute('selected'); }
  set selected(v) { const s = this.closest('select'); if (s && v) s.selectedIndex = s.options.indexOf(this); }
  get index() { const s = this.closest('select'); return s ? s.options.indexOf(this) : 0; }
  get defaultSelected() { return this.hasAttribute('selected'); }
}
reflectBool(HTMLOptionElement, ['disabled']);

class HTMLButtonElement extends HTMLElement {
  get type() { return (this.getAttribute('type') || 'submit').toLowerCase(); }
  set type(v) { this.setAttribute('type', v); }
}
mixin(HTMLButtonElement, formValue);
Object.defineProperty(HTMLButtonElement.prototype, 'value', {
  get() { return this.getAttribute('value') || ''; }, set(v) { this.setAttribute('value', v); }, configurable: true,
});
reflectBool(HTMLButtonElement, ['disabled']);

class HTMLFormElement extends HTMLElement {
  get elements() { return this.querySelectorAll('input, select, textarea, button, fieldset, output'); }
  get length() { return this.elements.length; }
  submit() { native('submit', hOf(this)); }
  requestSubmit(submitter) {
    if (this.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true, submitter: submitter || null }))) this.submit();
  }
  reset() {
    if (!this.dispatchEvent(new Event('reset', { bubbles: true, cancelable: true }))) return;
    for (const el of this.elements) {
      if (el instanceof HTMLInputElement) {
        if (el.type === 'checkbox' || el.type === 'radio') el.checked = el.defaultChecked;
        else el.value = el.defaultValue;
      } else if (el instanceof HTMLTextAreaElement) el.value = el.defaultValue;
    }
  }
  checkValidity() { return true; }
  reportValidity() { return true; }
}
reflect(HTMLFormElement, ['method', 'enctype', 'target', 'name']);
Object.defineProperty(HTMLFormElement.prototype, 'action', {
  get() { const v = this.getAttribute('action'); return native('resolve', v || '', null) || location.href; },
  set(v) { this.setAttribute('action', v); },
});

function urlParts(href) {
  try { return new URL(href); } catch (e) { return null; }
}
class HTMLAnchorElement extends HTMLElement {
  get text() { return this.textContent; }
  toString() { return this.href; }
}
reflectURL(HTMLAnchorElement, ['href']);
reflect(HTMLAnchorElement, ['target', 'rel', 'download', 'hreflang', 'type']);
for (const part of ['protocol', 'host', 'hostname', 'port', 'pathname', 'search', 'hash', 'origin']) {
  Object.defineProperty(HTMLAnchorElement.prototype, part, {
    configurable: true,
    get() { const u = urlParts(this.href); return u ? u[part] : ''; },
  });
}
Object.defineProperty(HTMLAnchorElement.prototype, 'relList', { get() { return tokenList(this, 'rel'); } });

class HTMLImageElement extends HTMLElement {
  get complete() { return true; }
  get naturalWidth() { return this.offsetWidth; }
  get naturalHeight() { return this.offsetHeight; }
  get width() { return parseInt(this.getAttribute('width'), 10) || this.offsetWidth; }
  set width(v) { this.setAttribute('width', v); }
  get height() { return parseInt(this.getAttribute('height'), 10) || this.offsetHeight; }
  set height(v) { this.setAttribute('height', v); }
  decode() { return Promise.resolve(); }
  get currentSrc() { return this.src; }
}
reflectURL(HTMLImageElement, ['src']);
reflect(HTMLImageElement, ['alt', 'srcset', 'sizes', 'loading', 'decoding', 'crossorigin']);

class HTMLScriptElement extends HTMLElement {
  get text() { return this.textContent; }
  set text(v) { this.textContent = v; }
}
reflectURL(HTMLScriptElement, ['src']);
reflect(HTMLScriptElement, ['type', 'charset', 'crossorigin', 'integrity', 'nonce']);
reflectBool(HTMLScriptElement, ['async', 'defer', 'nomodule']);

class HTMLLinkElement extends HTMLElement {
  get sheet() { return null; }
  get relList() { return tokenList(this, 'rel'); }
}
reflectURL(HTMLLinkElement, ['href']);
reflect(HTMLLinkElement, ['rel', 'media', 'type', 'as', 'crossorigin']);
class HTMLStyleElement extends HTMLElement { get sheet() { return null; } }
reflect(HTMLStyleElement, ['media', 'type']);
class HTMLIFrameElement extends HTMLElement {
  get contentWindow() { return null; }
  get contentDocument() { return null; }
}
reflectURL(HTMLIFrameElement, ['src']);
class HTMLCanvasElement extends HTMLElement {
  getContext() { return null; }
  toDataURL() { return 'data:,'; }
  get width() { return parseInt(this.getAttribute('width'), 10) || 300; }
  set width(v) { this.setAttribute('width', v); }
  get height() { return parseInt(this.getAttribute('height'), 10) || 150; }
  set height(v) { this.setAttribute('height', v); }
}
class HTMLTemplateElement extends HTMLElement {
  get content() {
    const f = document.createDocumentFragment();
    for (const c of this.childNodes) f.appendChild(c.cloneNode(true));
    return f;
  }
}
class HTMLDetailsElement extends HTMLElement {}
reflectBool(HTMLDetailsElement, ['open']);
class HTMLDialogElement extends HTMLElement {
  show() { this.setAttribute('open', ''); }
  showModal() { this.setAttribute('open', ''); }
  close(rv) { this.removeAttribute('open'); this.returnValue = rv === undefined ? '' : str(rv); this.dispatchEvent(new Event('close')); }
}
reflectBool(HTMLDialogElement, ['open']);
class HTMLLabelElement extends HTMLElement {
  get htmlFor() { return this.getAttribute('for') || ''; }
  set htmlFor(v) { this.setAttribute('for', v); }
  get control() { const f = this.htmlFor; return f ? document.getElementById(f) : this.querySelector('input, select, textarea, button'); }
}
class HTMLMediaElement extends HTMLElement {
  play() { return Promise.reject(new DOMException('media is not supported', 'NotSupportedError')); }
  pause() {}
  load() {}
  canPlayType() { return ''; }
  get paused() { return true; }
}
class HTMLBodyElement extends HTMLElement {}
defineOnProps(HTMLBodyElement.prototype, ['hashchange', 'popstate', 'beforeunload', 'unload', 'message', 'storage', 'online', 'offline']);
class HTMLHtmlElement extends HTMLElement {}
class HTMLHeadElement extends HTMLElement {}
class HTMLDivElement extends HTMLElement {}
class HTMLSpanElement extends HTMLElement {}
class HTMLParagraphElement extends HTMLElement {}
class HTMLHeadingElement extends HTMLElement {}
class HTMLUListElement extends HTMLElement {}
class HTMLOListElement extends HTMLElement {}
class HTMLLIElement extends HTMLElement {}
class HTMLTableElement extends HTMLElement {
  get rows() { return this.querySelectorAll('tr'); }
  get tBodies() { return this.getElementsByTagName('tbody'); }
}
class HTMLTableRowElement extends HTMLElement { get cells() { return this.querySelectorAll('td, th'); } }
class HTMLTableCellElement extends HTMLElement {}
class HTMLPreElement extends HTMLElement {}
class HTMLBRElement extends HTMLElement {}
class HTMLHRElement extends HTMLElement {}
class HTMLMetaElement extends HTMLElement {}
reflect(HTMLMetaElement, ['name', 'content', 'charset']);
Object.defineProperty(HTMLMetaElement.prototype, 'httpEquiv', { get() { return this.getAttribute('http-equiv') || ''; } });
class HTMLTitleElement extends HTMLElement {
  get text() { return this.textContent; }
  set text(v) { this.textContent = v; }
}
class HTMLUnknownElement extends HTMLElement {}
class SVGElement extends Element {
  get style() { return styleOf(this); }
  get dataset() { return datasetOf(this); }
  get ownerSVGElement() { return this.closest('svg'); }
  focus() {}
  blur() {}
}
defineOnProps(SVGElement.prototype, EVENT_NAMES);

Object.assign(TAGCLASS, {
  a: HTMLAnchorElement, input: HTMLInputElement, textarea: HTMLTextAreaElement, select: HTMLSelectElement,
  option: HTMLOptionElement, button: HTMLButtonElement, form: HTMLFormElement, img: HTMLImageElement,
  script: HTMLScriptElement, link: HTMLLinkElement, style: HTMLStyleElement, iframe: HTMLIFrameElement,
  canvas: HTMLCanvasElement, template: HTMLTemplateElement, details: HTMLDetailsElement,
  dialog: HTMLDialogElement, label: HTMLLabelElement, video: HTMLMediaElement, audio: HTMLMediaElement,
  body: HTMLBodyElement, html: HTMLHtmlElement, head: HTMLHeadElement, div: HTMLDivElement,
  span: HTMLSpanElement, p: HTMLParagraphElement, h1: HTMLHeadingElement, h2: HTMLHeadingElement,
  h3: HTMLHeadingElement, h4: HTMLHeadingElement, h5: HTMLHeadingElement, h6: HTMLHeadingElement,
  ul: HTMLUListElement, ol: HTMLOListElement, li: HTMLLIElement, table: HTMLTableElement,
  tr: HTMLTableRowElement, td: HTMLTableCellElement, th: HTMLTableCellElement, pre: HTMLPreElement,
  br: HTMLBRElement, hr: HTMLHRElement, meta: HTMLMetaElement, title: HTMLTitleElement,
  svg: SVGElement, path: SVGElement, g: SVGElement, circle: SVGElement, rect: SVGElement, use: SVGElement,
  line: SVGElement, polygon: SVGElement, polyline: SVGElement, ellipse: SVGElement, text: SVGElement,
});

class DocumentFragment extends Node {
  constructor() { super(); }
  getElementById(id) { return this.querySelector('#' + CSS.escape(str(id))); }
}
mixin(DocumentFragment, ParentNode);
// `new DocumentFragment()` is legal; Node's constructor is not, so build it by hand.
const DF = function DocumentFragment() { return document.createDocumentFragment(); };
DF.prototype = DocumentFragment.prototype;

// ----------------------------------------------------------------- document
let readyState = 'loading';
let currentScript = null;

class Document extends Node {
  get documentElement() { return wrap(native('root')); }
  get head() { return wrap(native('find_tag', 'head')); }
  get body() { return wrap(native('find_tag', 'body')); }
  set body(v) {}
  get title() { const t = this.querySelector('title'); return t ? t.textContent.replace(/\s+/g, ' ').trim() : ''; }
  set title(v) {
    let t = this.querySelector('title');
    if (!t) { t = this.createElement('title'); if (this.head) this.head.appendChild(t); }
    t.textContent = str(v);
  }
  getElementById(id) { return wrap(native('by_id', str(id))); }
  getElementsByName(name) { return this.querySelectorAll('[name="' + str(name).replace(/["\\]/g, '\\$&') + '"]'); }
  createElement(tag) { return wrap(native('create_element', str(tag))); }
  createElementNS(ns, tag) { return this.createElement(str(tag).replace(/^.*:/, '')); }
  createTextNode(s) { return wrap(native('create_text', str(s))); }
  createComment(s) { return wrap(native('create_comment', str(s))); }
  createDocumentFragment() { return wrap(native('create_fragment')); }
  createEvent(kind) {
    kind = str(kind).toLowerCase();
    const cls = kind.startsWith('mouse') ? MouseEvent : kind.startsWith('keyboard') ? KeyboardEvent : kind.startsWith('custom') ? CustomEvent : Event;
    const e = Object.create(cls.prototype);
    Event.call(e, '');
    return e;
  }
  createRange() {
    return { setStart() {}, setEnd() {}, selectNodeContents() {}, collapse() {}, getBoundingClientRect: () => new DOMRect(),
      getClientRects: () => [], createContextualFragment: (html) => { const f = document.createDocumentFragment(); const d = document.createElement('div'); d.innerHTML = html; while (d.firstChild) f.appendChild(d.firstChild); return f; } };
  }
  createTreeWalker(root) {
    const all = [root, ...root.querySelectorAll('*')];
    let i = 0;
    return { currentNode: root, nextNode() { i++; this.currentNode = all[i] || null; return this.currentNode; } };
  }
  get readyState() { return readyState; }
  get cookie() { return ''; }
  set cookie(v) {}
  get URL() { return location.href; }
  get documentURI() { return location.href; }
  get baseURI() { return location.href; }
  get referrer() { return ''; }
  get domain() { return location.hostname; }
  get defaultView() { return G; }
  get location() { return location; }
  set location(v) { location.href = v; }
  get activeElement() { return wrap(native('active')) || this.body; }
  get currentScript() { return currentScript; }
  get forms() { return this.getElementsByTagName('form'); }
  get images() { return this.getElementsByTagName('img'); }
  get links() { return this.querySelectorAll('a[href], area[href]'); }
  get scripts() { return this.getElementsByTagName('script'); }
  get styleSheets() { return nodeList([]); }
  get characterSet() { return 'UTF-8'; }
  get charset() { return 'UTF-8'; }
  get inputEncoding() { return 'UTF-8'; }
  get compatMode() { return 'CSS1Compat'; }
  get contentType() { return 'text/html'; }
  get doctype() { return null; }
  get hidden() { return false; }
  get visibilityState() { return 'visible'; }
  get fullscreenElement() { return null; }
  get scrollingElement() { return this.documentElement; }
  get implementation() { return { hasFeature: () => true, createHTMLDocument: detachedDocument }; }
  hasFocus() { return true; }
  write(...parts) { native('doc_write', parts.join('')); }
  writeln(...parts) { native('doc_write', parts.join('') + '\n'); }
  open() { return this; }
  close() {}
  importNode(n, deep) { return n.cloneNode(!!deep); }
  adoptNode(n) { if (n.parentNode) n.parentNode.removeChild(n); return n; }
  elementFromPoint() { return null; }
  elementsFromPoint() { return []; }
  getSelection() { return G.getSelection(); }
  execCommand() { return false; }
  queryCommandSupported() { return false; }
}
mixin(Document, ParentNode);

// document.implementation.createHTMLDocument(): a detached <html> tree that
// libraries such as jQuery use to parse HTML without touching the page.
function detachedDocument(title, parsedRoot) {
  const html = parsedRoot || document.createElement('html');
  const head = html.querySelector('head') || html.appendChild(document.createElement('head'));
  const body = html.querySelector('body') || html.appendChild(document.createElement('body'));
  if (title !== undefined) head.appendChild(document.createElement('title')).textContent = str(title);
  return {
    nodeType: 9, documentElement: html, head, body, defaultView: null, readyState: 'complete',
    get title() { const t = html.querySelector('title'); return t ? t.textContent : ''; },
    implementation: { hasFeature: () => true, createHTMLDocument: detachedDocument },
    createElement: (t) => document.createElement(t),
    createElementNS: (ns, t) => document.createElementNS(ns, t),
    createTextNode: (s) => document.createTextNode(s),
    createComment: (s) => document.createComment(s),
    createDocumentFragment: () => document.createDocumentFragment(),
    getElementById: (id) => html.querySelector('#' + CSS.escape(str(id))),
    querySelector: (s) => html.querySelector(s),
    querySelectorAll: (s) => html.querySelectorAll(s),
    getElementsByTagName: (t) => html.getElementsByTagName(t),
    getElementsByClassName: (c) => html.getElementsByClassName(c),
    importNode: (n, deep) => n.cloneNode(!!deep),
    adoptNode: (n) => n,
    addEventListener() {}, removeEventListener() {},
  };
}
class DOMParser {
  parseFromString(source, type) {
    if (str(type) !== 'text/html') throw new TypeError('Only text/html parsing is supported');
    return detachedDocument(undefined, wrap(native('parse_document', str(source))));
  }
}
defineOnProps(Document.prototype, EVENT_NAMES.concat(['readystatechange', 'visibilitychange', 'DOMContentLoaded']));
class HTMLDocument extends Document {}

const document = Object.create(HTMLDocument.prototype);
H.set(document, 0);
W.set(0, document);
INFO.set(document, [9, '#document']);

// ----------------------------------------------------------------- location
const URL_RE = /^([a-zA-Z][a-zA-Z0-9+.-]*:)(?:\/\/(?:[^@\/?#]*@)?(\[[^\]]*\]|[^:\/?#]*)(?::(\d*))?)?([^?#]*)(\?[^#]*)?(#.*)?$/;

class URLSearchParams {
  constructor(init) {
    this._p = [];
    if (init === undefined || init === null) return;
    if (typeof init === 'object') {
      const entries = (init instanceof URLSearchParams) ? init._p : (Symbol.iterator in init ? Array.from(init) : Object.entries(init));
      for (const [k, v] of entries) this._p.push([str(k), str(v)]);
      return;
    }
    let s = str(init);
    if (s.startsWith('?')) s = s.slice(1);
    for (const part of s.split('&')) {
      if (!part) continue;
      const i = part.indexOf('=');
      const dec = (x) => { try { return decodeURIComponent(x.replace(/\+/g, ' ')); } catch (e) { return x; } };
      this._p.push(i < 0 ? [dec(part), ''] : [dec(part.slice(0, i)), dec(part.slice(i + 1))]);
    }
  }
  get size() { return this._p.length; }
  append(k, v) { this._p.push([str(k), str(v)]); this._changed(); }
  delete(k) { this._p = this._p.filter((p) => p[0] !== str(k)); this._changed(); }
  get(k) { const p = this._p.find((p) => p[0] === str(k)); return p ? p[1] : null; }
  getAll(k) { return this._p.filter((p) => p[0] === str(k)).map((p) => p[1]); }
  has(k) { return this._p.some((p) => p[0] === str(k)); }
  set(k, v) {
    k = str(k);
    const i = this._p.findIndex((p) => p[0] === k);
    if (i < 0) this._p.push([k, str(v)]);
    else { this._p[i][1] = str(v); this._p = this._p.filter((p, j) => j <= i || p[0] !== k); }
    this._changed();
  }
  sort() { this._p.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)); this._changed(); }
  forEach(fn, t) { for (const [k, v] of this._p) fn.call(t, v, k, this); }
  keys() { return this._p.map((p) => p[0])[Symbol.iterator](); }
  values() { return this._p.map((p) => p[1])[Symbol.iterator](); }
  entries() { return this._p.map((p) => [p[0], p[1]])[Symbol.iterator](); }
  [Symbol.iterator]() { return this.entries(); }
  toString() {
    const enc = (x) => encodeURIComponent(x).replace(/%20/g, '+');
    return this._p.map(([k, v]) => enc(k) + '=' + enc(v)).join('&');
  }
  _changed() { if (this._url) this._url._search = this._p.length ? '?' + this.toString() : ''; }
}

class URL {
  constructor(href, base) {
    href = str(href);
    let abs = href;
    if (!URL_RE.test(href) || /^[a-zA-Z]:\\/.test(href)) {
      if (base === undefined) throw new TypeError("Invalid URL: " + href);
      abs = native('resolve', href, str(base));
      if (abs === null) throw new TypeError("Invalid URL: " + href);
    } else if (base !== undefined && !/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(href)) {
      abs = native('resolve', href, str(base));
    }
    const m = URL_RE.exec(abs);
    if (!m) throw new TypeError("Invalid URL: " + href);
    this._protocol = m[1].toLowerCase();
    this._hostname = (m[2] || '').toLowerCase();
    this._port = m[3] || '';
    if ((this._protocol === 'http:' && this._port === '80') || (this._protocol === 'https:' && this._port === '443')) this._port = '';
    this._special = m[2] !== undefined;
    this._pathname = m[4] || (this._special ? '/' : '');
    this._search = m[5] && m[5] !== '?' ? m[5] : '';
    this._hash = m[6] && m[6] !== '#' ? m[6] : '';
  }
  get protocol() { return this._protocol; }
  set protocol(v) { this._protocol = str(v).replace(/:?$/, ':'); }
  get hostname() { return this._hostname; }
  set hostname(v) { this._hostname = str(v); }
  get port() { return this._port; }
  set port(v) { this._port = str(v); }
  get host() { return this._hostname + (this._port ? ':' + this._port : ''); }
  set host(v) { const [h, p] = str(v).split(':'); this._hostname = h; this._port = p || ''; }
  get origin() { return this._special ? this._protocol + '//' + this.host : 'null'; }
  get pathname() { return this._pathname; }
  set pathname(v) { v = str(v); this._pathname = v.startsWith('/') ? v : '/' + v; }
  get search() { return this._search; }
  set search(v) { v = str(v); this._search = v && v !== '?' ? (v.startsWith('?') ? v : '?' + v) : ''; this._sp = null; }
  get hash() { return this._hash; }
  set hash(v) { v = str(v); this._hash = v && v !== '#' ? (v.startsWith('#') ? v : '#' + v) : ''; }
  get searchParams() {
    if (!this._sp) { this._sp = new URLSearchParams(this._search); this._sp._url = this; }
    return this._sp;
  }
  get username() { return ''; }
  get password() { return ''; }
  get href() { return this._protocol + (this._special ? '//' + this.host : '') + this._pathname + this._search + this._hash; }
  set href(v) { const u = new URL(v); Object.assign(this, u); this._sp = null; }
  toString() { return this.href; }
  toJSON() { return this.href; }
  static createObjectURL() { return 'blob:null'; }
  static revokeObjectURL() {}
  static canParse(u, b) { try { new URL(u, b); return true; } catch (e) { return false; } }
}

const location = {};
for (const part of ['protocol', 'host', 'hostname', 'port', 'pathname', 'search', 'hash', 'origin']) {
  Object.defineProperty(location, part, {
    enumerable: true,
    get() { return new URL(native('location'))[part]; },
    set(v) {
      if (part === 'origin') return;
      const u = new URL(native('location'));
      u[part] = v;
      native('navigate', u.href, false);
    },
  });
}
Object.defineProperties(location, {
  href: { enumerable: true, get() { return native('location'); }, set(v) { native('navigate', str(v), false); } },
  assign: { value(v) { native('navigate', str(v), false); } },
  replace: { value(v) { native('navigate', str(v), true); } },
  reload: { value() { native('navigate', native('location'), true); } },
  toString: { value() { return native('location'); } },
  ancestorOrigins: { value: nodeList([]) },
});
Object.freeze(location);

const historyState = { state: null };
const history = {
  get length() { return 1; },
  get state() { return historyState.state; },
  get scrollRestoration() { return 'auto'; },
  set scrollRestoration(v) {},
  back() { native('history', -1); },
  forward() { native('history', 1); },
  go(n) { if (+n) native('history', +n); },
  pushState(state) { historyState.state = state === undefined ? null : state; },
  replaceState(state) { historyState.state = state === undefined ? null : state; },
};

const navigator = Object.freeze({
  userAgent: 'Mozilla/5.0 (compatible; NetSurfer/1.0)',
  appName: 'Netscape', appVersion: '5.0', appCodeName: 'Mozilla', product: 'Gecko', vendor: '',
  platform: '', language: 'en-US', languages: Object.freeze(['en-US', 'en']), onLine: true,
  cookieEnabled: false, doNotTrack: '1', hardwareConcurrency: 1, maxTouchPoints: 0, webdriver: false,
  javaEnabled: () => false,
  sendBeacon: () => false,
  clipboard: Object.freeze({ writeText: () => Promise.reject(new DOMException('not allowed', 'NotAllowedError')), readText: () => Promise.reject(new DOMException('not allowed', 'NotAllowedError')) }),
  serviceWorker: undefined,
});

// ------------------------------------------------------------------ timers
const timers = new Map();
let nextTimer = 1;
function makeTimer(fn, ms, args, repeat) {
  if (typeof fn !== 'function') {
    const src = str(fn);
    fn = () => (0, eval)(src);
  }
  const id = nextTimer++;
  timers.set(id, { fn, args, repeat });
  try {
    native('timer_set', id, +ms || 0, repeat);
  } catch (e) {
    timers.delete(id);
    throw e;
  }
  return id;
}
function clearTimer(id) {
  id = +id;
  if (timers.has(id)) { timers.delete(id); native('timer_clear', id); }
}
function fireTimer(id) {
  const t = timers.get(id);
  if (!t) return;
  if (!t.repeat) timers.delete(id);
  try { t.fn.apply(G, t.args); } catch (e) { report(e); }
}

// ------------------------------------------------------------------ network
class Headers {
  constructor(init) {
    this._h = Object.create(null);
    if (init) {
      const entries = init instanceof Headers ? Object.entries(init._h) : (Symbol.iterator in Object(init) ? Array.from(init) : Object.entries(init));
      for (const [k, v] of entries) this.append(k, v);
    }
  }
  append(k, v) { k = str(k).toLowerCase(); this._h[k] = k in this._h ? this._h[k] + ', ' + str(v) : str(v); }
  set(k, v) { this._h[str(k).toLowerCase()] = str(v); }
  get(k) { const v = this._h[str(k).toLowerCase()]; return v === undefined ? null : v; }
  has(k) { return str(k).toLowerCase() in this._h; }
  delete(k) { delete this._h[str(k).toLowerCase()]; }
  forEach(fn, t) { for (const k of Object.keys(this._h)) fn.call(t, this._h[k], k, this); }
  keys() { return Object.keys(this._h)[Symbol.iterator](); }
  values() { return Object.values(this._h)[Symbol.iterator](); }
  entries() { return Object.entries(this._h)[Symbol.iterator](); }
  [Symbol.iterator]() { return this.entries(); }
}

class Response {
  constructor(body, init) {
    init = init || {};
    this._body = body === undefined || body === null ? '' : str(body);
    this.status = init.status === undefined ? 200 : +init.status;
    this.statusText = init.statusText === undefined ? '' : str(init.statusText);
    this.headers = new Headers(init.headers);
    this.url = init.url || '';
    this.redirected = false;
    this.type = 'basic';
    this.bodyUsed = false;
  }
  get ok() { return this.status >= 200 && this.status < 300; }
  _take() {
    if (this.bodyUsed) return Promise.reject(new TypeError('body already used'));
    this.bodyUsed = true;
    return Promise.resolve(this._body);
  }
  text() { return this._take(); }
  json() { return this._take().then(JSON.parse); }
  arrayBuffer() { return this._take().then((t) => new TextEncoder().encode(t).buffer); }
  blob() { return Promise.reject(new TypeError('Blob is not supported')); }
  clone() { const r = new Response(this._body, this); r.url = this.url; return r; }
}
class Request {
  constructor(input, init) {
    init = init || {};
    this.url = input instanceof Request ? input.url : str(input);
    this.method = str(init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    this.headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    this.body = init.body === undefined ? (input instanceof Request ? input.body : null) : init.body;
  }
}

const requests = new Map();
let nextRequest = 1;
function startRequest(url, method, body, headers, done) {
  const id = nextRequest++;
  const h = {};
  if (headers) for (const [k, v] of headers) h[k] = v;
  requests.set(id, done);
  try {
    native('fetch_start', id, str(url), method, body === null || body === undefined ? null : bodyText(body, h), JSON.stringify(h));
  } catch (e) {
    requests.delete(id);
    throw e;
  }
}
function bodyText(body, h) {
  if (body instanceof URLSearchParams) { if (!h['content-type']) h['content-type'] = 'application/x-www-form-urlencoded;charset=UTF-8'; return body.toString(); }
  if (body instanceof FormData) { if (!h['content-type']) h['content-type'] = 'application/x-www-form-urlencoded;charset=UTF-8'; return new URLSearchParams(body._p).toString(); }
  if (typeof body === 'string' && !h['content-type']) h['content-type'] = 'text/plain;charset=UTF-8';
  return str(body);
}
function fetchDone(id, json) {
  const done = requests.get(id);
  if (!done) return;
  requests.delete(id);
  done(JSON.parse(json));
}
function fetch(input, init) {
  return new Promise((resolve, reject) => {
    const req = new Request(input, init);
    const lower = new Headers(req.headers);
    try {
      startRequest(req.url, req.method, req.body, Object.entries(lower._h), (r) => {
        if (r.error) reject(new TypeError('Failed to fetch: ' + r.error));
        else resolve(Object.assign(new Response(r.body, { status: r.status, statusText: r.statusText, headers: r.headers }), { url: r.url }));
      });
    } catch (e) {
      reject(e instanceof TypeError ? e : new TypeError(String(e && e.message || e)));
    }
  });
}

class FormData {
  constructor(form) {
    this._p = [];
    if (form && form.elements) {
      for (const el of form.elements) {
        const name = el.getAttribute('name');
        if (!name || el.hasAttribute('disabled')) continue;
        const t = el instanceof HTMLInputElement ? el.type : '';
        if ((t === 'checkbox' || t === 'radio') && !el.checked) continue;
        if (t === 'submit' || t === 'button' || t === 'reset' || t === 'file' || el instanceof HTMLButtonElement) continue;
        this._p.push([name, el.value]);
      }
    }
  }
}
for (const k of ['append', 'delete', 'get', 'getAll', 'has', 'set', 'forEach', 'keys', 'values', 'entries'])
  FormData.prototype[k] = URLSearchParams.prototype[k];
FormData.prototype[Symbol.iterator] = URLSearchParams.prototype[Symbol.iterator];
FormData.prototype._changed = function () {};

class XMLHttpRequest extends EventTarget {
  constructor() {
    super();
    this.readyState = 0; this.status = 0; this.statusText = ''; this.responseText = ''; this.response = '';
    this.responseType = ''; this.responseURL = ''; this.timeout = 0; this.withCredentials = false;
    this.upload = new EventTarget();
    this._headers = new Headers(); this._resp = null; this._aborted = false;
  }
  open(method, url, async) {
    if (async === false) throw new DOMException('synchronous XMLHttpRequest is not supported', 'InvalidAccessError');
    this._method = str(method).toUpperCase(); this._url = str(url); this._aborted = false;
    this._state(1);
  }
  setRequestHeader(k, v) { this._headers.append(k, v); }
  getResponseHeader(k) { return this._resp ? (this._resp.headers[str(k).toLowerCase()] || null) : null; }
  getAllResponseHeaders() { return this._resp ? Object.entries(this._resp.headers).map(([k, v]) => k + ': ' + v).join('\r\n') : ''; }
  overrideMimeType() {}
  abort() { this._aborted = true; this.readyState = 0; this._fire('abort'); }
  send(body) {
    const self = this;
    try {
      startRequest(this._url, this._method, body === undefined ? null : body, Object.entries(this._headers._h), (r) => {
        if (self._aborted) return;
        if (r.error) { self._state(4); self._fire('error'); self._fire('loadend'); return; }
        self._resp = r;
        self.status = r.status; self.statusText = r.statusText; self.responseURL = r.url;
        self._state(2); self._state(3);
        self.responseText = r.body;
        if (self.responseType === 'json') { try { self.response = JSON.parse(r.body); } catch (e) { self.response = null; } }
        else self.response = r.body;
        self._state(4);
        self._fire('load'); self._fire('loadend');
      });
    } catch (e) {
      Promise.resolve().then(() => { self._state(4); self._fire('error'); self._fire('loadend'); });
    }
  }
  _state(s) { this.readyState = s; this._fire('readystatechange'); }
  _fire(type) {
    const ev = new ProgressEvent(type);
    const f = this['on' + type];
    if (typeof f === 'function') { try { f.call(this, ev); } catch (e) { report(e); } }
    this.dispatchEvent(ev);
  }
}
Object.assign(XMLHttpRequest, { UNSENT: 0, OPENED: 1, HEADERS_RECEIVED: 2, LOADING: 3, DONE: 4 });

// ------------------------------------------------------------------ storage
function makeStorage(local) {
  const api = {
    getItem(k) { return native('storage_get', local, str(k)); },
    setItem(k, v) { native('storage_set', local, str(k), str(v)); },
    removeItem(k) { native('storage_remove', local, str(k)); },
    clear() { native('storage_clear', local); },
    key(i) { const k = J(native('storage_keys', local)); return i < k.length ? k[i] : null; },
    get length() { return J(native('storage_keys', local)).length; },
  };
  return new Proxy(api, {
    get(t, k) { if (typeof k !== 'string' || k in t) return Reflect.get(t, k); const v = t.getItem(k); return v === null ? undefined : v; },
    set(t, k, v) { if (typeof k === 'string') t.setItem(k, v); return true; },
    deleteProperty(t, k) { if (typeof k === 'string') t.removeItem(k); return true; },
    has(t, k) { return typeof k === 'string' && (k in t || t.getItem(k) !== null); },
    ownKeys() { return J(native('storage_keys', local)); },
    getOwnPropertyDescriptor(t, k) { const v = typeof k === 'string' ? t.getItem(k) : null; return v === null ? undefined : { value: v, enumerable: true, configurable: true, writable: true }; },
  });
}

// ----------------------------------------------------------- misc globals
function fmt(args) {
  return args.map((a) => {
    if (typeof a === 'string') return a;
    try {
      if (a instanceof Error) return String(a);
      if (a && typeof a === 'object' && H.has(a)) return '<' + (a.nodeName || 'node').toLowerCase() + '>';
      const s = JSON.stringify(a);
      return s === undefined ? String(a) : s.length > 500 ? s.slice(0, 500) + '…' : s;
    } catch (e) { try { return String(a); } catch (e2) { return '[object]'; } }
  }).join(' ');
}
const counts = Object.create(null), timersLog = Object.create(null);
const console = {};
for (const level of ['log', 'info', 'warn', 'error', 'debug', 'trace', 'dir', 'dirxml', 'table'])
  console[level] = (...a) => { try { N('log', level, fmt(a)); } catch (e) {} };
console.assert = (c, ...a) => { if (!c) console.error('Assertion failed:', ...a); };
console.count = (l) => { l = l === undefined ? 'default' : str(l); counts[l] = (counts[l] || 0) + 1; console.log(l + ': ' + counts[l]); };
console.countReset = (l) => { delete counts[l === undefined ? 'default' : str(l)]; };
console.time = (l) => { timersLog[l === undefined ? 'default' : str(l)] = Date.now(); };
console.timeEnd = console.timeLog = (l) => { l = l === undefined ? 'default' : str(l); if (l in timersLog) console.log(l + ': ' + (Date.now() - timersLog[l]) + 'ms'); };
console.group = console.groupCollapsed = console.groupEnd = console.clear = () => {};

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
function btoa(s) {
  s = str(s);
  let out = '';
  for (let i = 0; i < s.length; i += 3) {
    const a = s.charCodeAt(i), b = s.charCodeAt(i + 1), c = s.charCodeAt(i + 2);
    if (a > 255 || b > 255 || c > 255) throw new DOMException('string contains characters outside Latin1', 'InvalidCharacterError');
    const n = (a << 16) | ((b || 0) << 8) | (c || 0);
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + (i + 1 < s.length ? B64[(n >> 6) & 63] : '=') + (i + 2 < s.length ? B64[n & 63] : '=');
  }
  return out;
}
function atob(s) {
  s = str(s).replace(/[\s=]+/g, '');
  if (/[^A-Za-z0-9+/]/.test(s) || s.length % 4 === 1) throw new DOMException('invalid base64', 'InvalidCharacterError');
  let out = '', bits = 0, n = 0;
  for (const ch of s) {
    n = (n << 6) | B64.indexOf(ch); bits += 6;
    if (bits >= 8) { bits -= 8; out += String.fromCharCode((n >> bits) & 255); }
  }
  return out;
}
class TextEncoder {
  get encoding() { return 'utf-8'; }
  encode(s) {
    const bin = unescape(encodeURIComponent(s === undefined ? '' : str(s)));
    const a = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
    return a;
  }
}
class TextDecoder {
  get encoding() { return 'utf-8'; }
  decode(buf) {
    if (!buf) return '';
    const a = buf instanceof Uint8Array ? buf : new Uint8Array(buf.buffer || buf);
    let bin = '';
    for (let i = 0; i < a.length; i++) bin += String.fromCharCode(a[i]);
    try { return decodeURIComponent(escape(bin)); } catch (e) { return bin; }
  }
}

function makeObserver(name, onObserve) {
  return class {
    constructor(cb) { if (typeof cb !== 'function') throw new TypeError(name + ': callback is not a function'); this._cb = cb; this._targets = []; }
    observe(target) { if (!this._targets.includes(target)) this._targets.push(target); if (onObserve) onObserve(this, target); }
    unobserve(target) { this._targets = this._targets.filter((t) => t !== target); }
    disconnect() { this._targets = []; }
    takeRecords() { return []; }
  };
}
const INTERSECTIONS = new WeakMap();
class IntersectionObserverEntry {
  constructor(init) { INTERSECTIONS.set(this, init); }
}
for (const key of ['target', 'isIntersecting', 'intersectionRatio', 'boundingClientRect',
                    'intersectionRect', 'rootBounds', 'time']) {
  Object.defineProperty(IntersectionObserverEntry.prototype, key, {
    get() { return INTERSECTIONS.get(this)[key]; }, enumerable: true,
  });
}
// Elements are treated as always on screen, so lazy-loading code shows its content.
const IntersectionObserver = makeObserver('IntersectionObserver', (obs, target) => {
  setTimeout(() => {
    if (!obs._targets.includes(target)) return;
    const r = target.getBoundingClientRect();
    obs._cb([new IntersectionObserverEntry({ target, isIntersecting: true, intersectionRatio: 1, boundingClientRect: r, intersectionRect: r, rootBounds: null, time: performance.now() })], obs);
  }, 0);
});
const MutationObserver = makeObserver('MutationObserver');
const ResizeObserver = makeObserver('ResizeObserver');
const PerformanceObserver = makeObserver('PerformanceObserver');
PerformanceObserver.supportedEntryTypes = Object.freeze([]);

const performance = {
  now() { return Date.now() - now0; },
  get timeOrigin() { return now0; },
  timing: { navigationStart: now0 },
  mark() {}, measure() {}, clearMarks() {}, clearMeasures() {},
  getEntries() { return []; }, getEntriesByType() { return []; }, getEntriesByName() { return []; },
};

function matchMedia(q) {
  q = str(q);
  return {
    media: q, matches: native('media', q), onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
  };
}

const CSS = {
  supports() { return false; },
  escape(s) { return str(s).replace(/([^\w-])/g, '\\$1').replace(/^(\d)/, '\\3$1 '); },
};

function viewport() { return J(native('viewport')); }
const screen = Object.freeze({ width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, colorDepth: 24, pixelDepth: 24, orientation: { type: 'landscape-primary', angle: 0 } });

const crypto = {
  getRandomValues(arr) {
    if (!arr || typeof arr.length !== 'number' || !arr.buffer) throw new TypeError('expected a typed array');
    const bytes = new Uint8Array(arr.buffer, arr.byteOffset, arr.byteLength);
    if (bytes.length > 65536) throw new DOMException('too many bytes requested', 'QuotaExceededError');
    const hex = native('random', bytes.length);
    for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
    return arr;
  },
  randomUUID() {
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
  },
};

function Image(w, h) {
  const img = document.createElement('img');
  if (w !== undefined) img.setAttribute('width', w);
  if (h !== undefined) img.setAttribute('height', h);
  return img;
}
Image.prototype = HTMLImageElement.prototype;
function Option(text, value, defaultSelected, selected) {
  const o = document.createElement('option');
  if (text !== undefined) o.textContent = text;
  if (value !== undefined) o.setAttribute('value', value);
  if (defaultSelected) o.setAttribute('selected', '');
  return o;
}
Option.prototype = HTMLOptionElement.prototype;

// --------------------------------------------------------- window object
const globals = {
  window: G, self: G, top: G, parent: G, frames: G, opener: null, closed: false, length: 0, frameElement: null,
  document, location, history, navigator, screen, console, performance, crypto, CSS,
  localStorage: makeStorage(true), sessionStorage: makeStorage(false),
  setTimeout: (fn, ms, ...args) => makeTimer(fn, ms, args, false),
  setInterval: (fn, ms, ...args) => makeTimer(fn, ms, args, true),
  clearTimeout: clearTimer, clearInterval: clearTimer,
  requestAnimationFrame: (fn) => makeTimer(() => fn(performance.now()), 16, [], false),
  cancelAnimationFrame: clearTimer,
  requestIdleCallback: (fn) => makeTimer(() => fn({ didTimeout: false, timeRemaining: () => 10 }), 1, [], false),
  cancelIdleCallback: clearTimer,
  queueMicrotask: (fn) => { Promise.resolve().then(fn).catch(report); },
  structuredClone: (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v))),
  reportError: report,
  fetch, Headers, Request, Response, FormData, XMLHttpRequest, URL, URLSearchParams,
  alert: (m) => { native('alert', 'alert', m === undefined ? '' : str(m)); },
  confirm: (m) => { native('alert', 'confirm', m === undefined ? '' : str(m)); return false; },
  prompt: (m) => { native('alert', 'prompt', m === undefined ? '' : str(m)); return null; },
  print() {}, stop() {}, focus() {}, blur() {}, close() {}, postMessage() {}, moveTo() {}, resizeTo() {},
  open() { native('log', 'warn', 'window.open() blocked'); return null; },
  scrollTo() {}, scrollBy() {}, scroll() {},
  getComputedStyle: (el) => computedStyle(el),
  getSelection: () => ({ rangeCount: 0, toString: () => '', removeAllRanges() {}, addRange() {}, getRangeAt() { return document.createRange(); } }),
  matchMedia, atob, btoa, TextEncoder, TextDecoder,
  DOMParser, IntersectionObserverEntry, IntersectionObserver, MutationObserver, ResizeObserver, PerformanceObserver,
  EventTarget, Event, CustomEvent, UIEvent, MouseEvent, PointerEvent, KeyboardEvent, FocusEvent, InputEvent,
  SubmitEvent, ProgressEvent, ErrorEvent, DOMException, DOMRect, DOMTokenList,
  Node, CharacterData, Text, Comment, Element, HTMLElement, Document, HTMLDocument, DocumentFragment: DF,
  HTMLAnchorElement, HTMLInputElement, HTMLTextAreaElement, HTMLSelectElement, HTMLOptionElement,
  HTMLButtonElement, HTMLFormElement, HTMLImageElement, HTMLScriptElement, HTMLLinkElement, HTMLStyleElement,
  HTMLIFrameElement, HTMLCanvasElement, HTMLTemplateElement, HTMLDetailsElement, HTMLDialogElement,
  HTMLLabelElement, HTMLMediaElement, HTMLVideoElement: HTMLMediaElement, HTMLAudioElement: HTMLMediaElement,
  HTMLBodyElement, HTMLHtmlElement, HTMLHeadElement, HTMLDivElement, HTMLSpanElement, HTMLParagraphElement,
  HTMLHeadingElement, HTMLUListElement, HTMLOListElement, HTMLLIElement, HTMLTableElement, HTMLTableRowElement,
  HTMLTableCellElement, HTMLPreElement, HTMLBRElement, HTMLHRElement, HTMLMetaElement, HTMLTitleElement,
  HTMLUnknownElement, SVGElement, Image, Option, NodeList: Array, HTMLCollection: Array,
};
for (const k of Object.keys(globals)) {
  Object.defineProperty(G, k, { value: globals[k], writable: true, configurable: true, enumerable: false });
}
// QuickJS (2021) predates WeakRef / FinalizationRegistry: hold the target strongly instead.
if (typeof G.WeakRef !== 'function') {
  G.WeakRef = class WeakRef { constructor(t) { this._t = t; } deref() { return this._t; } };
}
if (typeof G.FinalizationRegistry !== 'function') {
  G.FinalizationRegistry = class FinalizationRegistry { register() {} unregister() { return false; } };
}
if (typeof G.AbortController !== 'function') {
  class AbortSignal extends EventTarget {
    constructor() { super(); this.aborted = false; this.reason = undefined; this.onabort = null; }
    throwIfAborted() { if (this.aborted) throw this.reason; }
    _abort(reason) {
      if (this.aborted) return;
      this.aborted = true;
      this.reason = reason === undefined ? new DOMException('signal is aborted without reason', 'AbortError') : reason;
      const ev = new Event('abort');
      if (typeof this.onabort === 'function') { try { this.onabort.call(this, ev); } catch (e) { report(e); } }
      this.dispatchEvent(ev);
    }
    static abort(reason) { const s = new AbortSignal(); s._abort(reason); return s; }
    static timeout(ms) {
      const s = new AbortSignal();
      G.setTimeout(() => s._abort(new DOMException('signal timed out', 'TimeoutError')), ms);
      return s;
    }
    static any(signals) {
      const s = new AbortSignal();
      for (const t of signals) {
        if (t.aborted) { s._abort(t.reason); break; }
        t.addEventListener('abort', () => s._abort(t.reason));
      }
      return s;
    }
  }
  G.AbortSignal = AbortSignal;
  G.AbortController = class AbortController {
    constructor() { this.signal = new AbortSignal(); }
    abort(reason) { this.signal._abort(reason); }
  };
}
// document.fonts: web fonts are not downloaded, so every load "succeeds" at once with no faces.
const fontSet = new Set();
Object.assign(fontSet, {
  status: 'loaded', onloading: null, onloadingdone: null, onloadingerror: null,
  ready: Promise.resolve(),
  load: () => Promise.resolve([]),
  check: () => true,
  addEventListener() {}, removeEventListener() {},
});
fontSet.ready = Promise.resolve(fontSet);
Object.defineProperty(document, 'fonts', { value: fontSet, configurable: true });
G.FontFace = class FontFace {
  constructor(family, source, desc) { this.family = family; this.status = 'unloaded'; Object.assign(this, desc || {}); }
  load() { this.status = 'loaded'; return Promise.resolve(this); }
};
Object.defineProperty(G, 'location', { get() { return location; }, set(v) { location.href = v; }, configurable: false });
Object.defineProperty(G, 'document', { value: document, writable: false, configurable: false });
for (const [k, fn] of [['innerWidth', () => viewport().width], ['innerHeight', () => viewport().height],
  ['outerWidth', () => viewport().width], ['outerHeight', () => viewport().height],
  ['scrollX', () => 0], ['scrollY', () => viewport().scrollY], ['pageXOffset', () => 0], ['pageYOffset', () => viewport().scrollY],
  ['screenX', () => 0], ['screenY', () => 0], ['devicePixelRatio', () => 1], ['isSecureContext', () => location.protocol === 'https:'],
  ['origin', () => location.origin]]) {
  Object.defineProperty(G, k, { get: fn, set(v) {}, configurable: true });
}
for (const k of ['addEventListener', 'removeEventListener', 'dispatchEvent'])
  Object.defineProperty(G, k, { value: EventTarget.prototype[k], writable: true, configurable: true });
defineOnProps(G, WINDOW_EVENTS);
G.name = '';
G.status = '';

// ------------------------------------------ entry points for the browser
function hostFn(name, fn) {
  Object.defineProperty(G, '__host_' + name, { value: fn, writable: false, configurable: false, enumerable: false });
}
const EVENT_CLASSES = { click: MouseEvent, dblclick: MouseEvent, mousedown: MouseEvent, mouseup: MouseEvent,
  mouseover: MouseEvent, mouseout: MouseEvent, mousemove: MouseEvent, contextmenu: MouseEvent,
  keydown: KeyboardEvent, keyup: KeyboardEvent, keypress: KeyboardEvent, input: InputEvent,
  focus: FocusEvent, blur: FocusEvent, focusin: FocusEvent, focusout: FocusEvent, submit: SubmitEvent,
  pointerdown: PointerEvent, pointerup: PointerEvent, pointermove: PointerEvent, pointerover: PointerEvent,
  pointerout: PointerEvent };
hostFn('dispatch', (h, type, initJson) => {
  const init = JSON.parse(initJson);
  const target = h === -1 ? G : wrap(h);
  if (!target) return false;
  if (init.submitter !== undefined) init.submitter = wrap(init.submitter);
  const cls = EVENT_CLASSES[type] || Event;
  const ev = new cls(type, init);
  Object.defineProperty(ev, 'isTrusted', { value: true });
  dispatch(target, ev);
  return ev.defaultPrevented;
});
hostFn('fireTimer', fireTimer);
hostFn('fetchDone', fetchDone);
hostFn('setReady', (s) => {
  readyState = s;
  try { dispatch(document, new Event('readystatechange')); } catch (e) { report(e); }
});
hostFn('setCurrent', (h) => { currentScript = h === -1 ? null : wrap(h); });
hostFn('runInline', (code) => { (0, eval)(code); });
})();
