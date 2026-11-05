"""Offline document organization by structure, keyword rules and TF-IDF.

Input wording stays intact. Similarity produces suggestions, exact duplicates
retain all source locations, and ambiguous notes remain available for review.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import math
import re
import unicodedata


ORGANIZED_PREFIX = "organized-"
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_BLOCKS = 10000
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
CHINESE_HEADING = re.compile(r"^(?:第[一二三四五六七八九十百零\d]+[章节篇]|[一二三四五六七八九十]+[、．])\s*(\S.{0,45})$")
LABEL = re.compile(r"^([a-zA-Z_\u4e00-\u9fff][\w\u4e00-\u9fff /·-]{1,15})[：:]\s*(.*)$")
LIST = re.compile(r"^(\s*)(?:[-+*]\s+|\d+[.)、]\s+|[一二三四五六七八九十]+、\s*)\S")
ORDERED = re.compile(r"^\s*(?:\d+[.)、]\s+|[一二三四五六七八九十]+、\s*)\S")
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
REFERENCE = re.compile(r"^ {0,3}\[([^\]\n]+)\]:\s*\S")
STOP = re.compile(r"我们|你们|他们|这个|那个|这些|那些|可以|需要|进行|以及|一个|一些|目前|相关|内容|信息|文档|资料|通过|使用|支持|要求|功能|应该|时候|然后|因为|所以|但是|还有|项目|部分|能够|已经|处理|操作|本次|现在|完成|设置|调整|增加|减少|全部|所有|保持|更多|这里|因此|例如|包括|建议|事项|记录|说明")
ENGLISH_STOP = set("the and for with that this from have will should into your text file document information http https www com png jpg".split())
ALIASES = {
    "项目背景": "背景", "背景信息": "背景", "项目目标": "目标", "预期目标": "目标",
    "限制条件": "约束条件", "约束": "约束条件", "参考资料": "参考", "参考链接": "参考",
    "问题记录": "问题", "问题反馈": "问题", "已知问题": "问题",
}
NUMBER_PREFIX = re.compile(r"^(?:(?:第[一二三四五六七八九十百零\d]+[章节篇])|(?:[一二三四五六七八九十]+[、．])|(?:\d+(?:\.\d+)*[.、)\s]))\s*")
NUMBERS = re.compile(r"\d+(?:[.,:/-]\d+)*(?:\s*(?:%|％|ms|秒|分钟|小时|天|元|MB|GB|毫米|厘米|米))?", re.IGNORECASE)
LIMIT_WORDS = re.compile(r"不允许|不可以|不能|不要|不得|禁止|必须|允许|可以|无需|至少|最多|大于|小于|仅限")


class OrganizationCancelled(Exception):
    pass


@dataclass(frozen=True)
class OrganizeOptions:
    mode: str = "topic"
    merge_duplicates: bool = True
    suggest_similar: bool = True
    rules_text: str = ""

    def validate(self):
        if self.mode not in ("topic", "structure", "source"):
            raise ValueError("请选择有效的整理方式。")
        if not isinstance(self.merge_duplicates, bool) or not isinstance(self.suggest_similar, bool):
            raise ValueError("整理选项必须是勾选状态。")
        parse_rules(self.rules_text)


@dataclass
class Block:
    source: str
    start: int
    end: int
    headings: tuple[str, ...]
    text: str
    kind: str = "text"
    topic: str = ""
    basis: str = ""
    origins: list[str] = field(default_factory=list)
    close_fence: str = ""


@dataclass
class OrganizationResult:
    content: str
    report: str
    stats: dict[str, int]
    blocks: list[Block]


def parse_rules(text):
    """Each ordered line declares a topic and literal keyword aliases."""
    if not isinstance(text, str) or len(text) > 20000:
        raise ValueError("整理规则必须是 20000 字以内的文本。")
    rules = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, words = line.partition("=")
        name = name.strip()
        terms = tuple(dict.fromkeys(term.strip() for term in re.split(r"[,，、;；]", words) if term.strip()))
        if not separator or not name or len(name) > 40 or not terms or len(terms) > 30:
            raise ValueError(f"第 {number} 行规则无效。请按‘主题 = 关键词, 同义词’填写。")
        if name in rules or any(len(term) < 2 or len(term) > 48 for term in terms):
            raise ValueError(f"第 {number} 行主题重复，或关键词长度超出 2–48 字。")
        rules[name] = terms
    if len(rules) > 50:
        raise ValueError("一次最多配置 50 个整理主题。")
    return rules


def _heading(line):
    if line.startswith(("    ", "\t", ">")):
        return None
    match = HEADING.match(line)
    if match:
        return len(match[1]), match[2].strip()
    match = CHINESE_HEADING.match(line.strip())
    if match and not line.rstrip().endswith(("。", "！", "？", ";", "；")):
        return 2, match[1].strip()
    match = LABEL.match(line.strip())
    if match and not match[2] and match[1].lower() not in ("http", "https", "file"):
        return 2, match[1].strip()
    return None


def split_material(source, text):
    """Keep code, tables, ordered lists, quotations and nested lists intact."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headings = []
    blocks = []
    pending = None
    index = 0
    while index < len(lines):
        if len(blocks) > MAX_BLOCKS:
            raise ValueError("所选资料超过 10000 个内容块，请分批整理。")
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        heading = _heading(line)
        if CHINESE_HEADING.match(line) and index + 1 < len(lines) and CHINESE_HEADING.match(lines[index + 1]):
            heading = None
        setext = index + 1 < len(lines) and re.fullmatch(r" {0,3}(?:={3,}|-{3,})\s*", lines[index + 1])
        if setext and not LIST.match(line) and not line.startswith(("    ", "\t", ">", "|")):
            heading = (1 if lines[index + 1].lstrip().startswith("=") else 2, line.strip())
        if heading:
            if pending is not None:
                blocks.append(Block(source, pending + 1, pending + 1, tuple(title for _, title in headings), "", "heading"))
            level, title = heading
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title))
            pending = index
            index += 2 if setext else 1
            continue

        start = index
        kind = "text"
        closing = ""
        marker = FENCE.match(line)
        if marker:
            kind = "code"
            index += 1
            while index < len(lines):
                end = FENCE.match(lines[index])
                index += 1
                if end and end[1][0] == marker[1][0] and len(end[1]) >= len(marker[1]) and not end[2].strip():
                    break
            else:
                closing = marker[1]
        elif index + 1 < len(lines) and "|" in line and TABLE_RULE.match(lines[index + 1]):
            kind = "table"
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                index += 1
        elif LIST.match(line):
            kind = "list"
            ordered = bool(ORDERED.match(line))
            indent = len(LIST.match(line)[1])
            index += 1
            while index < len(lines):
                next_line = lines[index]
                next_item = LIST.match(next_line)
                if not next_line.strip():
                    lookahead = index + 1
                    while lookahead < len(lines) and not lines[lookahead].strip():
                        lookahead += 1
                    if lookahead == len(lines) or not lines[lookahead].startswith((" ", "\t")):
                        break
                if (_heading(next_line) and not next_item) or FENCE.match(next_line):
                    break
                if next_item and len(next_item[1]) <= indent and (not ordered or not ORDERED.match(next_line)):
                    break
                if next_line.strip() and not next_item and not next_line.startswith((" ", "\t")):
                    break
                index += 1
        elif line.startswith(("    ", "\t")):
            kind = "code"
            index += 1
            while index < len(lines) and (lines[index].startswith(("    ", "\t")) or not lines[index].strip()):
                index += 1
        elif line.startswith(">") or REFERENCE.match(line) or line.lstrip().startswith("<"):
            kind = "protected"
            index += 1
            while index < len(lines) and lines[index].strip() and not _heading(lines[index]):
                index += 1
        else:
            index += 1
            while index < len(lines) and lines[index].strip():
                if _heading(lines[index]) or FENCE.match(lines[index]) or LIST.match(lines[index]):
                    break
                if lines[index - 1].rstrip().endswith(("。", "！", "？", ".", "!", "?")) or LABEL.match(lines[index].strip()):
                    break
                if index + 1 < len(lines) and (TABLE_RULE.match(lines[index + 1]) or re.fullmatch(r" {0,3}(?:={3,}|-{3,})\s*", lines[index + 1])):
                    break
                index += 1

        body = "\n".join(lines[start:index]).strip("\n")
        fragments = re.split(r"(?<=[；;。！？])(?=\s*[a-zA-Z_\u4e00-\u9fff][\w\u4e00-\u9fff ·-]{1,15}[：:])", body) if kind == "text" else [body]
        offset = start
        for fragment in fragments:
            end_line = offset + fragment.count("\n") + 1
            blocks.append(Block(source, offset + 1, end_line, tuple(title for _, title in headings), fragment, kind, close_fence=closing))
            if len(blocks) > MAX_BLOCKS:
                raise ValueError("所选资料超过 10000 个内容块，请分批整理。")
            offset += fragment.count("\n")
        pending = None
    if pending is not None:
        blocks.append(Block(source, pending + 1, pending + 1, tuple(title for _, title in headings), "", "heading"))
    if len(blocks) > MAX_BLOCKS:
        raise ValueError("所选资料超过 10000 个内容块，请分批整理。")
    return blocks


def _namespace_references(blocks, number):
    """Keep Markdown reference labels local to their original source."""
    labels = {}
    for block in blocks:
        if block.kind == "code":
            continue
        for line in block.text.splitlines():
            match = REFERENCE.match(line)
            if match:
                label = match[1]
                labels[label.casefold()] = f"reqdoc-s{number}-{label.lstrip('^')}"
    if not labels:
        return
    for block in blocks:
        if block.kind == "code":
            continue
        def reference(match):
            caption, label = match[1], match[2] or match[1].lstrip("!")[1:-1]
            target = labels.get(label.casefold())
            return caption + "[" + target + "]" if target else match[0]

        def shortcut(match):
            whole, label = match[1], match[2]
            target = labels.get(label.casefold())
            return whole + "[" + target + "]" if target and not label.startswith("^") else whole

        rendered = []
        for line in block.text.splitlines():
            definition = REFERENCE.match(line)
            if definition:
                label = definition[1]
                target = labels[label.casefold()]
                if label.startswith("^"):
                    target = "^" + target
                line = line.replace("[" + label + "]:", "[" + target + "]:", 1)
            else:
                line = re.sub(r"(?<!\\)(!?\[[^\]\n]*\])\[([^\]\n]*)\]", reference, line)
                line = re.sub(r"(?<![\\\]])(!?\[([^\]\n]+)\])(?![\[(])", shortcut, line)
                for label, target in labels.items():
                    if label.startswith("^"):
                        line = re.sub(re.escape("[" + label + "]"), "[^" + target + "]", line, flags=re.IGNORECASE)
            rendered.append(line)
        block.text = "\n".join(rendered)


def _normalize_heading(title):
    title = unicodedata.normalize("NFKC", title).strip().rstrip("：:")
    title = NUMBER_PREFIX.sub("", title).strip()
    return ALIASES.get(title, title)


def _topic_path(block):
    return " / ".join(_normalize_heading(part) for part in block.headings)


def _keyword_match(text, term):
    if re.fullmatch(r"[a-z0-9_ -]+", term):
        return bool(re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", text))
    return term in text


def _features(text):
    text = re.sub(r"!?\[([^\]\n]*)\]\([^\n)]*\)", r"\1", text[:12000])
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    text = STOP.sub(" ", unicodedata.normalize("NFKC", text[:12000]).casefold())
    words = Counter(word for word in re.findall(r"[a-z][a-z0-9_+-]{1,30}", text) if word not in ENGLISH_STOP)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        for width in (2, 3, 4):
            words.update(run[index:index + width] for index in range(len(run) - width + 1))
    return Counter(dict(words.most_common(250)))


def _vectors(blocks, *, common_only=False, mask_values=False):
    counters = []
    for block in blocks:
        text = NUMBERS.sub(" ", LIMIT_WORDS.sub(" ", block.text)) if mask_values else block.text
        counters.append(_features(text) if block.kind in ("text", "list") else Counter())
    frequency = Counter(term for counts in counters for term in counts)
    size = max(1, len(blocks))
    vectors = []
    for counts in counters:
        vector = {term: (1 + math.log(count)) * (1 + math.log((size + 1) / (frequency[term] + 1)))
                  for term, count in counts.items() if not common_only or frequency[term] >= 2}
        length = math.sqrt(sum(weight * weight for weight in vector.values())) or 1
        vectors.append({term: weight / length for term, weight in vector.items()})
    return vectors


def _similarity(left, right):
    return sum(weight * right.get(term, 0) for term, weight in left.items())


def _candidates(vector, index):
    hits = Counter()
    for term in sorted(vector, key=vector.get, reverse=True)[:30]:
        hits.update(index.get(term, ()))
    return [number for number, _ in hits.most_common(60)]


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise OrganizationCancelled()


def _assign_topics(blocks, rules, vectors, cancel):
    for block in blocks:
        _check_cancel(cancel)
        body = unicodedata.normalize("NFKC", block.text[:12000]).casefold()
        heading_text = unicodedata.normalize("NFKC", " / ".join(block.headings)).casefold()
        scores = []
        for topic, terms in rules.items():
            hits = sum(_keyword_match(body, unicodedata.normalize("NFKC", term).casefold())
                       + 2 * _keyword_match(heading_text, unicodedata.normalize("NFKC", term).casefold()) for term in terms)
            if hits:
                scores.append((hits, topic))
        scores.sort(key=lambda item: -item[0])
        if scores and (len(scores) == 1 or scores[0][0] > scores[1][0]):
            block.topic, block.basis = scores[0][1], "关键词规则"
        elif _topic_path(block):
            block.topic, block.basis = _topic_path(block), "原文章节"
        else:
            match = LABEL.match(block.text.split("\n", 1)[0]) if block.kind == "text" else None
            if match and not match[2].startswith("//") and match[1].lower() not in ("http", "https", "file"):
                block.topic, block.basis = _normalize_heading(match[1]), "行内主题"
            elif scores:
                block.topic, block.basis = "待归类", "多个关键词主题并列命中"

    anchors = {}
    known_text = defaultdict(set)
    for block in blocks:
        if block.topic and block.topic != "待归类":
            anchors.setdefault(block.topic, _features(block.topic))
            if block.text.strip():
                known_text[" ".join(block.text.split())].add(block.topic)
    anchor_index = defaultdict(list)
    for topic, terms in anchors.items():
        for term in terms:
            anchor_index[term].append(topic)
    clusters = []
    inverted = defaultdict(list)
    for number, (block, vector) in enumerate(zip(blocks, vectors)):
        _check_cancel(cancel)
        if block.topic:
            continue
        exact_topics = known_text.get(" ".join(block.text.split()), set())
        if len(exact_topics) == 1:
            block.topic, block.basis = next(iter(exact_topics)), "与已归类原文完全重复"
            continue
        anchor_scores = defaultdict(float)
        for term, weight in vector.items():
            for topic in anchor_index.get(term, ()):
                anchor_scores[topic] += weight
        matches = [(score, topic) for topic, score in anchor_scores.items()]
        matches.sort(reverse=True)
        if matches and matches[0][0] >= 0.25 and (len(matches) == 1 or matches[0][0] > matches[1][0] * 1.35):
            block.topic, block.basis = matches[0][1], "正文匹配已有主题"
            continue
        best_cluster = None
        best_score = 0.28
        for cluster_id in _candidates(vector, inverted):
            representative = clusters[cluster_id][0]
            score = _similarity(vector, vectors[representative])
            shared = set(vector).intersection(vectors[representative])
            if score > best_score and shared:
                best_cluster, best_score = cluster_id, score
        if best_cluster is None:
            best_cluster = len(clusters)
            clusters.append([])
            for term in sorted(vector, key=vector.get, reverse=True)[:30]:
                if len(inverted[term]) < 120:
                    inverted[term].append(best_cluster)
        clusters[best_cluster].append(number)
    for members in clusters:
        if len(members) < 2:
            blocks[members[0]].topic, blocks[members[0]].basis = "待归类", "相似度不足，保留原文"
            continue
        counts = Counter(term for number in members for term in vectors[number])
        scores = {term: sum(vectors[number].get(term, 0) for number in members)
                  for term, count in counts.items() if count >= max(2, math.ceil(len(members) * 0.6))}
        terms = []
        for term in sorted(scores, key=lambda item: (-scores[item] * math.sqrt(len(item)), item)):
            if not any(term in old or old in term for old in terms):
                terms.append(term)
            if len(terms) == 2:
                break
        topic = "相近内容 · " + " / ".join(terms) if terms else "待归类"
        for number in members:
            blocks[number].topic, blocks[number].basis = topic, "正文词组相似，建议归类"


def _duplicate_key(block):
    if block.kind in ("code", "table", "protected", "list"):
        return block.topic, block.kind, block.text
    text = "\n".join(" ".join(line.split()) for line in block.text.splitlines()).strip()
    return block.topic, block.kind, unicodedata.normalize("NFC", text)


def _origin(block):
    span = f"第 {block.start} 行" if block.start == block.end else f"第 {block.start}–{block.end} 行"
    context = " / ".join(block.headings)
    return f"{block.source} · {span}" + (f" · {context}" if context else "")


def _review_pairs(blocks, vectors, cancel):
    index = defaultdict(list)
    reviews = []
    for number, (block, vector) in enumerate(zip(blocks, vectors)):
        _check_cancel(cancel)
        if len(block.text) < 12 or block.kind not in ("text", "list"):
            continue
        best = None
        for previous in _candidates(vector, index):
            if blocks[previous].topic != block.topic:
                continue
            score = _similarity(vector, vectors[previous])
            if score >= 0.65 and (best is None or score > best[0]):
                best = score, previous
        if best is not None:
            other = blocks[best[1]]
            differences = []
            if NUMBERS.findall(block.text) != NUMBERS.findall(other.text):
                differences.append("数值或日期不同")
            if LIMIT_WORDS.findall(block.text) != LIMIT_WORDS.findall(other.text):
                differences.append("允许、否定或限制表达不同")
            reviews.append((best[1] + 1, number + 1, "、".join(differences) or "措辞相近，建议核对是否重复"))
        for term in sorted(vector, key=vector.get, reverse=True)[:30]:
            if len(index[term]) < 150:
                index[term].append(number)
        if len(reviews) >= 200:
            break
    return reviews


def organize_with_report(materials, options=None, *, cancel=None):
    options = options or OrganizeOptions()
    options.validate()
    rules = parse_rules(options.rules_text)
    size = 0
    blocks = []
    source_count = 0
    for source, text in materials:
        _check_cancel(cancel)
        if not isinstance(source, str) or not isinstance(text, str):
            raise ValueError("资料名称和正文必须是文本。")
        size += len(text.encode("utf-8"))
        if size > MAX_INPUT_BYTES:
            raise ValueError("所选正文合计超过 20 MB，请分批整理。")
        source_count += 1
        material = split_material(source, text)
        _namespace_references(material, source_count)
        blocks.extend(material)
        if len(blocks) > MAX_BLOCKS:
            raise ValueError("所选资料超过 10000 个内容块，请分批整理。")
    if not blocks:
        raise ValueError("选中的资料没有可整理的正文。")
    input_count = len(blocks)
    vectors = _vectors(blocks, common_only=True)
    if options.mode == "topic":
        _assign_topics(blocks, rules, vectors, cancel)
    else:
        for block in blocks:
            path = _topic_path(block)
            block.topic = block.source if options.mode == "source" else path or "未分节内容"
            block.basis = "按来源保序" if options.mode == "source" else "原文章节"

    kept = []
    unique = {}
    for block in blocks:
        _check_cancel(cancel)
        block.origins.append(_origin(block))
        duplicate_key = _duplicate_key(block)
        if options.merge_duplicates and duplicate_key in unique:
            unique[duplicate_key].origins.extend(block.origins)
        else:
            kept.append(block)
            unique[duplicate_key] = block
    reviews = _review_pairs(kept, _vectors(kept, mask_values=True), cancel) if options.suggest_similar else []
    groups = defaultdict(list)
    for number, block in enumerate(kept, 1):
        groups[block.topic].append((number, block))
    ordered = [name for name in rules if name in groups]
    ordered.extend(name for name in groups if name not in ordered and name != "待归类")
    if "待归类" in groups:
        ordered.append("待归类")

    output = ["# 整理结果"]
    for topic in ordered:
        output.append("## " + topic)
        for number, block in groups[topic]:
            output.append(f"> 条目 {number} · 来源：" + "；".join(block.origins))
            if block.text:
                output.append(block.text)
            if block.close_fence:
                output.append(block.close_fence)
    content = "\n\n".join(output) + "\n"
    if len(content.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("整理结果超过 20 MB，请减少本次选择的资料。")

    stats = {"sources": source_count, "input_blocks": input_count, "output_blocks": len(kept),
             "duplicates": input_count - len(kept), "topics": len(groups),
             "unclassified": len(groups.get("待归类", [])), "reviews": len(reviews)}
    report = [f"资料 {source_count} 份，识别 {input_count} 块，保留 {len(kept)} 块，合并重复 {stats['duplicates']} 块。",
              f"分为 {len(groups)} 组，待归类 {stats['unclassified']} 块，相近内容核对 {len(reviews)} 对。",
              "", "归类依据"]
    for number, block in enumerate(kept, 1):
        report.append(f"条目 {number} → {block.topic}（{block.basis}）")
    if reviews:
        report.extend(["", "相近内容核对（两份原文均已保留，最多列出 200 对）"])
        report.extend(f"条目 {left} ↔ 条目 {right}，{reason}" for left, right, reason in reviews)
    repairs = [number for number, block in enumerate(kept, 1) if block.close_fence]
    if repairs:
        report.extend(["", "排版修正", "条目 " + "、".join(map(str, repairs)) + " 在结果中补齐代码围栏结束标记。"])
    report.extend(["", "本地规则说明",
                   "按原文章节、行内主题、关键词和正文词组相似度整理。词组相似仅表示建议归类。",
                   "保留原文措辞、数值、图片链接、表格和代码。相近段落并列保留，合并重复时记录所有来源行号。",
                   "长段落的分类采用前 12000 字提取特征，输出保留完整原文。归类不足的内容放在待归类。"])
    return OrganizationResult(content, "\n".join(report), stats, kept)


def organize_materials(materials, options=None):
    """Compatibility entry point for callers that only need organized text."""
    return organize_with_report(materials, options).content
