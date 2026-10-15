"""
Wiki-Link 注入模块
根据文档间相似度，自动向文档注入高置信度的 [[wiki-links]]。
"""
import re
from typing import Dict, List, Optional, Set, Tuple


def find_injection_point(content: str) -> int:
    """
    找到 wiki-link 注入位置（frontmatter 之后、正文开始处）。
    返回字符偏移量。
    """
    stripped = content.lstrip()
    if stripped.startswith('---'):
        # 跳过 frontmatter
        end_match = re.search(r'\n---\n', stripped[3:])
        if end_match:
            return len(content) - len(stripped) + 3 + end_match.start() + 4
    # 没有 frontmatter，在文件开头注入
    return 0


# 关联文档章节的两个标题。addToObsidian 早期写「相关文档」，管道写「关联文档」，
# 二者语义相同，必须互认 —— 否则同一文件里会出现两个重复的关联章节。
RELATED_HEADINGS = ('## 🔗 关联文档', '## 🔗 相关文档')

_AUTO_NOTE = '> 以下关联由知识图谱管道自动生成（基于标签共现相似度）'


def find_related_section(content: str) -> Optional[Tuple[int, int]]:
    """
    定位已有的关联文档章节，返回 (start, end)；没有则返回 None。
    范围从标题行起，到下一个 H2 标题之前或文末为止。
    """
    for heading in RELATED_HEADINGS:
        idx = content.find(heading)
        if idx == -1:
            continue
        # 要求独占一行，避免匹配到正文里对标题的引用
        if idx > 0 and content[idx - 1] != '\n':
            continue
        next_h2 = re.search(r'\n## ', content[idx:])
        end = idx + next_h2.start() if next_h2 else len(content)
        return idx, end
    return None


def _split_trailing_separators(section: str) -> Tuple[str, List[str]]:
    """
    把章节末尾的分隔线（--- / *** / ___）与正文拆开。

    关联章节后面常跟一条 --- 作为与下一节的视觉分隔。补充条目时必须插在
    分隔线之前，否则分隔线会落到章节中间，把它劈成两半。
    返回 (正文, 分隔线列表)。
    """
    body = section.rstrip()
    separators: List[str] = []

    while True:
        m = re.search(r'(?:\A|\n)([ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*)$', body)
        if not m:
            break
        separators.insert(0, m.group(1))
        body = body[:m.start()].rstrip()

    return body, separators


def generate_related_entries(related_docs: List[Tuple[str, float]]) -> List[str]:
    """生成关联条目行（不含章节标题）。"""
    return [f'- [[{doc_name}]] — 相似度 {score:.2f}'
            for doc_name, score in related_docs]


def generate_related_section(related_docs: List[Tuple[str, float]]) -> str:
    """生成一个全新的关联文档章节（文档中尚无该章节时使用）。"""
    if not related_docs:
        return ''
    lines = ['\n## 🔗 关联文档\n\n', _AUTO_NOTE + '\n']
    lines.extend(line + '\n' for line in generate_related_entries(related_docs))
    return ''.join(lines)


def auto_inject_wikilinks(
    file_path: str,
    content: str,
    related_docs: List[Tuple[str, float]],
    threshold: float = 0.5,
    create_backup: bool = True
) -> Tuple[str, bool]:
    """
    自动注入高置信度 wiki-links（Jaccard >= threshold）。

    已有「关联文档」或「相关文档」章节时**就地补充**缺失的关联，保留该章节里
    上游（addToObsidian）或人工撰写的条目与关系描述，不再新建重复章节。
    返回 (new_content, changed)；已全部存在时 changed=False（幂等，不触发写盘）。
    """
    # 只保留超过阈值的关联
    high_confidence = [(name, score) for name, score in related_docs if score >= threshold]
    if not high_confidence:
        return content, False

    existing = find_related_section(content)
    if existing is None:
        return content.rstrip() + '\n' + generate_related_section(high_confidence), True

    start, end = existing
    section = content[start:end]

    # 只补尚未出现的，不覆盖已有条目
    missing = [(name, score) for name, score in high_confidence
               if f'[[{name}]]' not in section]
    if not missing:
        return content, False

    entries = ''.join(line + '\n' for line in generate_related_entries(missing))
    body, separators = _split_trailing_separators(section)

    if _AUTO_NOTE in section:
        # 已有自动生成说明：条目直接续在现有的自动条目之后，避免空行把列表断开
        new_section = body + '\n' + entries
    else:
        # 首次注入：空行分段，再写上说明与条目（此时 body 里是上游/人工的表格）
        new_section = body + '\n\n' + _AUTO_NOTE + '\n' + entries

    if separators:
        new_section += '\n' + ''.join(line + '\n' for line in separators)

    return content[:start] + new_section + content[end:], True


def build_wikilink_recommendations(
    doc_similarities: List[Dict],
    current_doc: str,
    auto_threshold: float = 0.5,
    suggest_threshold: float = 0.3
) -> Dict[str, List]:
    """
    基于文档相似度结果，为指定文档生成 wiki-link 建议。
    返回:
    {
      'auto_inject': [(doc_name, similarity), ...],   # >= auto_threshold
      'suggest': [(doc_name, similarity), ...]          # >= suggest_threshold 但 < auto_threshold
    }
    """
    auto_inject = []
    suggest = []

    for sim in doc_similarities:
        doc_a = sim['doc_a']
        doc_b = sim['doc_b']

        # 确定关联目标
        if doc_a == current_doc:
            target = doc_b
        elif doc_b == current_doc:
            target = doc_a
        else:
            continue

        # 提取文件名（不含路径和扩展名）
        target_name = target.rsplit('.', 1)[0].rsplit('/', 1)[-1]

        if sim['similarity'] >= auto_threshold:
            auto_inject.append((target_name, sim['similarity']))
        elif sim['similarity'] >= suggest_threshold:
            suggest.append((target_name, sim['similarity']))

    return {
        'auto_inject': sorted(auto_inject, key=lambda x: x[1], reverse=True),
        'suggest': sorted(suggest, key=lambda x: x[1], reverse=True)
    }
