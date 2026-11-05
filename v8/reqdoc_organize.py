"""Organize local notes by their headings and keep their source names."""
from __future__ import annotations

import re
import unicodedata


HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
ORGANIZED_PREFIX = "organized-"


def sections(text):
    headings = []
    body = []
    fence = None
    for line in text.splitlines():
        marker = FENCE.match(line)
        if fence:
            body.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            body.append(line)
            continue
        heading = HEADING.match(line)
        if heading:
            yield tuple(title for _, title in headings), "\n".join(body).strip("\n")
            level = len(heading[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, heading[2].strip()))
            body = []
        else:
            body.append(line)
    if fence:
        body.append(fence)
    yield tuple(title for _, title in headings), "\n".join(body).strip("\n")


def organize_materials(materials):
    """Merge matching section paths and identical blocks, preserving order."""
    groups = {}
    for source_name, text in materials:
        for headings, body in sections(text):
            if not headings and not body.strip():
                continue
            key = tuple(
                " ".join(unicodedata.normalize("NFKC", part).split()).casefold()
                for part in headings
            )
            _, blocks = groups.setdefault(key, (headings, {}))
            names = blocks.setdefault(body, {})
            names[source_name] = None
    if not groups:
        raise ValueError("选中的资料没有可整理的正文。")

    output = ["# 整理结果"]
    for headings, blocks in groups.values():
        output.append("## " + (" / ".join(headings) or "未分节内容"))
        for body, names in blocks.items():
            output.append("> 来源：" + "、".join(names))
            if body:
                output.append(body)
    return "\n\n".join(output) + "\n"
