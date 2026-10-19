"""
Markdown → 文档 / 页 / 编码段。

一个 .md 文档的结构（ppt-extract 产出）：

    # 文档标题
    > 原始文件 ...
    ## 📋 总体概要
    ## 幻灯片目录            ← 排除
    ## Slide 1: xxx
    > **📝 摘要**: ...
    > **🏷️ 标签**: `a` `b`
    正文...
    ## 🔗 关联文档            ← 排除

切片策略（bge-m3，8,192 token）：
  - 一页 = 一个编码单元（实测 94.6% 的页 ≤4,000 字符）
  - 仅超长页按结构边界拆段，**不在句子中间切**
  - 表格整表为一组；超长则按行分组并重复表头
"""
import hashlib
import os
import re
from typing import Dict, List, Tuple

import frontmatter

# 不进入索引的章节（实测它们会稳定抢答，见方案文档）
EXCLUDE_HEADING = re.compile(r'🔗\s*(关联文档|相关文档)|幻灯片目录|报告目录')
# ppt-extract 的页标题：`Slide 12: 标题`
SLIDE_HEADING = re.compile(r'^Slide\s+(\d+)\s*[:：]\s*(.*)$')
# 标签行 / 摘要行
TAG_LINE = re.compile(r'^>\s*\*\*?🏷️.*$', re.M)
SUMMARY_LINE = re.compile(r'^>\s*\*\*?📝\s*摘要\*?\*?[：:]\s*(.+)$', re.M)
HEADING = re.compile(r'^##\s+(.+)$', re.M)
H1 = re.compile(r'^#\s+(.+)$', re.M)
BACKTICK = re.compile(r'`([^`]+)`')

MAX_PART_CHARS = 4000          # bge-m3 的安全上限（8,192 token 留余量）
SUBSECTION_MIN_CHARS = 1500    # 超过此长度的 `##` 章节按 `###` 细分后再索引
TABLE_ROW = re.compile(r'^\s*\|')


def _clean_id(name: str) -> str:
    return name.strip()


def _split_parts(text: str, limit: int = MAX_PART_CHARS) -> List[Tuple[str, int, int]]:
    """把一页正文切成 ≤limit 的编码段，返回 [(text, start, end)]。

    结构感知：按空行分块（段落 / 列表项 / 表格行），贪心装箱；
    表格整表为一组，超长则按行分组并重复表头。
    """
    if len(text) <= limit:
        return [(text, 0, len(text))]

    blocks: List[str] = []
    buf: List[str] = []
    for line in text.split('\n'):
        if line.strip() == '':
            if buf:
                blocks.append('\n'.join(buf))
                buf = []
        else:
            buf.append(line)
    if buf:
        blocks.append('\n'.join(buf))

    parts: List[str] = []
    cur: List[str] = []
    cur_len = 0

    def flush():
        nonlocal cur, cur_len
        if cur:
            parts.append('\n\n'.join(cur))
            cur, cur_len = [], 0

    for b in blocks:
        if len(b) > limit:
            flush()
            # 超长单块：表格按行拆且重复表头，其余按字符硬切（最后手段）
            lines = b.split('\n')
            if len(lines) > 1 and TABLE_ROW.match(lines[0]):
                header, body_rows = lines[0], lines[1:]
                chunk = [header]
                size = len(header)
                for row in body_rows:
                    if size + len(row) > limit and len(chunk) > 1:
                        parts.append('\n'.join(chunk))
                        chunk, size = [header, row], len(header) + len(row)
                    else:
                        chunk.append(row)
                        size += len(row)
                if len(chunk) > 1:
                    parts.append('\n'.join(chunk))
            else:
                for i in range(0, len(b), limit):
                    parts.append(b[i:i + limit])
            continue

        if cur_len + len(b) + 2 > limit:
            flush()
        cur.append(b)
        cur_len += len(b) + 2
    flush()

    # 回填字符区间（用于定位；不保证精确到重叠边界）
    out, pos = [], 0
    for p in parts:
        idx = text.find(p, pos)
        if idx < 0:
            idx = pos
        out.append((p, idx, idx + len(p)))
        pos = idx + len(p)
    return out


def parse_document(abs_path: str, doc_id: str, rel_path: str = '') -> Tuple[Dict, List[Dict]]:
    """解析单个 .md，返回 (doc, slides)。

    rel_path 是相对 vault 的路径（形如 `<目录>/<文件>.md`）——
    `kg-qa` 用它拼 `obsidian://` 链接，必须原样保留。
    """
    with open(abs_path, 'r', encoding='utf-8') as f:
        raw = f.read()

    post = frontmatter.loads(raw)
    meta = dict(post.metadata or {})
    body = post.content or ''

    content_hash = hashlib.md5(raw.encode('utf-8')).hexdigest()

    # 标题回退链：frontmatter.title → 目录名 → 首个 # 标题
    title = (meta.get('title') or '').strip()
    if not title:
        m = H1.search(body)
        title = m.group(1).strip() if m else doc_id
    aliases = meta.get('aliases') or []
    if isinstance(aliases, str):
        aliases = [aliases]
    aliases = [str(a).strip() for a in aliases if str(a).strip()]

    doc = {
        'doc_id': doc_id,
        'rel_path': rel_path or doc_id,
        'title': title,
        'aliases': aliases,
        'doc_type': str(meta.get('document-type') or ''),
        'source': str(meta.get('source') or ''),
        'pages': int(meta.get('pages') or 0),
        'content_hash': content_hash,
        'updated_at': '',
    }

    heads = list(HEADING.finditer(body))
    slides: List[Dict] = []

    def meta_of(text: str):
        tags = []
        for line in text.split('\n'):
            if '🏷️' in line:
                tags = [t.strip() for t in BACKTICK.findall(line) if t.strip()]
                break
        m = SUMMARY_LINE.search(text)
        summary = m.group(1).strip() if m else ''
        # 标签行不进入正文（避免与 tags 列重复计分）；摘要行保留
        return tags, summary, TAG_LINE.sub('', text).strip()

    def emit(heading, text, tags, summary, parent=''):
        clean = TAG_LINE.sub('', text).strip()
        sm = SLIDE_HEADING.match(heading)
        kind = 'slide' if sm else ('summary' if '总体概要' in heading else 'other')
        path = '%s > %s' % (title, heading)
        if parent:
            path = '%s > %s > %s' % (title, parent, heading)
        slides.append({
            'doc_id': doc_id,
            'seq': len(slides),
            'heading': heading,
            'heading_path': path,
            'body': clean,
            'tags': tags,
            'summary': summary,
            'chars': len(clean),
            'kind': kind,
            'slide_no': int(sm.group(1)) if sm else None,
            'parts': _split_parts(clean),
        })

    for i, h in enumerate(heads):
        heading = h.group(1).strip()
        if EXCLUDE_HEADING.search(heading):
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        section = body[h.end():end].strip()
        section = re.sub(r'\n-{3,}\s*$', '', section).strip()

        tags, summary, _ = meta_of(section)

        # 长章节按 `###` 细分：报告/Talk Track 这类文档把具体主题放在三级标题下，
        # 整章作为一个 BM25 块会把主题权重稀释掉（实测「AI 泡沫」「岗位影响」
        # 都打不中 Reactive Modules 这一大块）。PPT 文档没有 `###`，不受影响。
        sub_heads = list(re.finditer(r'^###\s+(.+)$', section, re.M))
        if len(section) > SUBSECTION_MIN_CHARS and sub_heads:
            pre = section[:sub_heads[0].start()].strip()
            if pre:
                emit(heading, pre, tags, summary)
            for j, sh in enumerate(sub_heads):
                send = sub_heads[j + 1].start() if j + 1 < len(sub_heads) else len(section)
                body_j = section[sh.end():send].strip()
                sub_tags, sub_summary, _ = meta_of(body_j)
                emit(sh.group(1).strip(), body_j,
                     sub_tags or tags, sub_summary, parent=heading)
            continue

        emit(heading, section, tags, summary)

    return doc, slides


def scan_documents(vault: str, exclude_dirs: List[str],
                   exclude_patterns: List[str]) -> List[Tuple[str, str]]:
    """扫描 vault，返回 [(doc_id, abs_path)]。

    复用 kg_pipeline_config.scan_vault_md_files —— **不要另写一套扫描规则**
    （kb-ingest/verify.py 与 kg-qa 自检各自硬编码过一版，三者不一致会误报）。
    """
    from kg_pipeline_config import scan_vault_md_files

    files = scan_vault_md_files(vault, exclude_dirs, exclude_patterns)
    out = []
    for rel_path, abs_path in sorted(files.items()):
        if '/asset/' in rel_path.replace('\\', '/'):
            continue
        # 文档目录名作为稳定标识（vault 是扁平结构：<Title>/<Title>.md）
        doc_id = os.path.dirname(rel_path) or os.path.splitext(os.path.basename(rel_path))[0]
        out.append((_clean_id(doc_id), rel_path, abs_path))
    return out
