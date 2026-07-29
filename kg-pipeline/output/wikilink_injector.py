"""
Wiki-Link 注入模块
根据文档间相似度，自动向文档注入高置信度的 [[wiki-links]]。
"""
import re
from typing import Dict, List, Set, Tuple


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


def generate_related_section(target_doc: str, related_docs: List[Tuple[str, float]]) -> str:
    """
    生成 related 区域文本。
    related_docs: [(doc_name, similarity_score), ...]
    """
    if not related_docs:
        return ''

    lines = ['\n## 🔗 关联文档\n']
    lines.append('> 以下关联由知识图谱管道自动生成（基于标签共现相似度）\n')
    for doc_name, score in related_docs:
        lines.append(f'- [[{doc_name}]] — 相似度 {score:.2f}\n')
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
    通过在文档末尾添加「关联文档」章节实现。
    返回 (new_content, changed)。
    """
    # 只保留超过阈值的关联
    high_confidence = [(name, score) for name, score in related_docs if score >= threshold]
    if not high_confidence:
        return content, False

    # 检查是否已存在关联文档章节
    if '## 🔗 关联文档' in content:
        # 移除旧的自动生成部分，重新生成
        content = re.sub(
            r'\n## 🔗 关联文档\n.*?(?=\n## |\Z)',
            '',
            content,
            flags=re.DOTALL
        )

    related_section = generate_related_section(file_path, high_confidence)
    new_content = content.rstrip() + '\n' + related_section

    return new_content, True


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
