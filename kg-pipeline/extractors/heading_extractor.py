"""
标题提取器
提取文档的 markdown 标题层级结构（H1-H6）。
"""
import re
from typing import List, Dict, Optional


HEADING_PATTERN = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)


def extract_headings(content: str) -> List[Dict]:
    """
    提取文档中所有标题及其层级。
    返回 [{'level': int, 'title': str, 'line': int}, ...]
    """
    headings = []
    lines = content.split('\n')
    for i, line in enumerate(lines):
        m = HEADING_PATTERN.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            # 去除标题中的 markdown 格式标记
            title = re.sub(r'[*_~`]+', '', title)
            headings.append({
                'level': level,
                'title': title,
                'line': i + 1
            })
    return headings


def build_heading_tree(headings: List[Dict]) -> List[Dict]:
    """
    将扁平的标题列表构建为树形结构。
    每个节点: {'title': str, 'level': int, 'line': int, 'children': [...]}
    """
    if not headings:
        return []

    root = {'title': '__root__', 'level': 0, 'line': 0, 'children': []}
    stack = [root]

    for h in headings:
        node = {'title': h['title'], 'level': h['level'], 'line': h['line'], 'children': []}

        # 找到合适的父节点
        while stack and stack[-1]['level'] >= h['level']:
            stack.pop()

        if stack:
            stack[-1]['children'].append(node)
        else:
            root['children'].append(node)

        stack.append(node)

    return root['children']


def get_heading_path(headings: List[Dict], target_line: int) -> Optional[str]:
    """
    获取指定行在标题层级中的路径。
    例如: "AI方向 → RAG → 知识实效性"
    """
    path_parts = []
    for h in sorted(headings, key=lambda x: x['line']):
        if h['line'] > target_line:
            break
        # 清理路径中已不合适的部分
        path_parts = [p for p in path_parts if p[0] < h['level']]
        path_parts.append((h['level'], h['title']))

    if path_parts:
        return ' → '.join([p[1] for p in path_parts])
    return None
