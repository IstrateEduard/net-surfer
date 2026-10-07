"""CSS parser: stylesheets -> rules made of selectors and declarations.

Supports: type/class/id/universal/attribute selectors, compound selectors,
descendant / child / adjacent / general-sibling combinators, a few
pseudo-classes (:first-child, :last-child, :nth-child, :not, :root, :link,
:any-link), selector lists, !important, @media (evaluated against the
viewport width), @import, @supports (evaluated against what the engine
supports) and shorthand expansion for margin/padding/border/background/font/
list-style/flex/gap/grid.
"""
import re

# ---------------------------------------------------------------------------
# Selectors


class Selector:
    """A complex selector: a list of (combinator, CompoundSelector) from left
    to right. The first combinator is None."""

    def __init__(self, parts):
        self.parts = parts
        a = b = c = 0
        for _, compound in parts:
            sa, sb, sc = compound.specificity
            a, b, c = a + sa, b + sb, c + sc
        self.specificity = (a, b, c)
        # Features that some ancestor must have for this selector to match
        # (from compounds joined to the right by descendant/child combinators).
        req = []
        for i in range(len(parts) - 1):
            nxt = parts[i + 1][0]
            if nxt not in (" ", ">"):
                continue
            # only if every combinator to the right of this compound is ancestral
            if any(p[0] not in (" ", ">") for p in parts[i + 1:]):
                continue
            comp = parts[i][1]
            if comp.tag and comp.tag != "*":
                req.append(comp.tag)
            req += ["#" + x for x in comp.ids]
            req += ["." + x for x in comp.classes]
        self.ancestor_requirements = tuple(req)
        self.pseudo_element = parts[-1][1].pseudo_element

    def matches(self, element):
        return _match_from(self.parts, len(self.parts) - 1, element)

    def __repr__(self):
        return "Selector(%r)" % self.parts


def _match_from(parts, idx, element):
    combinator, compound = parts[idx]
    if not compound.matches(element):
        return False
    if idx == 0:
        return True
    comb = parts[idx][0]
    if comb == " ":
        node = element.parent
        while node is not None and hasattr(node, "tag"):
            if _match_from(parts, idx - 1, node):
                return True
            node = node.parent
        return False
    if comb == ">":
        parent = element.parent
        return parent is not None and hasattr(parent, "tag") and _match_from(parts, idx - 1, parent)
    if comb in ("+", "~"):
        sibs = _element_siblings_before(element)
        if comb == "+":
            return bool(sibs) and _match_from(parts, idx - 1, sibs[-1])
        return any(_match_from(parts, idx - 1, s) for s in sibs)
    return False


def _element_siblings_before(element):
    parent = element.parent
    if parent is None:
        return []
    out = []
    for c in parent.children:
        if c is element:
            break
        if hasattr(c, "tag") and not getattr(c, "is_pseudo", False):
            out.append(c)
    return out


def _element_index(element, from_end=False, same_type=False):
    parent = element.parent
    if parent is None:
        return 1
    sibs = [c for c in parent.children if hasattr(c, "tag") and not getattr(c, "is_pseudo", False)
            and (not same_type or c.tag == element.tag)]
    if from_end:
        sibs.reverse()
    for i, s in enumerate(sibs):
        if s is element:
            return i + 1
    return 1


def _nth_matches(expr, index):
    expr = expr.replace(" ", "").lower()
    if expr == "odd":
        a, b = 2, 1
    elif expr == "even":
        a, b = 2, 0
    elif "n" in expr:
        a_s, _, b_s = expr.partition("n")
        a = -1 if a_s == "-" else (1 if a_s in ("", "+") else int(a_s))
        b = int(b_s) if b_s else 0
    else:
        return index == int(expr)
    if a == 0:
        return index == b
    return (index - b) % a == 0 and (index - b) / a >= 0


class CompoundSelector:
    def __init__(self):
        self.tag = None
        self.ids = []
        self.classes = []
        self.attrs = []       # (name, op, value)
        self.pseudos = []     # (name, arg)
        self.never = False    # unsupported pseudo-elements
        self.pseudo_element = None   # "before" / "after"

    @property
    def specificity(self):
        a = len(self.ids)
        b = len(self.classes) + len(self.attrs) + sum(1 for p in self.pseudos if p[0] not in ("not", "is", "where"))
        c = 1 if self.tag and self.tag != "*" else 0
        for name, arg in self.pseudos:
            if name in ("not", "is") and arg:
                best = max((s.specificity for s in arg), default=(0, 0, 0))
                a, b, c = a + best[0], b + best[1], c + best[2]
        return (a, b, c)

    def matches(self, el):
        if self.never:
            return False
        if self.tag and self.tag != "*" and el.tag != self.tag:
            return False
        attrs = el.attributes
        for i in self.ids:
            if attrs.get("id") != i:
                return False
        if self.classes:
            cls = attrs.get("class", "").split()
            for c in self.classes:
                if c not in cls:
                    return False
        for name, op, value in self.attrs:
            if name not in attrs:
                return False
            actual = attrs[name]
            if op is None:
                continue
            if op == "=" and actual != value:
                return False
            if op == "~=" and value not in actual.split():
                return False
            if op == "|=" and not (actual == value or actual.startswith(value + "-")):
                return False
            if op == "^=" and not actual.startswith(value):
                return False
            if op == "$=" and not actual.endswith(value):
                return False
            if op == "*=" and value not in actual:
                return False
        for name, arg in self.pseudos:
            if not _pseudo_matches(name, arg, el):
                return False
        return True

    def __repr__(self):
        return "Compound(%s#%s.%s)" % (self.tag, self.ids, self.classes)


def _pseudo_matches(name, arg, el):
    if name in ("link", "any-link"):
        return el.tag == "a" and "href" in el.attributes
    if name == "root":
        return el.parent is None or not hasattr(el.parent, "tag")
    if name == "first-child":
        return _element_index(el) == 1
    if name == "last-child":
        return _element_index(el, from_end=True) == 1
    if name == "only-child":
        return _element_index(el) == 1 and _element_index(el, from_end=True) == 1
    if name == "first-of-type":
        return _element_index(el, same_type=True) == 1
    if name == "last-of-type":
        return _element_index(el, from_end=True, same_type=True) == 1
    if name == "nth-child":
        try:
            return _nth_matches(arg, _element_index(el))
        except ValueError:
            return False
    if name == "nth-last-child":
        try:
            return _nth_matches(arg, _element_index(el, from_end=True))
        except ValueError:
            return False
    if name == "nth-of-type":
        try:
            return _nth_matches(arg, _element_index(el, same_type=True))
        except ValueError:
            return False
    if name == "not":
        if IGNORE_DYNAMIC[0] and any(is_dynamic_compound(c) for s in arg for _, c in s.parts):
            return True
        return not any(s.matches(el) for s in arg)
    if name in ("is", "where", "matches"):
        return any(s.matches(el) for s in arg)
    if name == "empty":
        return not el.children
    if name == "has":
        return any(_has_match(el, comb, sel) for comb, sel in arg)
    if name == "only-of-type":
        return _element_index(el, same_type=True) == 1 and _element_index(el, from_end=True, same_type=True) == 1
    if name == "nth-last-of-type":
        try:
            return _nth_matches(arg, _element_index(el, from_end=True, same_type=True))
        except ValueError:
            return False
    if name == "lang":
        want = (arg or "").strip().strip("\"'").lower()
        node = el
        while node is not None and hasattr(node, "attributes"):
            lang = node.attributes.get("lang") or node.attributes.get("xml:lang")
            if lang is not None:
                lang = lang.lower()
                return lang == want or lang.startswith(want + "-")
            node = node.parent
        return False
    if name == "target":
        if IGNORE_DYNAMIC[0] and name in IGNORE_DYNAMIC[0]:
            return True
        return getattr(el, "target_state", False)
    if name in FORM_STATE_PSEUDOS and IGNORE_DYNAMIC[0] and name in IGNORE_DYNAMIC[0]:
        return True
    if name == "dir":
        want = (arg or "ltr").strip().lower()
        return element_direction(el) == want
    if name in ("valid", "invalid", "user-valid", "user-invalid"):
        if el.tag == "form" or el.tag == "fieldset":
            bad = any(hasattr(d, "tag") and d.tag in ("input", "select", "textarea") and not field_valid(d)
                      for d in el.descendants())
        elif el.tag in ("input", "select", "textarea"):
            bad = not field_valid(el)
        else:
            return False
        if name.startswith("user-") and el.tag in ("input", "select", "textarea") and el.form_value is None:
            return False      # the user has not touched it yet
        return bad if name.endswith("invalid") else not bad
    if name in ("in-range", "out-of-range"):
        r = _range_state(el)
        if r is None:
            return False
        return r if name == "in-range" else not r
    if name == "indeterminate":
        if el.tag == "input" and el.attributes.get("type", "").lower() == "radio":
            group = el.attributes.get("name")
            root = el
            while root.parent is not None and getattr(root, "tag", "") != "form":
                root = root.parent
            return not any(getattr(d, "checked", False) for d in root.descendants()
                           if getattr(d, "tag", "") == "input" and d.attributes.get("name") == group)
        return el.tag == "progress" and "value" not in el.attributes
    if name == "placeholder-shown":
        return el.tag in ("input", "textarea") and "placeholder" in el.attributes and \
            not (el.form_value if el.form_value is not None else el.attributes.get("value", ""))
    if name == "required":
        return "required" in el.attributes
    if name == "optional":
        return el.tag in ("input", "select", "textarea") and "required" not in el.attributes
    if name == "read-only":
        return not (el.tag in ("input", "textarea") and "readonly" not in el.attributes
                    and "disabled" not in el.attributes) and el.attributes.get("contenteditable") is None
    if name == "read-write":
        return el.tag in ("input", "textarea") and "readonly" not in el.attributes and "disabled" not in el.attributes
    if name == "default":
        return getattr(el, "checked", False) and "checked" in el.attributes or \
            (el.tag == "option" and "selected" in el.attributes)
    if name in ("defined", "scope"):
        return True
    if name in ("visited",):
        return False
    if name == "checked":
        if IGNORE_DYNAMIC[0] and name in IGNORE_DYNAMIC[0]:
            return True
        return getattr(el, "checked", False)
    if name == "disabled":
        return "disabled" in el.attributes
    if name == "enabled":
        return "disabled" not in el.attributes
    if name in DYNAMIC_PSEUDOS:
        # User-interaction states, set on elements by the window (gui.py).
        if IGNORE_DYNAMIC[0] and name in IGNORE_DYNAMIC[0]:
            return True
        if name == "hover":
            return getattr(el, "hover_state", False)
        if name == "active":
            return getattr(el, "active_state", False)
        if name == "focus-within":
            return getattr(el, "focus_within_state", False)
        return getattr(el, "focus_state", False)
    # :visited and anything unknown never match
    return False


FORM_STATE_PSEUDOS = ("valid", "invalid", "user-valid", "user-invalid", "placeholder-shown", "in-range",
                      "out-of-range", "indeterminate")
DYNAMIC_PSEUDOS = ("hover", "active", "focus", "focus-visible", "focus-within", "checked", "target") + \
    FORM_STATE_PSEUDOS
PSEUDO_ELEMENTS = ("before", "after", "first-letter", "first-line", "marker", "placeholder", "selection")


def element_direction(el):
    node = el
    while node is not None and hasattr(node, "attributes"):
        d = node.attributes.get("dir", "").lower()
        if d in ("ltr", "rtl"):
            return d
        node = node.parent
    return "ltr"


def field_value(el):
    if el.form_value is not None:
        return el.form_value if isinstance(el.form_value, str) else ""
    if el.tag == "textarea":
        return el.text_content()
    return el.attributes.get("value", "")


def field_valid(el):
    """HTML constraint validation for :valid / :invalid."""
    a = el.attributes
    if "disabled" in a or a.get("type", "").lower() in ("hidden", "submit", "button", "reset", "image"):
        return True
    kind = a.get("type", "text").lower()
    if kind in ("checkbox", "radio"):
        if "required" not in a:
            return True
        if kind == "checkbox":
            return el.checked
        root = el
        while root.parent is not None and getattr(root, "tag", "") != "form":
            root = root.parent
        return any(getattr(d, "checked", False) for d in root.descendants()
                   if getattr(d, "tag", "") == "input" and d.attributes.get("name") == a.get("name"))
    v = field_value(el)
    if el.tag == "select":
        return "required" not in a or bool(v)
    if not v:
        return "required" not in a
    if kind == "email" and not re.match(r"^[^@\s]+@[^@\s]+$", v):
        return False
    if kind == "url" and not re.match(r"^[a-zA-Z][\w+.-]*:\S+$", v):
        return False
    if kind in ("number", "range"):
        try:
            float(v)
        except ValueError:
            return False
        if _range_state(el) is False:
            return False
    if "pattern" in a:
        try:
            if not re.fullmatch(a["pattern"], v):
                return False
        except re.error:
            pass
    if "minlength" in a and a["minlength"].isdigit() and len(v) < int(a["minlength"]):
        return False
    if "maxlength" in a and a["maxlength"].isdigit() and len(v) > int(a["maxlength"]):
        return False
    return True


def _range_state(el):
    """True/False for in-range/out-of-range number inputs, None if not applicable."""
    if el.tag != "input" or el.attributes.get("type", "").lower() not in ("number", "range", "date"):
        return None
    if "min" not in el.attributes and "max" not in el.attributes:
        return None
    try:
        v = float(field_value(el))
    except ValueError:
        return True
    try:
        if "min" in el.attributes and v < float(el.attributes["min"]):
            return False
        if "max" in el.attributes and v > float(el.attributes["max"]):
            return False
    except ValueError:
        pass
    return True


def _parse_relative_list(text):
    """The argument of :has(): relative selectors like '> img', '+ p', '.x'."""
    out = []
    for part in _split_top_level(text, ","):
        part = part.strip()
        if not part:
            continue
        comb = " "
        if part[0] in ">+~":
            comb, part = part[0], part[1:].strip()
        sel = parse_selector(part)
        if sel is not None:
            out.append((comb, sel))
    return out


def _has_match(el, comb, sel):
    if comb == ">":
        return any(hasattr(c, "tag") and sel.matches(c) for c in el.children)
    if comb in ("+", "~"):
        parent = el.parent
        if parent is None:
            return False
        sibs = [c for c in parent.children if hasattr(c, "tag") and not getattr(c, "is_pseudo", False)]
        try:
            i = sibs.index(el)
        except ValueError:
            return False
        after = sibs[i + 1:i + 2] if comb == "+" else sibs[i + 1:]
        return any(sel.matches(c) for c in after)
    stack = list(el.children)
    while stack:
        c = stack.pop()
        if hasattr(c, "tag"):
            if not getattr(c, "is_pseudo", False) and sel.matches(c):
                return True
            stack.extend(c.children)
    return False
# While set to a collection of names, those dynamic pseudo-classes always
# match: used to find which elements a hover/focus change can affect.
IGNORE_DYNAMIC = [None]


def is_dynamic_compound(compound):
    """Does this compound selector test :hover/:focus/:active (also inside :not/:is)?"""
    for name, arg in compound.pseudos:
        if name in DYNAMIC_PSEUDOS:
            return True
        if name in ("not", "is", "where", "matches") and arg and \
                any(is_dynamic_compound(c) for s in arg for _, c in s.parts):
            return True
    return False


IDENT = r"-?(?:[_a-zA-Z -￿]|\\.)(?:[_a-zA-Z0-9 -￿-]|\\.)*"
_COMPOUND_TOKEN = re.compile(
    r"(?P<star>\*)|(?P<tag>%s)|#(?P<id>(?:[_a-zA-Z0-9 -￿-]|\\.)+)|\.(?P<cls>%s)"
    r"|\[\s*(?P<an>[^\s~|^$*=\]]+)\s*(?:(?P<op>[~|^$*]?=)\s*(?P<av>\"[^\"]*\"|'[^']*'|[^\]\s]+)\s*(?:[iIsS]\s*)?)?\]"
    r"|(?P<pe>::?)(?P<pn>[a-zA-Z-]+)(?:\((?P<pa>[^()]*(?:\([^()]*\)[^()]*)*)\))?"
    % (IDENT, IDENT)
)


# url(...) with a double-quoted, single-quoted or bare argument (data: URIs often hold the other quote)
URL_RE = re.compile(r"""url\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|((?:[^'"()\s\\]|\\.)*))\s*\)""", re.S)


def unescape_string(s):
    r"""CSS string escapes: \3c / \00003c (hex, optional trailing space), \" and line continuations."""
    if "\\" not in s:
        return s

    def rep(m):
        if m.group(1):
            cp = int(m.group(1), 16)
            return chr(cp) if 0 < cp <= 0x10FFFF else "\ufffd"
        return "" if m.group(2) in "\n\r\f" else m.group(2)
    return re.sub(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n]?|(.))", rep, s, flags=re.S)


def url_of(m):
    """The (unescaped) URL of a URL_RE match."""
    return unescape_string(next((g for g in m.groups() if g is not None), "")).strip()


def css_urls(value):
    return [url_of(m) for m in URL_RE.finditer(value)]


def _unescape_ident(s):
    return re.sub(r"\\(.)", r"\1", s)


def parse_selector_list(text):
    """Parse 'a, b > c' into a list of Selector. Unparseable parts are skipped."""
    out = []
    for part in _split_top_level(text, ","):
        part = part.strip()
        if not part:
            continue
        sel = parse_selector(part)
        if sel is not None:
            out.append(sel)
    return out


def parse_selector(text):
    if "|" in text:   # @namespace prefixes (svg|rect, *|a): HTML documents have one namespace
        text = re.sub(r"(?<![\[=~^$*|])(?:[\w-]+|\*)?\|(?!=)", "", text)
    parts = []
    i = 0
    n = len(text)
    combinator = None
    while i < n:
        # whitespace / combinators
        j = i
        while j < n and text[j] in " \t\n\r\f":
            j += 1
        if j < n and text[j] in ">+~":
            combinator = text[j]
            j += 1
            while j < n and text[j] in " \t\n\r\f":
                j += 1
        elif j > i and parts:
            combinator = " "
        i = j
        if i >= n:
            break
        compound = CompoundSelector()
        start = i
        while i < n:
            m = _COMPOUND_TOKEN.match(text, i)
            if not m:
                break
            if m.group("star"):
                compound.tag = "*"
            elif m.group("tag"):
                if i != start:
                    return None
                compound.tag = m.group("tag").lower()
            elif m.group("id"):
                compound.ids.append(_unescape_ident(m.group("id")))
            elif m.group("cls"):
                compound.classes.append(_unescape_ident(m.group("cls")))
            elif m.group("an"):
                av = m.group("av")
                if av and av[0] in "\"'":
                    av = av[1:-1]
                compound.attrs.append((m.group("an").lower(), m.group("op"), av))
            elif m.group("pn"):
                name = m.group("pn").lower()
                arg = m.group("pa")
                if name in PSEUDO_ELEMENTS:
                    compound.pseudo_element = name
                elif name == "-webkit-input-placeholder" or name == "-moz-placeholder":
                    compound.pseudo_element = "placeholder"
                elif m.group("pe") == "::":
                    compound.never = True
                elif name in ("not", "is", "where", "matches"):
                    compound.pseudos.append((name, parse_selector_list(arg or "")))
                elif name == "has":
                    compound.pseudos.append((name, _parse_relative_list(arg or "")))
                else:
                    compound.pseudos.append((name, arg))
            i = m.end()
        if i == start:
            return None  # unparseable
        parts.append((None if not parts else (combinator or " "), compound))
        combinator = None
    if not parts:
        return None
    return Selector(parts)


# ---------------------------------------------------------------------------
# Stylesheets


class Rule:
    def __init__(self, selectors, declarations, order):
        self.selectors = selectors
        self.declarations = declarations  # list of (property, value, important)
        self.order = order

    def __repr__(self):
        return "Rule(%r, %r)" % (self.selectors, self.declarations)


def strip_comments(text):
    return re.sub(r"/\*.*?(\*/|$)", "", text, flags=re.S)


def _split_top_level(text, sep):
    """Split on `sep`, ignoring separators inside (), [] and quotes."""
    out = []
    depth = 0
    quote = None
    cur = []
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return out


def _find_block_end(text, start):
    """Given index just after '{', return index of the matching '}'."""
    depth = 1
    i = start
    n = len(text)
    quote = None
    while i < n:
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


class StyleSheetParser:
    def __init__(self, text, viewport_width=1000, order_start=0, layers=None):
        self.text = strip_comments(text)
        self.viewport_width = viewport_width
        self.order = order_start
        self.imports = []
        self.layers = layers if layers is not None else {}   # layer name -> order of first appearance
        self.keyframes = {}        # @keyframes name -> [(offset 0..1, [(prop, value)])]
        self.counter_styles = {}   # @counter-style name -> descriptors
        self.properties = {}       # @property --name -> (inherits, initial value)
        self.ctx_layer = None
        self.ctx_container = ()
        self.ctx_scope = None
        self.anon_layers = 0

    def parse(self):
        return self._parse_rules(self.text)

    def _layer_index(self, name):
        if name not in self.layers:
            self.layers[name] = len(self.layers)
        return self.layers[name]

    def _make_rule(self, selectors, decls):
        r = Rule(selectors, decls, self.order)
        r.layer = self.layers.get(self.ctx_layer) if self.ctx_layer is not None else None
        r.container = self.ctx_container
        self.order += 1
        return r

    def _scoped(self, prelude):
        """Selectors of a rule inside @scope (start) to (end)."""
        start, end = self.ctx_scope
        out = []
        for part in _split_top_level(prelude, ","):
            part = part.strip()
            if not part:
                continue
            if ":scope" in part:
                part = part.replace(":scope", ":is(%s)" % start)
            else:
                part = ":is(%s) %s" % (start, part)
            if end:
                part += ":not(:is(%s), :is(%s) *)" % (end, end)
            out.append(part)
        return ", ".join(out)

    def _parse_rules(self, text, parent=None):
        rules = []
        if parent is not None:
            # Declarations directly inside a nested block apply to the parent rule.
            decl_text, text = _split_nested(text)
            decls = parse_declarations(decl_text) if decl_text.strip() else []
            selectors = parse_selector_list(parent) if decls else None
            if selectors:
                rules.append(self._make_rule(selectors, decls))
        i = 0
        n = len(text)
        while i < n:
            while i < n and text[i] in " \t\r\n\f;":
                i += 1
            if i >= n:
                break
            if text.startswith("<!--", i) or text.startswith("-->", i):
                i += 4 if text.startswith("<!--", i) else 3
                continue
            if text[i] == "@":
                i = self._parse_at_rule(text, i, rules, parent)
                continue
            brace = text.find("{", i)
            if brace == -1:
                break
            prelude = text[i:brace].strip()
            end = _find_block_end(text, brace + 1)
            body = text[brace + 1:end]
            i = end + 1
            if parent is not None:
                prelude = _nest_selector(prelude, parent)
            elif self.ctx_scope is not None:
                prelude = self._scoped(prelude)
            nested = ""
            if "{" in body:   # CSS nesting
                body, nested = _split_nested(body)
            selectors = parse_selector_list(prelude)
            if selectors:
                decls = parse_declarations(body)
                if decls:
                    rules.append(self._make_rule(selectors, decls))
            if nested:
                rules.extend(self._parse_rules(nested, parent=prelude))
        return rules

    def _parse_at_rule(self, text, i, rules, parent=None):
        m = re.compile(r"@([-a-zA-Z]+)").match(text, i)
        name = m.group(1).lower() if m else ""
        semi = text.find(";", i)
        brace = text.find("{", i)
        if brace == -1 or (semi != -1 and semi < brace):
            # statement at-rule (@import, @charset, @namespace, @layer a, b;)
            end = semi if semi != -1 else len(text)
            arg = text[m.end():end].strip() if m else ""
            if name == "import":
                um = re.match(r"""(?:url\(\s*)?["']?([^"')\s]+)["']?\s*\)?\s*(.*)""", arg)
                if um:
                    media = re.sub(r"(?i)\b(layer|supports)\([^)]*\)|\blayer\b", "", um.group(2)).strip()
                    if not media or media_matches(media, self.viewport_width):
                        self.imports.append(um.group(1))
            elif name == "layer":
                for nm in arg.split(","):
                    nm = nm.strip()
                    if nm:
                        self._layer_index(self._full_layer(nm))
            return end + 1
        prelude = text[m.end():brace].strip() if m else ""
        end = _find_block_end(text, brace + 1)
        body = text[brace + 1:end]
        if name == "media":
            if media_matches(prelude, self.viewport_width):
                rules.extend(self._parse_rules(body, parent))
        elif name == "supports":
            if supports_matches(prelude):
                rules.extend(self._parse_rules(body, parent))
        elif name in ("document", "-moz-document", "starting-style"):
            if name != "starting-style":
                rules.extend(self._parse_rules(body, parent))
        elif name == "layer":
            if prelude:
                full = self._full_layer(prelude.split(",")[0].strip())
            else:
                self.anon_layers += 1
                full = self._full_layer("\0anon%d" % self.anon_layers)
            self._layer_index(full)
            saved, self.ctx_layer = self.ctx_layer, full
            try:
                rules.extend(self._parse_rules(body, parent))
            finally:
                self.ctx_layer = saved
        elif name == "container":
            cm = re.match(r"^(?!not\b|style\b)([a-zA-Z_][\w-]*)?\s*(.*)$", prelude, re.S)
            cname, cond = (cm.group(1), cm.group(2).strip()) if cm else (None, prelude)
            saved = self.ctx_container
            self.ctx_container = saved + ((cname, cond),)
            try:
                rules.extend(self._parse_rules(body, parent))
            finally:
                self.ctx_container = saved
        elif name == "scope":
            sm = re.match(r"^\s*(?:\((.*?)\))?\s*(?:to\s*\((.*)\))?\s*$", prelude, re.S)
            start = (sm.group(1) or ":root").strip() if sm else ":root"
            end_sel = (sm.group(2) or "").strip() if sm else ""
            saved, self.ctx_scope = self.ctx_scope, (start, end_sel)
            try:
                rules.extend(self._parse_rules(body, parent))
            finally:
                self.ctx_scope = saved
        elif name in ("keyframes", "-webkit-keyframes", "-moz-keyframes"):
            frames = []
            for sel, decl_body in _blocks(body):
                decls = [(p2, v2) for p2, v2, _ in parse_declarations(decl_body)]
                for k in sel.split(","):
                    k = k.strip().lower()
                    off = 0.0 if k == "from" else 1.0 if k == "to" else None
                    if off is None and k.endswith("%"):
                        try:
                            off = float(k[:-1]) / 100
                        except ValueError:
                            off = None
                    if off is not None:
                        frames.append((off, decls))
            frames.sort(key=lambda f: f[0])
            self.keyframes[prelude.strip().strip("\"'")] = frames
        elif name == "counter-style":
            desc = {}
            for dp, dv, _ in parse_declarations(body):
                desc[dp] = dv
            self.counter_styles[prelude.strip().lower()] = desc
        elif name == "property":
            desc = {}
            for dp, dv, _ in parse_declarations(body):
                desc[dp] = dv
            inherits = desc.get("inherits", "true").strip().lower() == "true"
            self.properties[prelude.strip()] = (inherits, desc.get("initial-value"))
        # @font-face, @page (no printing here), @font-feature-values ... are skipped.
        return end + 1

    def _full_layer(self, name):
        return name if self.ctx_layer is None else self.ctx_layer + "." + name


def _blocks(text):
    """'a { x } b { y }' -> [('a', ' x '), ('b', ' y ')]"""
    out = []
    i = 0
    while True:
        brace = text.find("{", i)
        if brace == -1:
            return out
        end = _find_block_end(text, brace + 1)
        out.append((text[i:brace].strip(), text[brace + 1:end]))
        i = end + 1


def _split_nested(body):
    """Separate a rule body into its own declarations and nested rules."""
    decls, nested = [], []
    i, n, start = 0, len(body), 0
    depth, quote = 0, None
    while i < n:
        c = body[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == ";" and depth == 0:
            decls.append(body[start:i + 1])
            start = i + 1
        elif c == "{" and depth == 0:
            end = _find_block_end(body, i + 1)
            nested.append(body[start:end + 1])
            i = end + 1
            start = i
            continue
        i += 1
    decls.append(body[start:])
    return "".join(decls), "\n".join(nested)


def _nest_selector(prelude, parent):
    """Resolve a nested rule's selector against its parent ('&' or implicit descendant)."""
    out = []
    for part in _split_top_level(prelude, ","):
        part = part.strip()
        if not part:
            continue
        if "&" in part:
            out.append(part.replace("&", ":is(%s)" % parent))
        else:
            out.append(":is(%s) %s" % (parent, part))
    return ", ".join(out)


def container_matches(conditions, el):
    """Evaluate the @container conditions of a rule for element `el`, against
    the size its nearest query container had in the last layout."""
    from .style import VIEWPORT
    for cname, cond in conditions:
        node = el.parent
        found = None
        while node is not None and hasattr(node, "style"):
            ct = node.style.get("container-type", "normal")
            if ct in ("inline-size", "size") and (not cname or cname in node.style.get("container-name", "").split()):
                found = node
                break
            node = node.parent
        if found is not None:
            size = getattr(found, "container_size", None) or (VIEWPORT["width"], VIEWPORT["height"])
            found.container_size_used = size
        else:
            size = (VIEWPORT["width"], VIEWPORT["height"])
        if not _container_eval(cond.strip().lower(), size[0], size[1]):
            return False
    return True


def _container_eval(cond, w, h):
    if not cond:
        return True
    parts = _supports_split(cond)
    if len(parts) > 1:
        vals = [_container_eval(x.strip(), w, h) for x in parts[0::2]]
        return all(vals) if parts[1] == "and" else any(vals)
    if cond.startswith(("not ", "not(")):
        return not _container_eval(cond[3:].strip(), w, h)
    if cond.startswith("style("):
        return False
    if cond.startswith("(") and cond.endswith(")") and cond[1:2] == "(":
        return _container_eval(cond[1:-1].strip(), w, h)
    cond = cond.replace("inline-size", "width").replace("block-size", "height")
    return _single_media_matches(cond, w, h)


# Properties and value functions the engine cannot honour, so @supports
# tests for them are false (and the page's fallback rules apply instead).
UNSUPPORTED_PROPS = ("backdrop-filter", "mask-border", "contain-intrinsic", "scroll-timeline",
                     "view-timeline", "animation-timeline", "anchor-name", "position-anchor", "text-wrap",
                     "content-visibility", "-webkit-", "-moz-", "-ms-")
UNSUPPORTED_VALUES = ("anchor(", "env(")


def supports_matches(prelude):
    """Evaluate an @supports condition: (prop: value), not, and, or, selector()."""
    try:
        return _supports_eval(prelude.strip().lower())
    except (ValueError, IndexError):
        return False


def _supports_eval(cond):
    cond = cond.strip()
    if not cond:
        return False
    parts = _supports_split(cond)
    if len(parts) > 1:
        vals = [_supports_eval(x) for x in parts[0::2]]
        return all(vals) if parts[1] == "and" else any(vals)
    if cond.startswith(("not ", "not(")):
        return not _supports_eval(cond[3:])
    if cond.startswith("selector("):
        return True
    if cond.startswith("(") and _matching_paren(cond, 0) == len(cond) - 1:
        inner = cond[1:-1].strip()
        if inner.startswith(("(", "not ", "not(", "selector(")) or len(_supports_split(inner)) > 1:
            return _supports_eval(inner)
        if ":" not in inner:
            return False
        prop, value = (x.strip() for x in inner.split(":", 1))
        if prop.startswith(UNSUPPORTED_PROPS):
            return False
        return not any(x in value for x in UNSUPPORTED_VALUES)
    return False


def _matching_paren(text, i):
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    raise ValueError("unbalanced parentheses")


def _supports_split(cond):
    """Split a condition into [operand, op, operand, ...] at top-level and/or."""
    out, i, start = [], 0, 0
    while i < len(cond):
        if cond[i] == "(":
            i = _matching_paren(cond, i) + 1
            continue
        m = re.match(r"\s+(and|or)\s+", cond[i:])
        if m:
            out += [cond[start:i], m.group(1)]
            i += m.end()
            start = i
            continue
        i += 1
    out.append(cond[start:])
    return out


def media_matches(query, viewport_width):
    """Evaluate a media query list against a screen of the given width."""
    query = query.strip().lower()
    if not query:
        return True
    for q in _split_top_level(query, ","):
        if _single_media_matches(q.strip(), viewport_width):
            return True
    return False


def _single_media_matches(q, width, height=None):
    negate = False
    if q.startswith("not "):
        negate = True
        q = q[4:]
    q = q.replace("only ", "")
    result = True
    for part in re.split(r"\s+and\s+", q):
        part = part.strip()
        if not part:
            continue
        if part in ("all", "screen"):
            continue
        if part in ("print", "speech", "tv", "handheld", "aural", "braille", "embossed", "projection", "tty"):
            result = False
            continue
        if height is None:
            from .style import VIEWPORT
            height = VIEWPORT.get("height", 700)
        if part.startswith("(") and part.endswith(")"):
            rng = _media_range(part, width, height)
            if rng is not None:
                result = result and rng
                continue
        m = re.match(r"\(\s*([-a-z]+)\s*(?::\s*(.+))?\)$", part)
        if not m:
            result = False
            continue
        feat, val = m.group(1), (m.group(2) or "").strip()
        px = _media_length(val)
        if feat == "min-width":
            result = result and px is not None and width >= px
        elif feat == "max-width":
            result = result and px is not None and width <= px
        elif feat == "width":
            result = result and px is not None and abs(width - px) < 0.5
        elif feat == "min-height":
            result = result and px is not None and height >= px
        elif feat == "max-height":
            result = result and px is not None and height <= px
        elif feat in ("min-aspect-ratio", "max-aspect-ratio", "aspect-ratio"):
            r = _media_ratio(val)
            ar = width / max(1, height)
            result = result and r is not None and (ar >= r if feat.startswith("min") else
                                                  ar <= r if feat.startswith("max") else abs(ar - r) < 0.01)
        elif feat == "orientation":
            result = result and val == ("landscape" if width >= height else "portrait")
        elif feat == "prefers-color-scheme":
            result = result and val == "light"
        elif feat in ("prefers-reduced-motion",):
            result = result and val == "no-preference"
        elif feat in ("hover", "any-hover"):
            result = result and val in ("hover", "")
        elif feat in ("pointer", "any-pointer"):
            result = result and val in ("fine", "")
        elif feat in ("min-resolution", "-webkit-min-device-pixel-ratio", "min--moz-device-pixel-ratio"):
            result = False
        elif feat in ("color",):
            pass
        else:
            m2 = re.match(r"\(\s*(\d+(?:\.\d+)?(?:px|em|rem)?)\s*(<=|>=|<|>)\s*width\s*\)", part) or None
            if m2:
                px2 = _media_length(m2.group(1))
                op = m2.group(2)
                result = result and px2 is not None and {"<=": px2 <= width, "<": px2 < width,
                                                         ">=": px2 >= width, ">": px2 > width}[op]
            else:
                m3 = re.match(r"\(\s*width\s*(<=|>=|<|>)\s*(\d+(?:\.\d+)?(?:px|em|rem)?)\s*\)", part)
                if m3:
                    px3 = _media_length(m3.group(2))
                    op = m3.group(1)
                    result = result and px3 is not None and {"<=": width <= px3, "<": width < px3,
                                                             ">=": width >= px3, ">": width > px3}[op]
                else:
                    result = False
    return result != negate


def _media_length(val):
    val = val.strip()
    if val.startswith(("calc(", "min(", "max(", "clamp(")):
        from .style import length
        return length(val, 16, None, None)
    m = re.match(r"(-?\d*\.?\d+)\s*(px|em|rem)?", val)
    if not m:
        return None
    num = float(m.group(1))
    return num * 16 if m.group(2) in ("em", "rem") else num


def _media_ratio(val):
    m = re.match(r"\s*(\d*\.?\d+)\s*(?:/\s*(\d*\.?\d+))?", val)
    if not m:
        return None
    return float(m.group(1)) / float(m.group(2) or 1)


def _media_range(part, width, height):
    """Range syntax: (width >= 600px), (400px < width <= 900px), (height < 50em), (aspect-ratio > 1)."""
    inner = part.strip()[1:-1].strip()
    m = re.match(r"^(?:(.+?)\s*(<=|>=|<|>|=)\s*)?(width|height|aspect-ratio)\s*(?:(<=|>=|<|>|=)\s*(.+))?$", inner)
    if not m or not (m.group(2) or m.group(4)):
        return None
    actual = {"width": width, "height": height, "aspect-ratio": width / max(1, height)}[m.group(3)]
    conv = _media_ratio if m.group(3) == "aspect-ratio" else _media_length
    ops = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
           ">=": lambda a, b: a >= b, "=": lambda a, b: abs(a - b) < 0.5}
    ok = True
    if m.group(2):
        v = conv(m.group(1))
        ok = ok and v is not None and ops[m.group(2)](v, actual)
    if m.group(4):
        v = conv(m.group(5))
        ok = ok and v is not None and ops[m.group(4)](actual, v)
    return ok


# ---------------------------------------------------------------------------
# Declarations and shorthands


def parse_declarations(body):
    out = []
    for decl in _split_top_level(body, ";"):
        if ":" not in decl:
            continue
        prop, _, value = decl.partition(":")
        prop = prop.strip()
        if not prop.startswith("--"):
            prop = prop.lower()
        value = value.strip()
        important = False
        low = value.lower()
        if low.endswith("!important"):
            important = True
            value = value[:-10].strip()
        elif re.search(r"!\s*important$", low):
            important = True
            value = re.sub(r"!\s*important$", "", value, flags=re.I).strip()
        if not prop or not value and not prop.startswith("--"):
            continue
        if prop.startswith("--"):
            out.append((prop, value, important))
            continue
        for p, v in expand_shorthand(prop, value):
            out.append((p, v, important))
    return out


def split_values(value):
    """Split a value on whitespace, keeping function calls like rgb(1, 2, 3) intact."""
    out = []
    depth = 0
    cur = ""
    quote = None
    for ch in value:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            cur += ch
        elif ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch.isspace() and depth == 0:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


BORDER_STYLES = {"none", "hidden", "dotted", "dashed", "solid", "double", "groove", "ridge", "inset", "outset"}
SIDES = ("top", "right", "bottom", "left")


def _four(values):
    if len(values) == 1:
        return values * 4
    if len(values) == 2:
        return [values[0], values[1], values[0], values[1]]
    if len(values) == 3:
        return [values[0], values[1], values[2], values[1]]
    return values[:4]


def _looks_like_length(v):
    return bool(re.match(r"^-?(\d*\.?\d+)(px|em|rem|pt|%|ex|ch|vw|vh|cm|mm|in)?$", v)) or v in ("thin", "medium", "thick") \
        or v.startswith(("calc(", "var(", "min(", "max(", "clamp("))


def _border_parts(value):
    width, style, color = "medium", "none", "currentcolor"
    for v in split_values(value):
        lv = v.lower()
        if lv in BORDER_STYLES:
            style = lv
        elif _looks_like_length(lv) and not lv.startswith("var("):
            width = lv
        else:
            color = v
    return width, style, color


def expand_shorthand(prop, value):
    if "var(" in value and prop in ("margin", "padding", "border", "border-top", "border-right", "border-bottom",
                                    "border-left", "background", "font", "flex", "border-width", "border-color",
                                    "border-style", "inset", "gap", "border-radius", "list-style", "outline",
                                    "grid-template", "grid", "grid-area", "grid-row", "grid-column",
                                    "place-items", "place-self", "place-content", "flex-flow", "columns",
                                    "border-image", "animation", "transition", "mask", "-webkit-mask",
                                    "column-rule"):
        # Can't expand until variables are substituted; the style engine
        # re-expands after substitution.
        return [(prop, value)]
    lv = value.lower()
    if prop in ("margin", "padding"):
        vals = _four(split_values(value))
        return [("%s-%s" % (prop, s), v) for s, v in zip(SIDES, vals)]
    if prop == "inset":
        vals = _four(split_values(value))
        return list(zip(SIDES, vals))
    if prop == "border-width":
        return [("border-%s-width" % s, v) for s, v in zip(SIDES, _four(split_values(value)))]
    if prop == "border-style":
        return [("border-%s-style" % s, v) for s, v in zip(SIDES, _four(split_values(value)))]
    if prop == "border-color":
        return [("border-%s-color" % s, v) for s, v in zip(SIDES, _four(split_values(value)))]
    if prop == "border":
        w, st, c = _border_parts(value)
        out = []
        for s in SIDES:
            out += [("border-%s-width" % s, w), ("border-%s-style" % s, st), ("border-%s-color" % s, c)]
        return out
    if prop in ("border-top", "border-right", "border-bottom", "border-left"):
        w, st, c = _border_parts(value)
        return [(prop + "-width", w), (prop + "-style", st), (prop + "-color", c)]
    if prop in ("border-block", "border-inline"):
        w, st, c = _border_parts(value)
        sides = ("top", "bottom") if prop == "border-block" else ("left", "right")
        out = []
        for s in sides:
            out += [("border-%s-width" % s, w), ("border-%s-style" % s, st), ("border-%s-color" % s, c)]
        return out
    if prop in ("margin-block", "padding-block", "margin-inline", "padding-inline"):
        base, axis = prop.split("-")
        vals = split_values(value)
        a, b = vals[0], vals[-1]
        sides = ("top", "bottom") if axis == "block" else ("left", "right")
        return [("%s-%s" % (base, sides[0]), a), ("%s-%s" % (base, sides[1]), b)]
    if prop in ("margin-inline-start", "padding-inline-start"):
        return [(prop.replace("inline-start", "left"), value)]
    if prop in ("margin-inline-end", "padding-inline-end"):
        return [(prop.replace("inline-end", "right"), value)]
    if prop in ("margin-block-start", "padding-block-start"):
        return [(prop.replace("block-start", "top"), value)]
    if prop in ("margin-block-end", "padding-block-end"):
        return [(prop.replace("block-end", "bottom"), value)]
    if prop == "background":
        return _expand_background(value)
    if prop in ("mask", "-webkit-mask"):
        # same grammar as a background layer: image, position / size, repeat (no colour)
        bg = dict(_expand_background(value))
        image = bg.get("background-image", "none")
        return [("mask-image", image), ("-webkit-mask-image", image),
                ("mask-position", bg.get("background-position", "0% 0%")),
                ("mask-size", bg.get("background-size", "auto")),
                ("mask-repeat", bg.get("background-repeat", "repeat"))]
    if prop == "border-image":
        return _expand_border_image(value)
    if prop == "font":
        return _expand_font(value)
    if prop == "list-style":
        out = []
        for v in split_values(lv):
            if v in ("inside", "outside"):
                out.append(("list-style-position", v))
            elif not v.startswith("url("):
                out.append(("list-style-type", v))
        return out or [("list-style-type", "disc")]
    if prop == "flex":
        vals = split_values(lv)
        if vals == ["none"]:
            return [("flex-grow", "0"), ("flex-shrink", "0"), ("flex-basis", "auto")]
        if vals == ["auto"]:
            return [("flex-grow", "1"), ("flex-shrink", "1"), ("flex-basis", "auto")]
        grow, shrink, basis = "1", "1", "0%"
        nums = [v for v in vals if re.match(r"^\d*\.?\d+$", v)]
        others = [v for v in vals if v not in nums]
        if nums:
            grow = nums[0]
        if len(nums) > 1:
            shrink = nums[1]
        if others:
            basis = others[0]
        elif len(nums) > 2:
            basis = nums[2]
        return [("flex-grow", grow), ("flex-shrink", shrink), ("flex-basis", basis)]
    if prop == "flex-flow":
        out = []
        for v in split_values(lv):
            if "wrap" in v:
                out.append(("flex-wrap", v))
            else:
                out.append(("flex-direction", v))
        return out
    if prop == "gap" or prop == "grid-gap":
        vals = split_values(value)
        return [("row-gap", vals[0]), ("column-gap", vals[-1])]
    if prop in _WEBKIT_BOX:
        name, mapping = _WEBKIT_BOX[prop]
        return [(name, mapping.get(lv.strip(), lv.strip()) if mapping else value)]
    if prop.startswith(("-webkit-column", "-moz-column")):
        return expand_shorthand(prop.split("-", 2)[2], value)
    if prop == "columns":
        width, count = "auto", "auto"
        for v in split_values(lv):
            if re.match(r"^\d+$", v):
                count = v
            elif v != "auto":
                width = v
        return [("column-width", width), ("column-count", count)]
    if prop in LOGICAL_SIZES:
        return [(LOGICAL_SIZES[prop], value)]
    if prop in ("inset-inline", "inset-block"):
        vals = split_values(value)
        sides = ("left", "right") if prop == "inset-inline" else ("top", "bottom")
        return [(sides[0], vals[0]), (sides[1], vals[-1])]
    m = re.match(r"^(border|inset)-(inline|block)-(start|end)(-.*)?$", prop)
    if m:
        side = {("inline", "start"): "left", ("inline", "end"): "right",
                ("block", "start"): "top", ("block", "end"): "bottom"}[(m.group(2), m.group(3))]
        if m.group(1) == "inset":
            return [(side, value)]
        return expand_shorthand("border-%s%s" % (side, m.group(4) or ""), value)
    m = re.match(r"^border-(start|end)-(start|end)-radius$", prop)
    if m:
        return [("border-%s-%s-radius" % ({"start": "top", "end": "bottom"}[m.group(1)],
                                          {"start": "left", "end": "right"}[m.group(2)]), value)]
    if prop in ("animation", "-webkit-animation"):
        return _expand_animation(value)
    if prop in ("transition", "-webkit-transition"):
        return _expand_transition(value)
    if prop.startswith("-webkit-animation-") or prop.startswith("-webkit-transition-"):
        return [(prop[8:], value)]
    if prop == "container":
        name, _, kind = value.partition("/")
        return [("container-name", name.strip() or "none"), ("container-type", kind.strip() or "normal")]
    if prop == "outline":
        w, st, c = _border_parts(value)
        if lv.strip() in ("0", "none"):
            st = "none"
        return [("outline-width", w), ("outline-style", st), ("outline-color", c)]
    if prop == "column-rule":
        w, st, c = _border_parts(value)
        return [("column-rule-width", w), ("column-rule-style", st), ("column-rule-color", c)]
    if prop == "grid-template":
        return _expand_grid_template(value)
    if prop == "grid":
        return _expand_grid(value)
    if prop == "grid-area":
        vals = [v.strip() for v in _split_top_level(value, "/")]
        rs = vals[0]
        cs = vals[1] if len(vals) > 1 else _grid_default_end(rs)
        rend = vals[2] if len(vals) > 2 else _grid_default_end(rs)
        cend = vals[3] if len(vals) > 3 else _grid_default_end(cs)
        return [("grid-row-start", rs), ("grid-column-start", cs), ("grid-row-end", rend), ("grid-column-end", cend)]
    if prop in ("grid-row", "grid-column"):
        vals = [v.strip() for v in _split_top_level(value, "/")]
        end = vals[1] if len(vals) > 1 else _grid_default_end(vals[0])
        return [(prop + "-start", vals[0]), (prop + "-end", end)]
    if prop == "place-self":
        vals = split_values(lv)
        return [("align-self", vals[0]), ("justify-self", vals[-1])]
    if prop == "place-items":
        vals = split_values(lv)
        return [("align-items", vals[0]), ("justify-items", vals[-1])]
    if prop == "place-content":
        vals = split_values(lv)
        return [("align-content", vals[0]), ("justify-content", vals[-1])]
    if prop in ("text-decoration", "text-decoration-line"):
        lines, style_, color, thick = [], None, None, None
        for v in split_values(value):
            lv2 = v.lower()
            if lv2 in ("underline", "line-through", "overline", "blink", "none", "spelling-error", "grammar-error"):
                lines.append(lv2)
            elif lv2 in ("solid", "double", "dotted", "dashed", "wavy"):
                style_ = lv2
            elif _looks_like_length(lv2) or lv2 in ("auto", "from-font"):
                thick = lv2
            else:
                color = v
        line = " ".join(l for l in lines if l != "none") or "none"
        out = [("text-decoration", line), ("text-decoration-line", line)]
        if prop == "text-decoration":
            out += [("text-decoration-style", style_ or "solid"), ("text-decoration-color", color or "currentcolor"),
                    ("text-decoration-thickness", thick or "auto")]
        return out
    if prop == "text-wrap":
        lv2 = lv.strip()
        if lv2 in ("nowrap", "wrap"):
            return [("text-wrap-mode", lv2)] + ([("white-space", "nowrap")] if lv2 == "nowrap" else [])
        return [("text-wrap-style", lv2), ("text-wrap-mode", "wrap")]
    if prop == "overflow":
        vals = split_values(lv)
        return [("overflow", vals[0]), ("overflow-x", vals[0]), ("overflow-y", vals[-1])]
    return [(prop, value)]


LOGICAL_SIZES = {"inline-size": "width", "block-size": "height", "min-inline-size": "min-width",
                 "max-inline-size": "max-width", "min-block-size": "min-height", "max-block-size": "max-height"}


_REPEATS = ("repeat", "no-repeat", "repeat-x", "repeat-y", "space", "round")
_BOXES = ("border-box", "padding-box", "content-box", "text")
_COLOR_FUNCS = ("rgb(", "rgba(", "hsl(", "hsla(", "hwb(", "lab(", "lch(", "oklab(", "oklch(", "color(",
                "color-mix(", "light-dark(")


def _expand_background(value):
    """All background longhands, one comma-separated item per layer
    (the colour comes from the last layer)."""
    if value.strip().lower() in ("none", "initial", "unset", "inherit"):
        v = value.strip().lower()
        return [("background-color", "transparent" if v == "none" else v), ("background-image", "none")]
    images, positions, sizes, repeats, origins, clips, attachments = [], [], [], [], [], [], []
    color = None
    layers = _split_top_level(value, ",")
    for li, layer in enumerate(layers):
        image, rep, att, boxes, pos, size = "none", [], "scroll", [], [], []
        target = pos
        for v in split_values(layer):
            lv2 = v.lower()
            if lv2 == "/":
                target = size
                continue
            if "/" in lv2 and not lv2.startswith(_COLOR_FUNCS + ("url(",)) and "gradient(" not in lv2:
                a, _, b = lv2.partition("/")
                if a:
                    pos.append(a)
                target = size
                if b:
                    size.append(b)
                continue
            if lv2.startswith("url(") or "gradient(" in lv2 or lv2.startswith("image-set("):
                image = v
            elif lv2 in _REPEATS:
                rep.append(lv2)
            elif lv2 in ("fixed", "scroll", "local"):
                att = lv2
            elif lv2 in _BOXES:
                boxes.append(lv2)
            elif lv2 in ("center", "top", "bottom", "left", "right", "cover", "contain", "auto") or \
                    _looks_like_length(lv2):
                target.append(v)
            elif lv2 in ("none",):
                image = "none"
            elif lv2 not in ("initial", "inherit", "unset") and li == len(layers) - 1:
                color = v
        images.append(image)
        positions.append(" ".join(pos) or "0% 0%")
        sizes.append(" ".join(size) or "auto")
        repeats.append(" ".join(rep) or "repeat")
        origins.append(boxes[0] if boxes else "padding-box")
        clips.append(boxes[1] if len(boxes) > 1 else (boxes[0] if boxes else "border-box"))
        attachments.append(att)
    return [("background-color", color or "transparent"), ("background-image", ", ".join(images)),
            ("background-position", ", ".join(positions)), ("background-size", ", ".join(sizes)),
            ("background-repeat", ", ".join(repeats)), ("background-origin", ", ".join(origins)),
            ("background-clip", ", ".join(clips)), ("background-attachment", ", ".join(attachments))]


def _expand_border_image(value):
    if value.strip().lower() in ("none", "initial", "unset", "inherit"):
        return [("border-image-source", "none")]
    source, groups, rep = "none", [[], [], []], []
    g = 0
    for v in split_values(value):
        lv2 = v.lower()
        if lv2.startswith("url(") or "gradient(" in lv2:
            source = v
        elif lv2 in ("stretch", "repeat", "round", "space"):
            rep.append(lv2)
        elif lv2 == "/":
            g = min(2, g + 1)
        elif "/" in lv2:
            parts = lv2.split("/")
            for k, part in enumerate(parts):
                if part:
                    groups[min(2, g + k)].append(part)
            g = min(2, g + len(parts) - 1)
        else:
            groups[g].append(lv2)
    out = [("border-image-source", source), ("border-image-repeat", " ".join(rep) or "stretch")]
    out.append(("border-image-slice", " ".join(groups[0]) or "100%"))
    if groups[1]:
        out.append(("border-image-width", " ".join(groups[1])))
    if groups[2]:
        out.append(("border-image-outset", " ".join(groups[2])))
    return out


# The 2009 flexbox draft (display: -webkit-box) mapped onto flexbox.
_WEBKIT_BOX = {
    "-webkit-box-flex": ("flex-grow", None),
    "-webkit-box-ordinal-group": ("order", None),
    "-webkit-box-pack": ("justify-content", {"start": "flex-start", "end": "flex-end", "justify": "space-between"}),
    "-webkit-box-align": ("align-items", {"start": "flex-start", "end": "flex-end"}),
}


_TIMING = ("ease", "linear", "ease-in", "ease-out", "ease-in-out", "step-start", "step-end")


def _is_time(v):
    return bool(re.match(r"^-?\d*\.?\d+(ms|s)$", v))


def _expand_animation(value):
    cols = {k: [] for k in ("name", "duration", "timing-function", "delay", "iteration-count", "direction",
                            "fill-mode", "play-state")}
    for item in _split_top_level(value, ","):
        got = {}
        for v in split_values(item.strip()):
            lv2 = v.lower()
            if _is_time(lv2):
                got["delay" if "duration" in got else "duration"] = lv2
            elif lv2 in _TIMING or lv2.startswith(("cubic-bezier(", "steps(", "linear(")):
                got["timing-function"] = lv2
            elif lv2 == "infinite" or re.match(r"^\d*\.?\d+$", lv2):
                got["iteration-count"] = lv2
            elif lv2 in ("normal", "reverse", "alternate", "alternate-reverse") and "direction" not in got:
                got["direction"] = lv2
            elif lv2 in ("forwards", "backwards", "both") or (lv2 == "none" and "name" in got):
                got["fill-mode"] = lv2
            elif lv2 in ("running", "paused"):
                got["play-state"] = lv2
            else:
                got["name"] = v.strip("\"'")
        defaults = {"name": "none", "duration": "0s", "timing-function": "ease", "delay": "0s",
                    "iteration-count": "1", "direction": "normal", "fill-mode": "none", "play-state": "running"}
        for k in cols:
            cols[k].append(got.get(k, defaults[k]))
    return [("animation-" + k, ", ".join(v)) for k, v in cols.items()]


def _expand_transition(value):
    cols = {k: [] for k in ("property", "duration", "timing-function", "delay")}
    for item in _split_top_level(value, ","):
        got = {}
        for v in split_values(item.strip()):
            lv2 = v.lower()
            if _is_time(lv2):
                got["delay" if "duration" in got else "duration"] = lv2
            elif lv2 in _TIMING or lv2.startswith(("cubic-bezier(", "steps(", "linear(")):
                got["timing-function"] = lv2
            elif lv2 == "allow-discrete":
                continue
            else:
                got["property"] = lv2
        defaults = {"property": "all", "duration": "0s", "timing-function": "ease", "delay": "0s"}
        for k in cols:
            cols[k].append(got.get(k, defaults[k]))
    return [("transition-" + k, ", ".join(v)) for k, v in cols.items()]


def _grid_default_end(start):
    """`grid-row: foo` means `foo / foo`; a number or a span means `x / auto`."""
    v = start.strip().lower()
    if re.match(r"^-?[a-z_][\w-]*$", v) and v not in ("auto", "span"):
        return start.strip()
    return "auto"


def _expand_grid_template(value):
    v = value.strip()
    if v.lower() in ("none", "initial", "unset", "inherit"):
        return [("grid-template-rows", "none"), ("grid-template-columns", "none"), ("grid-template-areas", "none")]
    parts = _split_top_level(v, "/")
    rows = parts[0].strip()
    cols = parts[1].strip() if len(parts) > 1 else "none"
    if '"' not in rows and "'" not in rows:
        return [("grid-template-rows", rows), ("grid-template-columns", cols), ("grid-template-areas", "none")]
    # [names]? "string" size? [names]? ...: the strings are the areas, the sizes the rows.
    areas, sizes = [], []
    for tok in re.findall(r"\"[^\"]*\"|'[^']*'|\[[^\]]*\]|[^\s\[\]\"'(]+(?:\([^)]*\))?", rows):
        if tok[0] in "\"'":
            areas.append('"%s"' % tok[1:-1])
            sizes.append("auto")
        elif tok.startswith("["):
            sizes.append(tok)
        else:
            for k in range(len(sizes) - 1, -1, -1):   # the size of the latest row
                if not sizes[k].startswith("["):
                    sizes[k] = tok
                    break
    return [("grid-template-rows", " ".join(sizes) or "none"), ("grid-template-columns", cols),
            ("grid-template-areas", " ".join(areas) or "none")]


def _expand_grid(value):
    v = value.strip()
    if "auto-flow" not in v.lower():
        return _expand_grid_template(v) + [("grid-auto-flow", "row"), ("grid-auto-rows", "auto"),
                                           ("grid-auto-columns", "auto")]
    parts = [x.strip() for x in _split_top_level(v, "/")]
    left, right = parts[0], (parts[1] if len(parts) > 1 else "none")
    if "auto-flow" in left.lower():
        dense = " dense" if "dense" in left.lower() else ""
        size = re.sub(r"(?i)auto-flow|dense", "", left).strip() or "auto"
        return [("grid-auto-flow", "row" + dense), ("grid-auto-rows", size), ("grid-template-rows", "none"),
                ("grid-template-columns", right), ("grid-template-areas", "none")]
    dense = " dense" if "dense" in right.lower() else ""
    size = re.sub(r"(?i)auto-flow|dense", "", right).strip() or "auto"
    return [("grid-auto-flow", "column" + dense), ("grid-auto-columns", size), ("grid-template-columns", "none"),
            ("grid-template-rows", left), ("grid-template-areas", "none")]


FONT_WEIGHTS = {"normal", "bold", "bolder", "lighter", "100", "200", "300", "400", "500", "600", "700", "800", "900"}
FONT_SIZES = {"xx-small", "x-small", "small", "medium", "large", "x-large", "xx-large", "xxx-large", "smaller", "larger"}


def _expand_font(value):
    lv = value.lower().strip()
    if lv in ("caption", "icon", "menu", "message-box", "small-caption", "status-bar", "inherit", "initial"):
        return [("font-family", "sans-serif")]
    vals = split_values(value)
    style, weight, size, line_height = "normal", "normal", None, None
    i = 0
    while i < len(vals):
        v = vals[i].lower()
        if v in ("italic", "oblique"):
            style = v
        elif v in FONT_WEIGHTS:
            weight = v
        elif v in ("normal", "small-caps"):
            pass
        elif v in FONT_SIZES or re.match(r"^[\d.]", v) or v.startswith(("calc(", "clamp(", "var(", "min(", "max(")):
            if "/" in v:
                size, line_height = v.split("/", 1)
            else:
                size = v
                if i + 1 < len(vals) and vals[i + 1].startswith("/"):
                    lh = vals[i + 1][1:]
                    if not lh and i + 2 < len(vals):
                        lh = vals[i + 2]
                        i += 1
                    line_height = lh
                    i += 1
            i += 1
            break
        i += 1
    family = " ".join(vals[i:]) or "serif"
    out = [("font-style", style), ("font-weight", weight), ("font-family", family)]
    if size:
        out.append(("font-size", size))
    out.append(("line-height", line_height or "normal"))
    return out


def parse_stylesheet(text, viewport_width=1000, order_start=0, layers=None):
    p = StyleSheetParser(text, viewport_width, order_start, layers)
    rules = p.parse()
    return rules, p.imports


def parse_stylesheet_full(text, viewport_width=1000, order_start=0, layers=None):
    """Like parse_stylesheet, but returns the parser too (keyframes,
    counter styles and registered custom properties)."""
    p = StyleSheetParser(text, viewport_width, order_start, layers)
    rules = p.parse()
    return rules, p
