"""
共现分析模块
计算标签共现矩阵和文档间 Jaccard 相似度。
"""
import math
from typing import Dict, List, Set, Tuple
from collections import defaultdict


def build_cooccurrence_matrix(doc_tags: Dict[str, List[str]]) -> Dict[Tuple[str, str], int]:
    """
    构建标签共现矩阵。
    doc_tags: {doc_path: [tag1, tag2, ...]}
    返回: {(tag_a, tag_b): cooccurrence_count}
    """
    cooccur = defaultdict(int)

    for doc_path, tags in doc_tags.items():
        unique_tags = list(set(tags))
        for i in range(len(unique_tags)):
            for j in range(i + 1, len(unique_tags)):
                a, b = sorted([unique_tags[i], unique_tags[j]])
                cooccur[(a, b)] += 1

    return dict(cooccur)


def jaccard_similarity(set_a: Set[str], set_b: Set[str]) -> float:
    """计算两个集合的 Jaccard 相似度。"""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def compute_document_similarities(
    doc_tags: Dict[str, List[str]]
) -> List[Dict]:
    """
    计算所有文档对之间的 Jaccard 相似度（基于标签集合）。
    返回: [{'doc_a': str, 'doc_b': str, 'similarity': float, 'shared_tags': [...]}, ...]
    """
    doc_paths = list(doc_tags.keys())
    results = []

    for i in range(len(doc_paths)):
        for j in range(i + 1, len(doc_paths)):
            a_path = doc_paths[i]
            b_path = doc_paths[j]
            tags_a = set(doc_tags[a_path])
            tags_b = set(doc_tags[b_path])
            sim = jaccard_similarity(tags_a, tags_b)
            if sim > 0:
                results.append({
                    'doc_a': a_path,
                    'doc_b': b_path,
                    'similarity': round(sim, 4),
                    'shared_tags': sorted(tags_a & tags_b)
                })

    # 按相似度降序排列
    results.sort(key=lambda x: x['similarity'], reverse=True)
    return results


def compute_tag_coccurrence_weights(
    cooccur_matrix: Dict[Tuple[str, str], int],
    tag_doc_counts: Dict[str, int]
) -> Dict[Tuple[str, str], float]:
    """
    基于共现矩阵计算标签间的 Jaccard 权重。
    tag_doc_counts: {tag: 包含该标签的文档数}
    返回: {(tag_a, tag_b): jaccard_weight}
    """
    weights = {}
    for (a, b), count in cooccur_matrix.items():
        count_a = tag_doc_counts.get(a, 0)
        count_b = tag_doc_counts.get(b, 0)
        union = count_a + count_b - count
        weight = count / union if union > 0 else 0.0
        weights[(a, b)] = round(weight, 4)
    return weights


def get_tag_doc_counts(doc_tags: Dict[str, List[str]]) -> Dict[str, int]:
    """计算每个标签出现在多少文档中。"""
    counts = defaultdict(int)
    for tags in doc_tags.values():
        for tag in set(tags):
            counts[tag] += 1
    return dict(counts)
