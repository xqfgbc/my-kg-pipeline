#!/usr/bin/env python3
"""
知识库检索器 —— 根据用户问题从图谱和原文中检索相关知识。
用法: python3 kg-pipeline/retrieval.py "如何实现AIOps"
输出: JSON，包含匹配的实体、关联概念、原文摘要

v2 优化:
  - jieba 分词替代 n-gram 暴力切词
  - EntityResolver 双语别名扩展（中→英 / 英→中）
  - TF-IDF + cosine similarity 替代子串匹配
"""
import sys
import json
import os
import re
import yaml
from pathlib import Path
from typing import List, Dict, Any, Set, Tuple

import jieba
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KG_DIR = os.path.dirname(SCRIPT_DIR)
GRAPH_PATH = os.path.join(KG_DIR, "kg-pipeline-data", "graph.json")
CONFIG_PATH = os.path.join(KG_DIR, "kg-pipeline", "config.yaml")

# 动态加载本地配置
sys.path.insert(0, KG_DIR)
from kg_pipeline_config import get_vault_path
VAULT_PATH = get_vault_path()


def load_graph() -> Dict:
    with open(GRAPH_PATH, 'r') as f:
        return json.load(f)


# ============================================================
# 优化 1: jieba 中文分词
# ============================================================

def tokenize_query(query: str) -> List[str]:
    """jieba 中文分词 + 英文词提取 + 原始全文。"""
    tokens = []

    # 1. jieba 精确模式分词（处理中文）
    chinese_tokens = list(jieba.cut(query))
    tokens.extend(t for t in chinese_tokens if len(t.strip()) >= 2)

    # 2. 英文词提取
    eng_words = re.findall(r'[A-Za-z]+', query)
    tokens.extend(w.lower() for w in eng_words if len(w) >= 2)

    # 3. 2-gram 组合（保留词组能力）
    for i in range(len(chinese_tokens) - 1):
        bigram = chinese_tokens[i] + chinese_tokens[i + 1]
        if len(bigram.strip()) >= 2:
            tokens.append(bigram)

    # 4. 原始查询全文（精确匹配回退）
    tokens.append(query.lower())

    return list(set(tokens))


# ============================================================
# 优化 3: 双语别名扩展（复用 EntityResolver 现有别名）
# ============================================================

def _load_alias_index() -> Dict[str, str]:
    """从 config.yaml + config.aliases.yaml 合并加载实体别名映射。"""
    alias_index = {}
    config_paths = [
        os.path.join(KG_DIR, "config.aliases.local.yaml"),     # 本地别名（人工+自动）
    ]
    for path in config_paths:
        try:
            with open(path, 'r') as f:
                data = yaml.safe_load(f) or {}
            entity_aliases = data.get('entity_aliases', {})
            for canonical, aliases in entity_aliases.items():
                alias_index[canonical.lower().strip()] = canonical
                for alias in aliases:
                    key = alias.lower().strip()
                    if key not in alias_index:
                        alias_index[key] = canonical
        except FileNotFoundError:
            pass
        except Exception:
            pass
    return alias_index


# 模块加载时构建别名索引
_ALIAS_INDEX = _load_alias_index()


def expand_bilingual(tokens: List[str]) -> List[str]:
    """利用别名反向索引做中英双语扩展。

    对每个 token，如果命中 alias_index，
    扩展出 canonical name + 同组所有别名。
    """
    expanded = list(tokens)
    for token in tokens:
        key = token.lower().strip()
        if key in _ALIAS_INDEX:
            canonical = _ALIAS_INDEX[key]
            expanded.append(canonical)
            # 如果 config 中有同组别名，也加入
            # （此处仅扩展规范名，别名在 TF-IDF entity_texts 中覆盖）
    return list(set(expanded))


# ============================================================
# 优化 2: TF-IDF + cosine similarity 实体匹配
# ============================================================

class EntityMatcher:
    """基于 TF-IDF 的实体匹配器。"""

    def __init__(self, graph: Dict):
        self.entity_nodes = [
            n for n in graph['nodes']
            if n['type'] in ('entity', 'tag', 'topic')
        ]
        self.entity_names = [e.get('canonical_name', e['id']) for e in self.entity_nodes]

        # 为每个实体构建"富文本"：规范名 + 所有别名拼接
        self.entity_texts = []
        for e in self.entity_nodes:
            canonical = e.get('canonical_name', e['id'])
            aliases = self._gather_aliases(canonical)
            self.entity_texts.append(f"{canonical} {' '.join(aliases)}")

        # TF-IDF 向量化
        self.vectorizer = TfidfVectorizer(
            analyzer='char_wb',
            ngram_range=(2, 4),
            max_features=5000,
        )
        self.entity_matrix = self.vectorizer.fit_transform(self.entity_texts)

    def _gather_aliases(self, canonical: str) -> List[str]:
        """收集实体的所有别名（从 alias_index 反向查找）。"""
        aliases = []
        for alias_key, can_name in _ALIAS_INDEX.items():
            if can_name == canonical and alias_key != canonical.lower():
                aliases.append(alias_key)
        return aliases

    def search(self, query_tokens: List[str], top_k: int = 8) -> List[Dict]:
        """TF-IDF + cosine 匹配实体。"""
        query_text = ' '.join(query_tokens)
        query_vec = self.vectorizer.transform([query_text])
        scores = cosine_similarity(query_vec, self.entity_matrix).flatten()
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)

        results = []
        for idx, score in ranked[:top_k]:
            if score > 0:
                results.append({
                    'entity': self.entity_nodes[idx],
                    'score': float(score),
                })
        return results


def match_entities(graph: Dict, tokens: List[str], top_k: int = 8) -> Tuple[List[Dict], Dict[str, float]]:
    """
    TF-IDF 版实体匹配。返回（实体列表, {entity_id: score}）。
    """
    matcher = EntityMatcher(graph)
    matched = matcher.search(tokens, top_k=top_k)
    entities = [m['entity'] for m in matched]
    scores = {m['entity']['id']: m['score'] for m in matched}
    return entities, scores


def get_related_entities(graph: Dict, entity_id: str, top_k: int = 8) -> List[Tuple[str, float]]:
    """获取与指定实体共现权重最高的关联实体。"""
    related = []
    for e in graph['edges']:
        if e['type'] != 'cooccurs_with':
            continue
        if e['source'] == entity_id:
            related.append((e['target'].replace('entity:', ''), e['weight']))
        elif e['target'] == entity_id:
            related.append((e['source'].replace('entity:', ''), e['weight']))

    related.sort(key=lambda x: x[1], reverse=True)
    seen = set()
    result = []
    for name, weight in related:
        if name not in seen:
            seen.add(name)
            result.append((name, weight))
            if len(result) >= top_k:
                break
    return result


def extract_passages_from_docs(graph: Dict, entity_ids: List[str], vault: str) -> List[Dict]:
    """
    根据实体 provenance 定位原文段落。
    支持两种文档结构：
      - ppt-extract: slide = "Slide N" → 匹配 ## Slide N 标题
      - web-pdf-extract: slide = 章节标题 → 匹配对应 ## 标题
        slide = "unknown" → 回退到 ## 摘要 章节
    """
    passages = []
    seen = set()

    # 收集所有 provenance 信息
    target_entities = set(entity_ids)
    for n in graph['nodes']:
        if n['id'] in target_entities:
            for p in n.get('provenance', []):
                doc_path = p['file']
                slides = set(p.get('slides', []))
                for s in slides:
                    key = (doc_path, s)
                    if key not in seen:
                        seen.add(key)
                        passages.append({
                            'doc': doc_path,
                            'slide': s,
                            'entity': n.get('canonical_name', n['id']),
                        })

    # 读取原文，提取内容
    result = []
    doc_cache = {}
    for psg in passages[:30]:  # 限制数量
        doc_path = psg['doc']
        if doc_path not in doc_cache:
            try:
                with open(os.path.join(vault, doc_path), 'r') as f:
                    doc_cache[doc_path] = f.read()
            except Exception:
                continue

        content = doc_cache[doc_path]
        slide_content = None
        title = psg['slide']
        summary = ''

        # ---- 定位内容：分三种情况 ----
        if psg['slide'] == 'unknown':
            # 回退：提取 ## 摘要 章节作为文档级上下文
            summary_match = re.search(
                r'## 摘要\s*\n+(.*?)(?=\n(?:---|## |#> ))',
                content, re.DOTALL
            )
            if summary_match:
                slide_content = summary_match.group(0)
                title = 'Document Summary'
                # 提取摘要正文：跳过头行（## 摘要），取剩余文本
                body_lines = slide_content.split('\n')[1:]  # skip heading
                summary = ' '.join(
                    l.strip() for l in body_lines
                    if l.strip() and not l.startswith('#') and not l.startswith('>')
                )[:300]
            if not slide_content:
                continue
        elif re.match(r'^Slide\s+\d+', psg['slide']):
            # ppt-extract: 匹配 ## Slide N
            slide_header = f"## {psg['slide']}"
            idx = content.find(slide_header)
            if idx == -1:
                continue
            next_idx = content.find('\n## ', idx + len(slide_header))
            if next_idx == -1:
                slide_content = content[idx:]
            else:
                slide_content = content[idx:next_idx]
        else:
            # web-pdf-extract: 匹配 ## 章节标题
            slide_header = f"## {psg['slide']}"
            idx = content.find(slide_header)
            if idx == -1:
                # 尝试 ### 子标题
                slide_header = f"### {psg['slide']}"
                idx = content.find(slide_header)
            if idx == -1:
                continue
            next_idx = content.find('\n## ', idx + len(slide_header))
            if next_idx == -1:
                next_idx = content.find('\n### ', idx + len(slide_header))
            if next_idx == -1:
                slide_content = content[idx:]
            else:
                slide_content = content[idx:next_idx]

        # ---- 提取摘要（ppt-extract per-slide 格式） ----
        if not summary:
            summary_match = re.search(
                r'> \*?\*?📝\s*摘要\*?\*?[：:]\s*(.+?)(?:\n|$)',
                slide_content
            )
            summary = summary_match.group(1) if summary_match else ''

        # ---- 提取标题（非 Slide 类型用原始 slide 值） ----
        title_match = re.search(r'^## .+$', slide_content, re.MULTILINE)
        if title_match:
            title = title_match.group(0).replace('## ', '')
        elif re.match(r'^Slide\s+\d+', psg['slide']):
            title = psg['slide']

        # ---- 清洗正文 ----
        body = clean_slide_body(slide_content)

        if body.strip():
            result.append({
                'doc': doc_path,
                'slide': psg['slide'],
                'title': title,
                'summary': summary,
                'body': body,
                'entity': psg['entity'],
            })

    # 去重
    seen_keys = set()
    unique = []
    for r in result:
        key = (r['doc'], r['slide'])
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(r)
    return unique


def rerank_passages(
    passages: List[Dict],
    entity_scores: Dict[str, float],
    query_tokens: List[str],
) -> List[Dict]:
    """
    段落级重排序：结合实体匹配分 + 正文关键词命中分。
    score = entity_score * 0.4 + body_keyword_score * 0.6
    """
    scored = []
    for psg in passages:
        entity_id = None
        # 从 provenance 反查 entity_id（实体名 → entity:id）
        for n in [n for n in _cached_graph_nodes() if n.get('canonical_name') == psg['entity']]:
            entity_id = n['id']
            break

        entity_score = entity_scores.get(entity_id, 0.0)

        # 正文关键词命中率
        body_lower = psg.get('body', '').lower()
        hits = sum(1 for t in query_tokens if t.lower() in body_lower)
        body_score = hits / max(len(query_tokens), 1)

        combined = entity_score * 0.4 + body_score * 0.6
        scored.append((combined, psg))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [psg for _, psg in scored]


# 缓存图谱节点（供 rerank 使用）
_cached_nodes = None


def _cached_graph_nodes():
    global _cached_nodes
    if _cached_nodes is None:
        graph = load_graph()
        _cached_nodes = graph['nodes']
    return _cached_nodes


def clean_slide_body(slide_content: str) -> str:
    """清洗幻灯片 Markdown 正文，去掉图片引用和无意义行，保留结构化内容。"""
    lines = []
    skip_patterns = [
        r'^!\[.*\]\(asset/.*\)$',   # 图片引用
        r'^>\s*\*?\*?📝\s*摘要',    # 摘要行（已有 summary 字段）
        r'^>\s*\*?\*?🏷️\s*标签',    # 标签行（ppt-extract）
        r'^>\s*\*{0,2}Tags?\*{0,2}[：:]',   # 标签行（web-pdf-extract: > **Tags**: ...）
        r'^>\s*\*?\*?原始文件',     # 元信息行
        r'^>\s*\*?\*?来源\*?\*?[：:]',  # 来源行（web-pdf-extract: > **来源**: ...）
        r'^---$',                    # 分隔线
    ]
    for line in slide_content.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if any(re.match(p, stripped) for p in skip_patterns):
            continue
        lines.append(line)

    body = '\n'.join(lines).strip()
    # 截断：单页幻灯片正文不超过 3000 字符，防止大模型上下文溢出
    if len(body) > 3000:
        body = body[:3000] + '\n\n[... 内容过长已截断 ...]'
    return body


def search_fulltext(query: str, vault: str, max_results: int = 5) -> List[Dict]:
    """在 vault 所有 .md 文件中全文搜索，支持两种摘要格式。"""
    results = []
    keywords = [t for t in tokenize_query(query) if len(t) >= 2]

    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in files:
            if not fname.endswith('.md'):
                continue
            filepath = os.path.join(root, fname)
            try:
                with open(filepath, 'r') as f:
                    content = f.read()
            except Exception:
                continue

            rel_path = os.path.relpath(filepath, vault)
            matched = False

            # 格式 1: ppt-extract — > 📝 摘要: ...（per-slide blockquote）
            for line in content.split('\n'):
                if '📝 摘要' not in line:
                    continue
                if any(kw.lower() in line.lower() for kw in keywords):
                    m = re.search(r'> \*?\*?📝\s*摘要\*?\*?[：:]\s*(.+)', line)
                    if m:
                        results.append({
                            'doc': rel_path,
                            'text': m.group(1),
                        })
                        matched = True
                        break

            # 格式 2: web-pdf-extract — ## 摘要 章节（document-level）
            if not matched:
                summary_section = re.search(
                    r'## 摘要\s*\n+(.*?)(?=\n(?:---|## |#> ))',
                    content, re.DOTALL
                )
                if summary_section:
                    section_text = summary_section.group(1).strip()
                    if any(kw.lower() in section_text.lower() for kw in keywords):
                        # 截取前 300 字符
                        results.append({
                            'doc': rel_path,
                            'text': section_text[:300],
                        })

    return results[:max_results]


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "请提供查询问题"}, ensure_ascii=False))
        sys.exit(1)

    query = ' '.join(sys.argv[1:])
    graph = load_graph()

    # Step 1: jieba 分词
    tokens = tokenize_query(query)

    # Step 2: 双语别名扩展
    tokens = expand_bilingual(tokens)

    # Step 3: TF-IDF + cosine 实体匹配
    matched, entity_scores = match_entities(graph, tokens, top_k=8)

    # Step 4: 对每个匹配实体找到关联实体（图谱共现边遍历）
    entity_ids = [e['id'] for e in matched]
    all_related = {}
    for eid in entity_ids[:4]:
        related = get_related_entities(graph, eid, top_k=5)
        for name, weight in related:
            if name not in all_related or weight > all_related[name]:
                all_related[name] = weight

    # Step 5: 从原文提取完整幻灯片正文（provenance 定位）
    search_entity_ids = entity_ids[:8]
    passages = extract_passages_from_docs(graph, search_entity_ids, VAULT_PATH)

    # Step 5b: 段落级重排序（实体匹配分 + 正文关键词命中）
    passages = rerank_passages(passages, entity_scores, tokens)
    passages = passages[:8]  # top-8 最相关幻灯片

    # Step 6: 全文搜索补充
    fulltext = search_fulltext(query, VAULT_PATH, max_results=3)

    # 组装输出（JSON schema 不变）
    output = {
        "query": query,
        "matched_entities": [
            {
                "name": e.get('canonical_name', e['id']),
                "type": e['type'],
                "doc_count": len(e.get('provenance', [])),
            }
            for e in matched
        ],
        "related_concepts": sorted(all_related.items(), key=lambda x: x[1], reverse=True)[:10],
        "source_passages": passages,
        "fulltext_matches": fulltext,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
