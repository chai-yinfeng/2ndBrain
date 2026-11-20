"""Rendered content, navigation and unsafe payloads in an offline temporary vault."""

from html.parser import HTMLParser
from urllib.parse import quote

import pytest

from secondbrain.web.render import render_markdown
from secondbrain.wiki.store import Page, Wiki


class Document(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)

    def attrs(self, tag, attribute):
        return [attrs[attribute] for kind, attrs in self.elements
                if kind == tag and attribute in attrs]

    @property
    def content(self):
        return "".join(self.text)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    wiki = Wiki(root=root, raw_root=root / "raw")
    for slug, title, aliases in [
        ("attention", "注意力机制", ["注意力", "Attention"]),
        ("cache", "缓存", ["共享名称"]),
        ("memory", "内存", ["共享名称"]),
    ]:
        wiki.save_page(Page(slug, {"title": title, "type": "concept", "aliases": aliases}, ""))
    source = wiki.pages["cache"]
    for relative in ["raw/42.md", "notes/我的 笔记.md", "resources/图片 (1).png"]:
        path = root / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"fixture")
    return wiki, source


def test_wikilinks_use_slug_title_alias_and_unicode_heading(vault):
    wiki, source = vault
    doc = Document(render_markdown(
        "[[attention|别名]] [[注意力机制#工作 原理|跳转]] [[注意力]] [[#当前 标题|本页]]\n\n"
        "## 当前 标题\n\n中文 **正文**。", wiki, source,
    ))
    assert doc.attrs("a", "href") == [
        "/wiki/page/attention", "/wiki/page/attention#" + quote("工作-原理"),
        "/wiki/page/attention", "/wiki/page/cache#" + quote("当前-标题"),
    ]
    assert doc.attrs("h2", "id") == ["当前-标题"]
    assert "别名" in doc.content and "跳转" in doc.content and "中文 正文" in doc.content


def test_target_heading_matches_rendered_destination(vault):
    wiki, source = vault
    target = Document(render_markdown("## 工作 原理\n\n## Élan 中文", wiki, source))
    links = Document(render_markdown("[[cache#工作 原理]] [[cache#Élan 中文]]", wiki, source))
    assert links.attrs("a", "href") == [
        "/wiki/page/cache#" + quote(anchor) for anchor in target.attrs("h2", "id")
    ]


def test_missing_and_ambiguous_links_are_readable_not_random(vault):
    wiki, source = vault
    doc = Document(render_markdown("[[不存在|找不到]] [[共享名称|有多个]]", wiki, source))
    assert doc.attrs("a", "href") == []
    assert "找不到（目标不存在）" in doc.content
    assert "有多个（目标有歧义）" in doc.content
    assert set(doc.attrs("span", "class")) == {"missing", "ambiguous"}


def test_markdown_links_also_explain_missing_and_ambiguous_targets(vault):
    wiki, source = vault
    doc = Document(render_markdown("[丢失](不存在) [多个](共享名称)", wiki, source))
    assert not doc.attrs("a", "href")
    assert "丢失（目标不存在）" in doc.content
    assert "多个（目标有歧义）" in doc.content


def test_cjk_page_ids_and_relative_page_paths_remain_clickable(vault):
    wiki, source = vault
    wiki.save_page(Page("中文页面", {"title": "中文标题", "type": "concept"}, ""))
    doc = Document(render_markdown(
        "[[中文页面|中文别名]] [相对页面](中文页面.md)", wiki, source,
    ))
    assert doc.attrs("a", "href") == [
        "/wiki/page/" + quote("中文页面"), "/wiki/page/" + quote("中文页面"),
    ]
    assert "中文别名" in doc.content and "相对页面" in doc.content


def test_exact_stable_slug_wins_over_another_pages_alias(vault):
    wiki, source = vault
    wiki.pages["memory"].meta["aliases"].append("attention")
    doc = Document(render_markdown("[[attention]]", wiki, source))
    assert doc.attrs("a", "href") == ["/wiki/page/attention"]
    assert doc.content == "注意力机制"


def test_relative_pages_raw_notes_and_images_use_safe_routes(vault):
    wiki, source = vault
    doc = Document(render_markdown(
        "[页面](attention.md) [中文页面](注意力机制) "
        "[原文](../../raw/42.md) [笔记](<../../notes/我的%20笔记.md>)\n\n"
        "![图](<../../resources/图片%20(1).png>)\n\n"
        "[[../../notes/我的 笔记.md|笔记别名]] ![[resources/图片 (1).png|图别名]]",
        wiki, source,
    ))
    assert doc.attrs("a", "href") == [
        "/wiki/page/attention", "/wiki/page/attention", "/item/42",
        "/vault/notes/" + quote("我的 笔记.md"), "/vault/notes/" + quote("我的 笔记.md"),
    ]
    assert doc.attrs("img", "src") == [
        "/vault/resources/" + quote("图片 (1).png"),
        "/vault/resources/" + quote("图片 (1).png"),
    ]
    assert doc.attrs("img", "alt") == ["图", "图别名"]


def test_root_index_resolves_pages_and_vault_resources(vault):
    wiki, _ = vault
    doc = Document(render_markdown(
        "[[注意力]] [原文](raw/42.md) [笔记](<notes/我的 笔记.md>)", wiki,
    ))
    assert doc.attrs("a", "href") == [
        "/wiki/page/attention", "/item/42", "/vault/notes/" + quote("我的 笔记.md"),
    ]


def test_alias_payloads_are_text_not_html_or_markdown(vault):
    wiki, source = vault
    doc = Document(render_markdown(
        '[[attention|<img src=x onerror="alert(1)">]] '
        '[[attention|**不是粗体**]] [[不存在|<script>alert(1)</script>]]', wiki, source,
    ))
    assert "<img src=x onerror=" in doc.content
    assert "**不是粗体**" in doc.content
    assert not any(tag in {"img", "script", "strong"} for tag, _ in doc.elements)
    assert doc.attrs("a", "href") == ["/wiki/page/attention", "/wiki/page/attention"]


def test_tables_fenced_inline_and_indented_code_preserve_literal_content(vault):
    wiki, source = vault
    doc = Document(render_markdown(
        "| 项目 | 内容 |\n| --- | --- |\n| 页面 | [[attention]] |\n\n"
        "`[[attention]] $x$ <img>`\n\n"
        "```python\n[[attention]]\n$x$\n<script>alert(1)</script>\n```\n\n"
        "    [[attention]] $x$\n", wiki, source,
    ))
    assert any(tag == "table" for tag, _ in doc.elements)
    assert doc.attrs("a", "href") == ["/wiki/page/attention"]
    assert doc.content.count("[[attention]]") == 3
    assert doc.content.count("$x$") == 3
    assert "<script>alert(1)</script>" in doc.content
    assert "<img>" in doc.content
    assert not doc.attrs("span", "data-display")
    assert not any(tag == "script" for tag, _ in doc.elements)


def test_equations_remain_literal_tex_until_safe_client_render(vault):
    wiki, source = vault
    doc = Document(render_markdown(
        "行内 $a_b + \\frac{1}{2}$，转义 \\$不是公式。\n\n"
        "$$\n\\begin{aligned}\nx &= 1 \\\\\n\ny &= 2\n\\end{aligned}\n$$\n\n"
        "之后 [[attention]]，同行 $$x^2$$。", wiki, source,
    ))
    assert doc.attrs("span", "data-display") == ["false", "true"]
    assert doc.attrs("div", "data-display") == ["true"]
    assert r"a_b + \frac{1}{2}" in doc.content
    assert r"\begin{aligned}" in doc.content and "y &= 2" in doc.content
    assert "$不是公式" in doc.content
    assert doc.attrs("a", "href") == ["/wiki/page/attention"]


def test_math_cannot_inject_markup_and_does_not_expand_wikilinks(vault):
    wiki, source = vault
    payload = r'\text{<img src=x onerror=alert(1)>} + [[attention]] + \href{javascript:alert(1)}{x}'
    doc = Document(render_markdown("$" + payload + "$", wiki, source))
    assert doc.content == payload
    assert doc.attrs("span", "data-display") == ["false"]
    assert not any(tag in {"img", "a", "script"} for tag, _ in doc.elements)


@pytest.mark.parametrize("destination", [
    "javascript:alert%281%29", "JaVaScRiPt:alert%281%29", "data:text/html;base64,PHNjcmlwdD4=",
    "vbscript:msgbox%281%29", "//evil.example/x", "%2f%2fevil.example/x",
    "java%09script:alert%281%29", "%6aavascript:alert%281%29",
    "%256aavascript:alert%25281%2529", "file:///etc/passwd", "https:%2f%2fevil.example%0a/x",
])
def test_dangerous_urls_never_become_active_links_or_images(vault, destination):
    wiki, source = vault
    doc = Document(render_markdown(
        f"[链接](<{destination}>) ![图片](<{destination}>)", wiki, source,
    ))
    assert not doc.attrs("a", "href")
    assert not doc.attrs("img", "src")
    assert "链接" in doc.content and "图片" in doc.content


@pytest.mark.parametrize("target", [
    "../../../private.txt", "../../%2e%2e/private.txt",
    "../../%252e%252e/private.txt", "/vault/../private.txt",
    "/vault/%2e%2e/private.txt", "../../resources/%2f../../private.txt",
    "../../resources/%5c..%5cprivate.txt",
])
def test_relative_links_cannot_escape_vault(vault, target):
    wiki, source = vault
    doc = Document(render_markdown(f"[私密](<{target}>) ![私密图](<{target}>)", wiki, source))
    assert not doc.attrs("a", "href")
    assert not doc.attrs("img", "src")
    assert "不安全" in doc.content


def test_symlink_escape_is_rejected(vault, tmp_path):
    wiki, source = vault
    outside = tmp_path / "private.png"
    outside.write_bytes(b"private")
    (wiki.root / "resources" / "escape.png").symlink_to(outside)
    doc = Document(render_markdown(
        "[外部](../../resources/escape.png) ![[resources/escape.png]]", wiki, source,
    ))
    assert not doc.attrs("a", "href") and not doc.attrs("img", "src")
    assert "不安全" in doc.content


def test_raw_html_event_attributes_and_fence_attributes_do_not_execute(vault):
    wiki, source = vault
    doc = Document(render_markdown(
        '<script>alert(1)</script>\n\n<img src="x" onerror="alert(2)">\n\n'
        '<svg onload=alert(3)><a href="javascript:alert(4)">x</a></svg>\n\n'
        '[安全链接](https://example.org "\\\" onmouseover=alert(5)")\n\n'
        '```{.python onclick="alert(6)"}\nprint("safe")\n```', wiki, source,
    ))
    assert "<script>alert(1)</script>" in doc.content
    assert not any(tag in {"script", "svg", "img"} for tag, _ in doc.elements)
    assert all(not name.startswith("on") and name != "style"
               for _, attrs in doc.elements for name in attrs)


def test_safe_external_links_and_unicode_resource_names_remain_usable(vault):
    wiki, source = vault
    doc = Document(render_markdown(
        '[网页](https://example.org/search?q=中文&lang=zh) [邮件](mailto:me@example.org) '
        '[资源](<../../resources/图片%20%281%29.png>)', wiki, source,
    ))
    assert doc.attrs("a", "href") == [
        "https://example.org/search?q=中文&lang=zh", "mailto:me@example.org",
        "/vault/resources/" + quote("图片 (1).png"),
    ]


def test_internal_edit_markers_are_hidden_but_code_examples_still_render(vault):
    wiki, source = vault
    marker = "<!-- wiki-rewrite:start -->"
    text = (
        marker + "\n正文可读。\n<!-- wiki-rewrite:end -->\n"
        "<!-- wiki-ingest:start material-7:abcdef -->\n"
        "<!-- material-7:abcdef -->\n补充解释。\n"
        "<!-- wiki-ingest:end material-7:abcdef -->\n\n"
        "```html\n" + marker + "\n```\n"
    )
    doc = Document(render_markdown(text, wiki, source))
    assert "正文可读" in doc.content and "补充解释" in doc.content
    assert doc.content.count(marker) == 1
    assert "wiki-rewrite:end" not in doc.content
    assert "material-7:abcdef" not in doc.content
