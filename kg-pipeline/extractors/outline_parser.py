"""
大纲解析器
解析嵌套大纲结构（缩进列表、章节结构等）。
用于提取文档中的结构化知识路径。
"""
import re
from typing import List, Dict, Optional


# 匹配缩进列表项: "  - xxx" 或 "  1. xxx"
LIST_ITEM_PATTERN = re.compile(r'^(\s*)(?:[-*+]|\d+[.)])\s+(.+)$')

# 匹配章节结构: "章节 01：xxx" 或 "Chapter 1: xxx"
CHAPTER_PATTERN = re.compile(r'^(?:章节|Chapter)\s*\d+[：:]\s*(.+)', re.IGNORECASE)

# 匹配箭头分隔的路径: "xxx → yyy → zzz"
ARROW_PATH_PATTERN = re.compile(r'(.+?)\s*→\s*(.+)')


def extract_outline(content: str) -> List[Dict]:
    """
    从文档中提取大纲结构。
    识别缩进列表、编号列表等形成的层级大纲。
    返回 [{'indent': int, 'text': str, 'line': int, 'children': [...]}, ...]
    """
    lines = content.split('\n')
    root_items = []
    stack: List[Dict] = []  # (indent_level, node)

    for i, line in enumerate(lines):
        m = LIST_ITEM_PATTERN.match(line)
        if not m:
            continue

        indent = len(m.group(1))
        text = m.group(2).strip()
        # 清理 markdown 格式
        text = re.sub(r'\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]', r'\1', text)

        node = {
            'indent': indent,
            'text': text,
            'line': i + 1,
            'children': []
        }

        # 确定父节点
        while stack and stack[-1]['indent'] >= indent:
            stack.pop()

        if stack:
            stack[-1]['children'].append(node)
        else:
            root_items.append(node)

        stack.append(node)

    return root_items


def extract_arrow_paths(content: str) -> List[Dict]:
    """
    从文档中提取用 → 箭头分隔的层级路径。
    例如: "技术解决方案 → 可观测性 → Dynatrace"
    返回 [{'path': ['技术解决方案', '可观测性', 'Dynatrace'], 'line': N}, ...]
    """
    results = []
    lines = content.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()

        # 跳过 markdown 表格行
        if '|' in stripped and stripped.startswith('|'):
            continue
        # 跳过代码块
        if stripped.startswith('`'):
            continue
        # 跳过 📝 摘要 / 📋 概要 等描述性行
        if '📝' in stripped or '📋' in stripped:
            continue
        # 跳过以 > 开头的块引用（摘要行）
        if stripped.startswith('>'):
            continue

        # 预处理：去除行内的 ** 粗体标记
        clean_line = stripped.replace('**', '')

        # 检查是否包含 → 箭头
        if '→' not in clean_line:
            continue

        # 用 → 分割
        segments = clean_line.split('→')
        if len(segments) < 2:
            continue

        # 清理每个片段
        parts = []
        # 描述性关键词（如果片段以这些词开头/包含这些词，不是实体路径）
        noise_words = ['流程', '右侧', '左侧', '中部', '底部', '端到端', '图例', '交付物']
        for s in segments:
            s = s.strip()
            if not s:
                continue
            # 跳过包含噪声词的片段
            if any(w in s for w in noise_words):
                continue
            # 去除末尾的括号内容
            s = re.sub(r'[（(][^）)]*[）)]$', '', s).strip()
            # 去除末尾的 ：
            s = re.sub(r'[：:]\s*$', '', s).strip()
            # 跳过过长的片段
            if len(s) > 50:
                continue
            if s:
                parts.append(s)

        if len(parts) >= 2:
            results.append({
                'path': parts,
                'line': i + 1
            })

    return results


def extract_chapters(content: str) -> List[str]:
    """提取文档中的章节标题。"""
    chapters = []
    lines = content.split('\n')
    for line in lines:
        m = CHAPTER_PATTERN.match(line.strip())
        if m:
            chapters.append(m.group(1).strip())
    return chapters
