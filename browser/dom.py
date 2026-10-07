"""Document tree: the browser's own DOM-like node classes."""


class Node:
    def __init__(self, parent=None):
        self.parent = parent
        self.children = []
        # Filled in by the style engine.
        self.style = {}

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def descendants(self):
        for child in self.children:
            yield child
            yield from child.descendants()


class Text(Node):
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.text = text

    def __repr__(self):
        return "Text(%r)" % self.text[:40]


class Comment(Node):
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.text = text


class Element(Node):
    def __init__(self, tag, attributes=None, parent=None):
        super().__init__(parent)
        self.tag = tag
        self.attributes = attributes or {}
        # Form state (value typed by the user, checked boxes).
        self.form_value = None
        self.checked = "checked" in self.attributes

    @property
    def id(self):
        return self.attributes.get("id")

    @property
    def classes(self):
        return self.attributes.get("class", "").split()

    def element_children(self):
        return [c for c in self.children if isinstance(c, Element)]

    def get_elements_by_tag(self, tag):
        return [n for n in self.descendants()
                if isinstance(n, Element) and n.tag == tag]

    def find_by_id(self, ident):
        for n in self.descendants():
            if isinstance(n, Element) and (n.attributes.get("id") == ident
                                           or (n.tag == "a" and n.attributes.get("name") == ident)):
                return n
        return None

    def text_content(self):
        out = []
        for n in self.descendants():
            if isinstance(n, Text):
                out.append(n.text)
        return "".join(out)

    def __repr__(self):
        attrs = "".join(" %s=%r" % kv for kv in list(self.attributes.items())[:3])
        return "<%s%s>" % (self.tag, attrs)


class PseudoElement(Element):
    """A ::before / ::after box generated from CSS `content`."""
    is_pseudo = True

    def __init__(self, which, parent):
        super().__init__("::" + which, {}, parent)
        self.pseudo_decls = []


def dump(node, indent=0, out=None):
    """Pretty-print a tree (used by the DOM inspector and tests)."""
    if out is None:
        out = []
    pad = "  " * indent
    if isinstance(node, Element):
        out.append(pad + repr(node))
    elif isinstance(node, Text):
        if node.text.strip():
            out.append(pad + repr(node.text.strip()[:60]))
    elif isinstance(node, Comment):
        pass
    for child in node.children:
        dump(child, indent + 1, out)
    return out
