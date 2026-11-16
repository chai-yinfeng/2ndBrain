"""Obsidian Markdown → safe, offline-friendly Web content.

Only Markdown-generated tags survive the final allowlist serializer. Math remains
escaped text until the local KaTeX frontend renders it with ``trust: false``.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as etree
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit

import markdown
from markdown.blockprocessors import BlockProcessor
from markdown.extensions import Extension
from markdown.extensions.toc import TocExtension, slugify_unicode
from markdown.inlinepatterns import InlineProcessor
from markdown.util import AtomicString

from secondbrain.wiki.store import PAGE_TYPES, Page, Wiki

_RESOURCE_SUFFIXES = {".md", ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_HINTS = {"missing": "目标不存在", "ambiguous": "目标有歧义", "unsafe": "不安全的链接"}
_WIKILINK = r"(!?)\[\[([^\]\n]+)\]\]"
_MATH = r"(?<!\\)(?:\$\$([\s\S]+?)(?<!\\)\$\$|\$([^\n$]+?)(?<!\\)\$)"


def _fragment(value: str) -> str:
    return quote(slugify_unicode(value, "-"), safe="") if value else ""


def _decode_url(value: str) -> str | None:
    """Reject obfuscated schemes/separators before interpreting any local path."""
    value = html.unescape(value).strip()
    for _ in range(10):
        decoded = unquote(value)
        if decoded == value:
            if any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
                return None
            return value
        value = decoded
    return None


def _destination(
    value: str, wiki: Wiki, source: Page | None, *, image: bool = False
) -> tuple[str | None, str]:
    value = _decode_url(value)
    if value is None or value.startswith("//"):
        return None, "unsafe"
    try:
        url = urlsplit(value)
    except ValueError:
        return None, "unsafe"
    if url.scheme:
        allowed = {"http", "https"} if image else {"http", "https", "mailto"}
        if url.scheme.lower() not in allowed:
            return None, "unsafe"
        if url.scheme.lower() in {"http", "https"} and not url.netloc:
            return None, "unsafe"
        return value, ""
    if url.netloc:
        return None, "unsafe"
    fragment = _fragment(url.fragment)
    suffix = "#" + fragment if fragment else ""
    if not url.path:
        return (suffix or "#", "") if not image else (None, "unsafe")
    # Generated page routes and existing material IDs remain stable.
    if not image and re.fullmatch(r"/(?:wiki/page/[^?#]+|item/\d+)", url.path):
        if any(part in {".", ".."} for part in url.path.split("/")):
            return None, "unsafe"
        return quote(url.path, safe="/") + suffix, ""
    # Raw links carry a stable material ID even in older vault layouts; mapping
    # the ID never serves a filesystem path outside the current vault.
    raw = re.fullmatch(r"(?:\.\./)*raw/(\d+)\.md", url.path)
    if raw and not image:
        return "/item/" + raw.group(1) + suffix, ""
    root = wiki.root.resolve()
    if url.path.startswith("/vault/"):
        path = root / url.path.removeprefix("/vault/")
    else:
        if url.path.startswith("/"):
            return None, "unsafe"
        matches = wiki.resolve(url.path, source=source)
        if len(matches) > 1:
            return None, "ambiguous"
        if matches and not image:
            return "/wiki/page/" + quote(matches[0].slug, safe="/") + suffix, ""
        if source is None:
            base = root
        elif source.path is not None:
            base = source.path.resolve().parent
        else:
            base = root / "pages" / PAGE_TYPES[source.type]
        if not base.is_relative_to(root):
            return None, "unsafe"
        path = base / url.path
        # Obsidian also permits vault-root resource paths in wikilinks.
        if not url.path.startswith(("./", "../")) and not path.is_file():
            rooted = root / url.path
            if rooted.is_file() or url.path.split("/", 1)[0] in {"raw", "notes", "resources"}:
                path = rooted
    try:
        path = path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None, "unsafe"
    if not path.is_relative_to(root):
        return None, "unsafe"
    relative = path.relative_to(root)
    if not image and re.fullmatch(r"raw/\d+\.md", relative.as_posix()):
        return "/item/" + path.stem + suffix, ""
    if path.suffix.lower() not in (_IMAGE_SUFFIXES if image else _RESOURCE_SUFFIXES):
        return None, "missing"
    if relative.parts and relative.parts[0] == "pages" and path.suffix.lower() == ".md":
        return None, "missing"
    return "/vault/" + quote(relative.as_posix(), safe="/") + suffix, ""


def _math_element(text: str, display: bool, tag: str = "span") -> etree.Element:
    element = etree.Element(tag, {"class": "math", "data-display": str(display).lower()})
    element.text = AtomicString(text.strip())
    return element


class _MathInline(InlineProcessor):
    def handleMatch(self, match: re.Match, data: str):
        display = match.group(1) is not None
        return _math_element(match.group(1) if display else match.group(2), display), *match.span()


class _MathBlock(BlockProcessor):
    def test(self, parent: etree.Element, block: str) -> bool:
        return block.startswith("$$")

    def run(self, parent: etree.Element, blocks: list[str]) -> bool:
        collected = []
        for index, block in enumerate(blocks):
            collected.append(block)
            joined = "\n\n".join(collected)
            closing = re.search(r"(?<!\\)\$\$", joined[2:])
            if closing is None:
                continue
            end = closing.start() + 2
            parent.append(_math_element(joined[2:end], True, "div"))
            del blocks[: index + 1]
            remainder = joined[end + 2 :].lstrip("\n")
            if remainder:
                blocks.insert(0, remainder)
            return True
        return False


class _WikiInline(InlineProcessor):
    def __init__(self, md: markdown.Markdown, wiki: Wiki, source: Page | None):
        super().__init__(_WIKILINK, md)
        self.wiki, self.source = wiki, source

    def handleMatch(self, match: re.Match, data: str):
        embedded, content = match.groups()
        target, separator, alias = content.partition("|")
        name, _, heading = target.partition("#")
        matches = self.wiki.resolve(name.strip(), source=self.source) if name.strip() else []
        if not name.strip() and self.source is not None:
            matches = [self.source]
        label = alias if separator else (matches[0].title if len(matches) == 1 else target)
        if not separator and heading:
            label += " › " + heading
        if len(matches) > 1:
            destination, status = None, "ambiguous"
        elif matches:
            destination = "/wiki/page/" + quote(matches[0].slug, safe="/")
            if heading:
                destination += "#" + _fragment(heading)
            status = ""
        else:
            destination, status = _destination(target, self.wiki, self.source, image=bool(embedded))
        if destination is None:
            element = etree.Element("span", {"class": status, "title": _HINTS[status]})
            element.text = AtomicString(label + "（" + _HINTS[status] + "）")
        elif embedded and destination.startswith("/vault/"):
            element = etree.Element("img", {"src": destination, "alt": label})
        else:
            element = etree.Element("a", {"href": destination})
            element.text = AtomicString(label)
        return element, *match.span()


class _VaultMarkdown(Extension):
    def __init__(self, wiki: Wiki, source: Page | None):
        super().__init__()
        self.wiki, self.source = wiki, source

    def extendMarkdown(self, md: markdown.Markdown) -> None:
        # Parse raw HTML as ordinary escaped text, including inline event attributes.
        md.preprocessors.deregister("html_block")
        md.inlinePatterns.deregister("html")
        md.inlinePatterns.register(_MathInline(_MATH, md), "vault_math", 185)
        md.inlinePatterns.register(_WikiInline(md, self.wiki, self.source), "vault_wikilink", 175)
        md.parser.blockprocessors.register(_MathBlock(md.parser), "vault_math_block", 85)


class _SafeHTML(HTMLParser):
    """Rebuild output with a small positive allowlist, never forwarding raw markup.

    Attribute values and text are always escaped on output. Unknown elements,
    comments, styles, events and URL schemes cannot enter the resulting HTML.
    This also sanitizes the HTML emitted by Markdown's fenced-code stash.
    """

    TAGS = {
        "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
        "ul", "ol", "li", "strong", "em", "del", "pre", "code", "table", "thead",
        "tbody", "tr", "th", "td", "a", "img", "span", "div",
    }
    VOID = {"br", "hr", "img"}
    ATTRS = {
        "a": {"href", "title"}, "img": {"src", "alt", "title"},
        "code": {"class"}, "span": {"class", "title", "data-display"},
        "div": {"class", "data-display"}, "ol": {"start"},
        **{f"h{n}": {"id"} for n in range(1, 7)},
    }

    def __init__(self, wiki: Wiki, source: Page | None):
        super().__init__(convert_charrefs=True)
        self.wiki, self.source = wiki, source
        self.output: list[str] = []
        self.stack: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.TAGS:
            return
        attributes = {key: value for key, value in attrs if key in self.ATTRS.get(tag, ())}
        output_tag, hint = tag, ""
        if tag in {"a", "img"}:
            key = "href" if tag == "a" else "src"
            destination, status = _destination(
                attributes.get(key) or "", self.wiki, self.source, image=tag == "img"
            )
            if destination is not None:
                attributes[key] = destination
            else:
                output_tag = "span"
                hint = "（" + _HINTS[status] + "）"
                alt = attributes.get("alt") or ""
                attributes = {"class": status, "title": _HINTS[status]}
                if tag == "img":
                    self.output.append(
                        '<span class="' + status + '">' + html.escape(alt + hint) + "</span>"
                    )
                    return
        serialized = "".join(
            f' {key}="{html.escape(value, quote=True)}"'
            for key, value in attributes.items() if value is not None
        )
        self.output.append("<" + output_tag + serialized + ">")
        if tag not in self.VOID:
            self.stack.append((tag, output_tag, hint))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID or not self.stack or self.stack[-1][0] != tag:
            return
        _, output_tag, hint = self.stack.pop()
        self.output.append(html.escape(hint) + "</" + output_tag + ">")

    def handle_data(self, data: str) -> None:
        self.output.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.handle_endtag(self.stack[-1][0])
        return "".join(self.output)


def render_markdown(text: str, wiki: Wiki, page: Page | None = None) -> str:
    """Render a vault document without executing HTML or selecting ambiguous pages."""
    rendered = markdown.markdown(
        text,
        extensions=[
            "tables", "fenced_code", "sane_lists",
            TocExtension(slugify=slugify_unicode), _VaultMarkdown(wiki, page),
        ],
    )
    sanitizer = _SafeHTML(wiki, page)
    sanitizer.feed(rendered)
    sanitizer.close()
    return sanitizer.result()
