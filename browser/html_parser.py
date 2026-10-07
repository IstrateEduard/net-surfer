"""HTML tokenizer and tree builder.

Turns HTML source into a tree of dom.Element / dom.Text nodes. It is a
forgiving, hand-written parser modelled loosely on the HTML5 algorithm:
implied <html>/<head>/<body>, void elements, raw-text elements
(<script>, <style>), implied end tags for <p>, <li>, table parts, and
recovery from mismatched end tags.
"""
import html
import re

from .dom import Comment, Element, Text

VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr", "keygen",
}
RAW_TEXT_ELEMENTS = {"script", "style", "textarea", "title", "xmp", "iframe", "noembed"}
HEAD_ELEMENTS = {"base", "basefont", "bgsound", "link", "meta", "title", "style", "script", "noscript"}

# Start tags that implicitly close an open <p>.
CLOSES_P = {
    "address", "article", "aside", "blockquote", "center", "details", "dialog", "dir", "div", "dl",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hgroup", "hr", "main", "menu", "nav", "ol", "p", "pre", "section", "table", "ul",
    "li", "dd", "dt",
}

# tag -> (set of tags it closes if open, set of tags that act as a boundary for the search)
IMPLIED_END = {
    "li": ({"li"}, {"ul", "ol", "menu"}),
    "dt": ({"dt", "dd"}, {"dl"}),
    "dd": ({"dt", "dd"}, {"dl"}),
    "option": ({"option"}, {"select", "datalist"}),
    "optgroup": ({"option", "optgroup"}, {"select"}),
    "tr": ({"tr"}, {"table", "tbody", "thead", "tfoot"}),
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
    "thead": ({"tbody", "thead", "tfoot"}, {"table"}),
    "tbody": ({"tbody", "thead", "tfoot"}, {"table"}),
    "tfoot": ({"tbody", "thead", "tfoot"}, {"table"}),
}

ATTR_RE = re.compile(
    r"""([^\s"'>/=]+)          # name
        (?:\s*=\s*
          (?:"([^"]*)"         # double-quoted
            |'([^']*)'         # single-quoted
            |([^\s>]+)         # unquoted
          )
        )?""",
    re.VERBOSE,
)
TAG_NAME_RE = re.compile(r"[A-Za-z][^\s/>]*")


def tokenize(source):
    """Yield ('text', str) | ('start', tag, attrs, selfclosing) | ('end', tag) | ('comment', str)."""
    i = 0
    n = len(source)
    raw_end = None  # when inside <script> etc., the tag we're waiting to close
    while i < n:
        if raw_end is not None:
            m = re.compile(r"</%s\s*>" % re.escape(raw_end), re.I).search(source, i)
            end = m.start() if m else n
            if end > i:
                yield ("text", source[i:end])
            if m:
                yield ("end", raw_end)
                i = m.end()
            else:
                i = n
            raw_end = None
            continue

        lt = source.find("<", i)
        if lt == -1:
            yield ("text", source[i:])
            break
        if lt > i:
            yield ("text", source[i:lt])
        i = lt

        if source.startswith("<!--", i):
            end = source.find("-->", i + 4)
            if end == -1:
                end = n
            yield ("comment", source[i + 4:end])
            i = end + 3
        elif source.startswith("<!", i) or source.startswith("<?", i):
            end = source.find(">", i)
            i = n if end == -1 else end + 1  # doctype / processing instruction: ignored
        elif source.startswith("</", i):
            m = TAG_NAME_RE.match(source, i + 2)
            end = source.find(">", i)
            if end == -1:
                break
            if m:
                yield ("end", m.group(0).lower())
            i = end + 1
        else:
            m = TAG_NAME_RE.match(source, i + 1)
            if not m:
                yield ("text", "<")
                i += 1
                continue
            tag = m.group(0).lower()
            j = m.end()
            # Find end of tag, respecting quotes in attribute values.
            quote = None
            k = j
            while k < n:
                c = source[k]
                if quote:
                    if c == quote:
                        quote = None
                elif c in "\"'":
                    # only a quote if preceded by '=' (possibly with spaces)
                    back = source[j:k].rstrip()
                    if back.endswith("="):
                        quote = c
                elif c == ">":
                    break
                k += 1
            attr_text = source[j:k]
            self_closing = attr_text.rstrip().endswith("/")
            attrs = {}
            for am in ATTR_RE.finditer(attr_text):
                name = am.group(1).lower()
                if name == "/":
                    continue
                value = am.group(2)
                if value is None:
                    value = am.group(3)
                if value is None:
                    value = am.group(4)
                if value is None:
                    value = ""
                if name not in attrs:
                    attrs[name] = html.unescape(value)
            yield ("start", tag, attrs, self_closing)
            i = k + 1
            if tag in RAW_TEXT_ELEMENTS and not self_closing:
                raw_end = tag


class HTMLParser:
    def __init__(self, source):
        self.source = source

    def parse(self):
        self.root = Element("html")
        self.head = None
        self.body = None
        self.stack = [self.root]
        raw_parent = None
        for token in tokenize(self.source):
            kind = token[0]
            if kind == "text":
                text = token[1]
                if self.stack[-1].tag in RAW_TEXT_ELEMENTS:
                    self.add_text(text, raw=True)
                else:
                    self.add_text(html.unescape(text))
            elif kind == "start":
                self.start_tag(token[1], token[2], token[3])
            elif kind == "end":
                self.end_tag(token[1])
            elif kind == "comment":
                parent = self.stack[-1]
                parent.children.append(Comment(token[1], parent))
        self.ensure_body()
        return self.root

    # --- helpers -----------------------------------------------------
    def ensure_head(self):
        if self.head is None:
            self.head = Element("head", {}, self.root)
            self.root.children.insert(0, self.head)

    def ensure_body(self):
        if self.body is None:
            self.ensure_head()
            # close anything left in head
            while len(self.stack) > 1:
                self.stack.pop()
            self.body = Element("body", {}, self.root)
            self.root.children.append(self.body)
            self.stack.append(self.body)

    def open_tags(self):
        return [e.tag for e in self.stack]

    def add_text(self, text, raw=False):
        if not raw and text.isspace() and self.body is None:
            return  # whitespace before <body> is dropped
        if self.body is None and self.stack[-1].tag not in RAW_TEXT_ELEMENTS:
            self.ensure_body()
        parent = self.stack[-1]
        if parent.children and isinstance(parent.children[-1], Text):
            parent.children[-1].text += text
        else:
            parent.children.append(Text(text, parent))

    def close_until(self, tags, boundary):
        """Pop the stack up to (and including) the nearest element in `tags`,
        unless an element in `boundary` is found first."""
        for idx in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[idx].tag
            if tag in tags:
                del self.stack[idx:]
                return True
            if tag in boundary:
                return False
        return False

    def start_tag(self, tag, attrs, self_closing):
        if tag == "html":
            for k, v in attrs.items():
                self.root.attributes.setdefault(k, v)
            return
        if tag == "head":
            self.ensure_head()
            if self.body is None and self.stack[-1] is self.root:
                self.stack.append(self.head)
            return
        if tag == "body":
            if self.body is None:
                self.ensure_body()
            for k, v in attrs.items():
                self.body.attributes.setdefault(k, v)
            return

        if self.body is None:
            if tag in HEAD_ELEMENTS:
                self.ensure_head()
                if self.stack[-1] is not self.head:
                    self.stack = [self.root, self.head]
            else:
                self.ensure_body()

        if tag in CLOSES_P and "p" in self.open_tags():
            self.close_until({"p"}, {"button", "table", "td", "th", "li", "div"} - {tag})
        if tag in IMPLIED_END:
            closes, boundary = IMPLIED_END[tag]
            self.close_until(closes, boundary)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self.stack[-1].tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.stack.pop()
        if tag == "a" and "a" in self.open_tags():
            self.close_until({"a"}, set())
        if tag in ("td", "th") and self.stack[-1].tag in ("table", "tbody", "thead", "tfoot"):
            self.start_tag("tr", {}, False)
        if tag == "tr" and self.stack[-1].tag == "table":
            self.start_tag("tbody", {}, False)

        parent = self.stack[-1]
        node = Element(tag, attrs, parent)
        parent.children.append(node)
        # In SVG/MathML (foreign content) "<x/>" really is self-closing.
        foreign = self_closing and (tag in ("svg", "math") or any(e.tag in ("svg", "math") for e in self.stack))
        if tag not in VOID_ELEMENTS and not foreign:
            self.stack.append(node)

    def end_tag(self, tag):
        if tag in ("html", "body"):
            return  # keep body open for trailing content
        if tag == "head":
            if self.stack[-1] is self.head:
                self.stack.pop()
            return
        if tag == "p" and "p" not in self.open_tags():
            # </p> with no open <p> creates an empty paragraph (per spec)
            self.start_tag("p", {}, False)
        if tag == "br":
            self.start_tag("br", {}, True)
            return
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == tag:
                del self.stack[idx:]
                return
            if self.stack[idx].tag in ("table", "td", "th") and tag not in ("table", "td", "th", "tr", "tbody", "thead", "tfoot"):
                return  # don't let stray end tags escape a table cell
        # unmatched end tag: ignored


def parse(source):
    return HTMLParser(source).parse()
