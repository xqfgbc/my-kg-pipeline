"""
Wiki-Link 提取器
从 .md 文件正文中提取 [[wiki-links]]，包括普通链接和带别名的链接。
"""
import re
from typing import List, Dict, Set


# 匹配 [[target]] 或 [[target|alias]]
WIKILINK_PATTERN = re.compile(r'\[\[([^\]|#]+?)(?:[|#][^\]]+?)?\]\]')


def extract_wikilinks(content: str) -> List[str]:
    """
    从文档正文中提取所有 wiki-link 目标名称。
    返回去重后的目标名称列表。
    """
    matches = WIKILINK_PATTERN.findall(content)
    seen: Set[str] = set()
    result = []
    for m in matches:
        target = m.strip()
        if target and target not in seen:
            seen.add(target)
            result.append(target)
    return result


def extract_wikilinks_with_context(content: str) -> List[Dict]:
    """
    从文档正文中提取 wiki-link，返回包含上下文信息。
    每个结果包含 target、line_context（所在行的前后50字符）。
    """
    results = []
    lines = content.split('\n')
    for line in lines:
        matches = WIKILINK_PATTERN.finditer(line)
        for m in matches:
            target = m.group(1).strip()
            if target:
                start = max(0, m.start() - 50)
                end = min(len(line), m.end() + 50)
                ctx = line[start:end].strip()
                results.append({
                    'target': target,
                    'context': ctx
                })
    return results


def wikilink_exists(content: str, target: str) -> bool:
    """检查文档中是否已存在指向 target 的 wiki-link。"""
    for link in extract_wikilinks(content):
        if link.lower() == target.lower():
            return True
    return False
