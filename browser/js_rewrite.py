"""Source rewrites that work around bugs in the bundled QuickJS.

QuickJS (2021 release, used by the `quickjs` package) refuses to compile a
generator where a `yield` sits inside a larger expression within a
try { ... } finally { ... } block, e.g.

    function* () { try { e(yield d(x)) } finally { d() } }

fails with "InternalError: unconsistent stack size". Google's main script
has two of these, and because the whole 1 MB script fails to compile, none of
its click handlers ever exist. We rewrite the try body into an inner
generator that the outer one delegates to:

    function* () { try { yield* (function* () { e(yield d(x)) }).call(this) } finally { d() } }

which compiles, and behaves the same: values, next(v), throw() and return()
all pass through yield*, and the finally block still runs.
"""
import re

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<str>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<tmpl>`)
  | (?P<name>[A-Za-z_$\u0080-￿][\w$\u0080-￿]*)
  | (?P<num>\.?\d[\w.]*)
  | (?P<punct>=>|\.\.\.|\?\.|[{}()\[\];,<>+\-*%&|^!~?:=@#.])
  | (?P<slash>/)
""", re.S | re.X)

# After these, a "/" starts a regular expression rather than dividing.
_REGEX_AFTER_WORDS = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case",
                      "do", "else", "yield", "await"}
_REGEX_RE = re.compile(r"/(?:[^/\\\[\n]|\\.|\[(?:[^\]\\\n]|\\.)*\])+/[A-Za-z]*")


def tokenize(src):
    """List of (kind, text, start, end) for the significant tokens of `src`.
    Strings, template literals and regexes are single tokens."""
    out = []
    i, n = 0, len(src)
    prev = None
    while i < n:
        m = _TOKEN.match(src, i)
        if m is None:
            i += 1          # unknown character: skip it
            continue
        kind = m.lastgroup
        if kind in ("ws", "comment"):
            i = m.end()
            continue
        if kind == "tmpl":
            j = _skip_template(src, i)
            out.append(("str", src[i:j], i, j))
            prev, i = out[-1], j
            continue
        if kind == "slash":
            regex_ok = prev is None or (prev[0] == "punct" and prev[1] not in (")", "]", "}")) or \
                (prev[0] == "name" and prev[1] in _REGEX_AFTER_WORDS)
            if regex_ok:
                r = _REGEX_RE.match(src, i)
                if r:
                    out.append(("str", r.group(), i, r.end()))
                    prev, i = out[-1], r.end()
                    continue
            if src.startswith("/=", i):
                out.append(("punct", "/=", i, i + 2))
                prev, i = out[-1], i + 2
                continue
            out.append(("punct", "/", i, i + 1))
            prev, i = out[-1], i + 1
            continue
        out.append((kind, m.group(), i, m.end()))
        prev, i = out[-1], m.end()
    return out


def _skip_template(src, i):
    """Index just past the template literal starting at src[i] == '`'."""
    i += 1
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return i + 1
        elif c == "$" and src.startswith("${", i):
            i += 2
            depth = 1
            while i < n and depth:
                m = _TOKEN.match(src, i)
                if m is None:
                    i += 1
                    continue
                if m.lastgroup == "tmpl":
                    i = _skip_template(src, i)
                    continue
                t = m.group()
                if t == "{":
                    depth += 1
                elif t == "}":
                    depth -= 1
                i = m.end()
        else:
            i += 1
    return n


def _match_braces(toks):
    """Map index of each '{' / '(' / '[' token to its partner."""
    pairs, stack = {}, []
    opens = {"{": "}", "(": ")", "[": "]"}
    for k, t in enumerate(toks):
        if t[0] != "punct":
            continue
        if t[1] in opens:
            stack.append(k)
        elif t[1] in ("}", ")", "]") and stack:
            pairs[stack.pop()] = k
    return pairs


def _function_bodies(toks, pairs):
    """(start, end, is_generator) token ranges of function bodies; arrow bodies
    in braces count as non-generators."""
    out = []
    for k, t in enumerate(toks):
        if t[:2] == ("name", "function"):
            j = k + 1
            gen = j < len(toks) and toks[j][1] == "*"
            if gen:
                j += 1
            if j < len(toks) and toks[j][0] == "name":
                j += 1
            if j < len(toks) and toks[j][1] == "(" and j in pairs:
                b = pairs[j] + 1
                if b < len(toks) and toks[b][1] == "{" and b in pairs:
                    out.append((b, pairs[b], gen))
        elif t[1] == "=>" and k + 1 < len(toks) and toks[k + 1][1] == "{" and k + 1 in pairs:
            out.append((k + 1, pairs[k + 1], False))
        elif t[1] == "*" and k + 1 < len(toks) and toks[k + 1][0] == "name" and k + 2 < len(toks) \
                and toks[k + 2][1] == "(" and k > 0 and toks[k - 1][1] in ("{", "}", ";", ",") :
            # generator method in a class / object literal:  *name(...) { ... }
            j = k + 2
            if j in pairs:
                b = pairs[j] + 1
                if b < len(toks) and toks[b][1] == "{" and b in pairs:
                    out.append((b, pairs[b], True))
    return out


_UNSAFE = {"return", "break", "continue", "arguments", "super"}


def fix_generator_finally(src):
    """Rewrite try/finally bodies in generators that QuickJS cannot compile
    (see the module docstring). Returns `src` unchanged when nothing applies."""
    if "finally" not in src or "yield" not in src:
        return src
    toks = tokenize(src)
    pairs = _match_braces(toks)
    bodies = _function_bodies(toks, pairs)
    # owner[k] = the innermost function body containing token k
    owner = [None] * len(toks)
    for b in sorted(bodies, key=lambda r: r[0]):
        for k in range(b[0] + 1, b[1]):
            owner[k] = b
    edits = []
    for k, t in enumerate(toks):
        if t[:2] != ("name", "try") or k + 1 >= len(toks) or toks[k + 1][1] != "{" or k + 1 not in pairs:
            continue
        fn = owner[k]
        if fn is None or not fn[2]:
            continue
        open_i, close_i = k + 1, pairs[k + 1]
        after = close_i + 1
        if after < len(toks) and toks[after][1] == "catch":
            j = after + 1
            if j < len(toks) and toks[j][1] == "(" and j in pairs:
                j = pairs[j] + 1
            if j < len(toks) and toks[j][1] == "{" and j in pairs:
                after = pairs[j] + 1
        if after >= len(toks) or toks[after][1] != "finally":
            continue
        inner = range(open_i + 1, close_i)
        mine = [i for i in inner if owner[i] is fn]
        if not any(toks[i][1] == "yield" for i in mine):
            continue
        # only a yield inside an expression trips the bug: skip plain `yield x;` statements
        if all(toks[i - 1][1] in ("{", "}", ";") for i in mine if toks[i][1] == "yield"):
            continue
        if any(toks[i][0] == "name" and toks[i][1] in _UNSAFE for i in mine):
            continue
        # `var` names declared in the body must not be used outside it
        declared = {toks[i + 1][1] for i in mine if toks[i][1] == "var" and i + 1 < len(toks)
                    and toks[i + 1][0] == "name"}
        if declared:
            outside = {toks[i][1] for i in range(fn[0] + 1, fn[1]) if not (open_i < i < close_i)
                       and toks[i][0] == "name"}
            if declared & outside:
                continue
        edits.append((toks[open_i][3], toks[close_i][2]))
    if not edits:
        return src
    # innermost first doesn't matter: the bodies we rewrite don't overlap in practice,
    # but apply from the end so earlier offsets stay valid; skip nested overlaps.
    edits.sort()
    kept = []
    for e in edits:
        if kept and e[0] < kept[-1][1]:
            continue
        kept.append(e)
    for a, b in reversed(kept):
        src = src[:a] + "yield* (function*(){" + src[a:b] + "\n}).call(this);" + src[b:]
    return src
