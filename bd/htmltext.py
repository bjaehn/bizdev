"""A small, forgiving HTML reader: a tree, the visible text and the links. Standard library only; page content is treated as data.

The visible text of a page and of any element inside it are built the same way (text nodes joined by single spaces), so the text of a listing
entry is always a contiguous piece of the page's text. That is what lets code check that an excerpt really is on the page."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from bd.urls import safe_urljoin

VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
SKIP_TEXT = frozenset({"script", "style", "noscript", "template", "svg", "head", "iframe"})
_WS = re.compile(r"\s+")


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)
    parent: "Node | None" = None

    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def walk(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.walk()

    def elements(self) -> list["Node"]:
        return [c for c in self.children if isinstance(c, Node)]


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("[document]")
        self.cur = self.root
        self.json_ld: list[str] = []
        self.title = ""
        self._in_title = False
        self._ld = False
        self._ld_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._ld, self._ld_buf = True, []
        n = Node(tag, a, parent=self.cur)
        self.cur.children.append(n)
        if tag == "title":
            self._in_title = True
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        n = Node(tag, {k.lower(): (v or "") for k, v in attrs}, parent=self.cur)
        self.cur.children.append(n)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in VOID:
            return
        if tag == "script" and self._ld:
            self.json_ld.append("".join(self._ld_buf))
            self._ld = False
        if tag == "title":
            self._in_title = False
        n = self.cur
        while n is not self.root and n.tag != tag:      # close up to the matching open tag; an end tag with no open tag is ignored
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        if self._ld:
            self._ld_buf.append(data)
        if self._in_title:
            self.title += data
        if data.strip():
            self.cur.children.append(data)


@dataclass
class Page:
    root: Node
    title: str
    json_ld: list[str]


def parse_html(html: str) -> Page:
    b = _Builder()
    b.feed(html)
    b.close()
    return Page(b.root, _WS.sub(" ", b.title).strip(), b.json_ld)


def _texts(node: Node):
    for c in node.children:
        if isinstance(c, str):
            yield c
        elif c.tag not in SKIP_TEXT:
            yield from _texts(c)


def node_text(node: Node) -> str:
    """The visible text under a node: text nodes joined by single spaces, whitespace collapsed."""
    if node.tag in SKIP_TEXT:
        return ""
    return _WS.sub(" ", " ".join(_texts(node))).strip()


def page_text(page: Page) -> str:
    return node_text(page.root)


def links(node: Node, base_url: str) -> list[tuple[str, str, Node]]:
    """(absolute href, anchor text, the anchor) for every link under a node; fragments, mailto, tel and javascript links are left out."""
    out = []
    for n in node.walk():
        if n.tag == "a" and n.attrs.get("href"):
            h = n.attrs["href"].strip()
            if h.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            text = node_text(n) or n.attrs.get("aria-label", "") or n.attrs.get("title", "")
            if not text:
                img = next((i for i in n.walk() if i.tag == "img" and i.attrs.get("alt")), None)
                text = img.attrs["alt"] if img else ""
            href = safe_urljoin(base_url, h)
            if href:                                                                       # a link that is not a URL (a template placeholder) is left out
                out.append((href, _WS.sub(" ", text).strip(), n))
    return out


def ld_objects(page: Page) -> list[dict]:
    """Parsed JSON-LD objects (a @graph is flattened). Unparseable blocks are skipped."""
    out: list[dict] = []

    def add(o):
        if isinstance(o, list):
            for i in o:
                add(i)
        elif isinstance(o, dict):
            if "@graph" in o:
                add(o["@graph"])
            out.append(o)

    for raw in page.json_ld:
        try:
            add(json.loads(raw))
        except ValueError:
            continue
    return out
