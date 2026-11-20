"""按文本块编辑 config/sources.yaml，保留注释与其余格式（ADR 0018）。

一个信源条目 = 从 `- id: X` 那一行开始，到下一个缩进不大于它的非空行（下一个条目、
同级注释或顶层键）之前；末尾的空行不算在块内。所有修改都在写入前重新解析校验。
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml
from pydantic import ValidationError

from secondbrain import config
from secondbrain.models import SourceConfig

_ID_LINE = re.compile(r"^(?P<indent>\s*)-\s+id:\s*['\"]?(?P<id>[\w.-]+)['\"]?\s*(#.*)?$")
_EMPTY_LIST = re.compile(r"^sources:\s*\[\s*\]\s*(#.*)?$", re.M)
_SOURCES_KEY = re.compile(r"^sources:\s*(#.*)?$", re.M)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def block_span(lines: list[str], source_id: str) -> tuple[int, int] | None:
    """条目在 lines 中的 [start, end)；找不到返回 None。"""
    for start, line in enumerate(lines):
        m = _ID_LINE.match(line)
        if m and m.group("id") == source_id:
            base = len(m.group("indent"))
            end = start + 1
            while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > base):
                end += 1
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            return start, end
    return None


def entry_text(text: str, source_id: str) -> str | None:
    """条目的原文（含其中的注释）；用于把 example 中的条目原样追加到个人配置。"""
    lines = text.splitlines()
    span = block_span(lines, source_id)
    return "\n".join(lines[span[0] : span[1]]) if span else None


def dump_entry(source: SourceConfig) -> str:
    """新信源（Web 表单）的 YAML 块，两格缩进，与 example 的风格一致。"""
    body = yaml.safe_dump(
        [config.source_to_dict(source)], allow_unicode=True, sort_keys=False, width=1000
    )
    return "\n".join(f"  {line}" if line else line for line in body.rstrip().splitlines())


def append_entry(text: str, block: str) -> str:
    """在文本末尾追加条目块；`sources: []` 先改为 `sources:`。"""
    if _EMPTY_LIST.search(text):
        text = _EMPTY_LIST.sub("sources:", text, count=1)
    elif not _SOURCES_KEY.search(text):
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + "sources:\n"
    head = text.rstrip("\n")
    gap = "\n" if head.endswith("sources:") else "\n\n"  # 第一个条目紧跟 sources:
    return head + gap + block.rstrip("\n") + "\n"


def remove_entry(text: str, source_id: str) -> str:
    """按块删除条目（连同其后的空行）；删光后写回 `sources: []`。"""
    lines = text.splitlines()
    span = block_span(lines, source_id)
    if span is None:
        raise config.ConfigError(f"找不到信源：{source_id}")
    start, end = span
    while end < len(lines) and not lines[end].strip():
        end += 1
    lines = lines[:start] + lines[end:]
    new = "\n".join(lines) + "\n"
    if not any(_ID_LINE.match(line) for line in lines):
        new = _SOURCES_KEY.sub("sources: []", new, count=1)
    return new


def set_enabled(text: str, source_id: str, enabled: bool) -> str:
    """改条目中的 `enabled:` 值（保留行尾注释）；没有该行时在块末尾添加。"""
    lines = text.splitlines()
    span = block_span(lines, source_id)
    if span is None:
        raise config.ConfigError(f"找不到信源：{source_id}")
    start, end = span
    value = "true" if enabled else "false"
    pattern = re.compile(r"^(\s+enabled:\s*)(\S+)(.*)$")
    for i in range(start + 1, end):
        m = pattern.match(lines[i])
        if m:
            lines[i] = f"{m.group(1)}{value}{m.group(3)}"
            break
    else:
        field_indent = " " * (_indent(lines[start]) + 2)
        lines.insert(end, f"{field_indent}enabled: {value}")
    return "\n".join(lines) + "\n"


def _yaml_value(value: Any) -> str:
    # JSON flow values are YAML-compatible and keep multiline strings on one line.
    return json.dumps(value, ensure_ascii=False)


def _value_comments(text: str, start: int, end: int) -> list[str]:
    scalars = [
        (token.start_mark.index, token.end_mark.index)
        for token in yaml.scan(text)
        if isinstance(token, yaml.ScalarToken)
    ]
    comments = []
    offset = start
    for line in text[start:end].splitlines(keepends=True):
        for match in re.finditer("#", line):
            position = offset + match.start()
            if not any(left <= position < right for left, right in scalars):
                comments.append(" " * _indent(line) + line[match.start():].rstrip("\r\n"))
                break
        offset += len(line)
    return comments


def update_entry(text: str, source_id: str, changes: dict) -> str:
    """只更新指定字段；原位保留其他条目、字段顺序及不相关注释。"""
    sources = config.parse_sources_text(text)
    current = next((source for source in sources if source.id == source_id), None)
    if current is None:
        raise config.ConfigError(f"找不到信源：{source_id}")
    try:
        updated = SourceConfig.model_validate({**current.model_dump(), **changes})
    except ValidationError as exc:
        raise config.ConfigError(f"信源 {source_id} 不合法：{exc}") from exc
    if any(source.id == updated.id and source.id != source_id for source in sources):
        raise config.ConfigError(f"信源 id 已存在：{updated.id}")
    if not changes:
        return text

    document = yaml.compose(text)
    sequence = next(value for key, value in document.value if key.value == "sources")
    entry = next(
        node
        for node in sequence.value
        if any(key.value == "id" and value.value == source_id for key, value in node.value)
    )
    fields = {key.value: (key, value) for key, value in entry.value}
    edits: list[tuple[int, int, str]] = []
    missing: list[str] = []
    for field in changes:
        value = _yaml_value(getattr(updated, field))
        if field not in fields:
            missing.append(f"{field}: {value}")
            continue
        key_node, value_node = fields[field]
        start = text.index(":", key_node.end_mark.index) + 1
        end = value_node.end_mark.index
        if value_node.tag == "tag:yaml.org,2002:null" and value_node.start_mark.index == end:
            end = start
        value = " " + value
        block_value = (
            isinstance(value_node, (yaml.SequenceNode, yaml.MappingNode))
            and not value_node.flow_style
        )
        if block_value:
            comments = _value_comments(text, start, end)
            if comments:
                value += "\n" + "\n".join(comments)
            value += "\n" + " " * value_node.end_mark.column
        elif isinstance(value_node, yaml.ScalarNode) and value_node.style in ("|", ">"):
            value += "\n" + " " * value_node.end_mark.column
        edits.append((start, end, value))
    if missing:
        if entry.flow_style:
            end = entry.end_mark.index - 1
            edits.append((end, end, ", " + ", ".join(missing)))
        else:
            id_value = fields["id"][1]
            end = id_value.end_mark.index
            newline = text.find("\n", end)
            end = len(text) if newline < 0 else newline + 1
            prefix = "\n" if end and text[end - 1] != "\n" else ""
            indent = " " * entry.value[0][0].start_mark.column
            body = "".join(f"{indent}{field}\n" for field in missing)
            edits.append((end, end, prefix + body))
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    config.parse_sources_text(text)
    return text


def write(text: str) -> list[SourceConfig]:
    """校验通过后原子写入 config/sources.yaml；校验失败抛 ConfigError，文件不变。"""
    path = config.config_dir() / "sources.yaml"
    sources = config.parse_sources_text(text, str(path))
    config._atomic_write(path, text)
    return sources
