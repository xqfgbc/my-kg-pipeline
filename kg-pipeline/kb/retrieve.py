"""
BM25 检索 —— 取代旧的三段式（TF-IDF 实体匹配 + 图遍历 + 摘要行子串扫描）。

输出字段与旧 `retrieval.py` 完全一致，`kg-qa` 无需改动：

    matched_entities : [{name, type, doc_count}]
    related_concepts : [[name, weight], ...]
    source_passages  : [{doc, slide, title, summary, body, entity}]
    fulltext_matches : [{doc, text}]

排序
----
`bm25()` 返回负值（越小越相关）。这里转成正分 `sim = -bm25`，
再乘一个**文档先验**（标题/别名与查询的重合度）作为「两阶段」的第一阶段：

    final = sim * (1 + DOC_PRIOR_WEIGHT * overlap)

用**乘法加权而不是硬过滤**：22 篇文档里硬路由到前 N 篇，
一旦路由错了就再也捞不回来；加权错了只是排序略偏。
"""
import json
import os
import sqlite3
from typing import Dict, List, Optional, Tuple

from .aliases import expand as expand_aliases
from .normalize import fold, normalize_query, normalize_term
from .tokenize import build_match_expr, query_terms

DOC_PRIOR_WEIGHT = 0.5     # 文档先验的权重；0 = 关闭（退化为纯 BM25）
BM25_WEIGHTS = (0.0, 0.0, 10.0, 6.0, 1.0)   # 对应列：doc_id, slide_id, heading, tags, body
CANDIDATE_POOL = 40        # 全局 BM25 候选池

# ---- 两阶段：文档路由 ----
# 查询与某文档的标题/别名重合度 ≥ 该阈值时，**把该文档的页直接拉进候选**，
# 而不是等 BM25 把它们捞上来。实测：No-Regret 那篇的 prior=1.0，
# 但它的页完全不在 BM25 top-40 里 —— 只做重排救不回来。
ROUTE_MIN_PRIOR = 0.6
ROUTE_SLIDES_PER_DOC = 20

# 只在短查询上做别名扩展。
# 实测：逐字长句（任务 B）本来就自带足够词项，再补同义词只会稀释 BM25，
# @1 从 81.8% 掉到 63.6%；而 "MCP"/"可观测" 这类短查询才是扩展真正救得回来的。
EXPAND_MAX_TERMS = 3


def load_kb(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- 文档先验
def _doc_prior(conn: sqlite3.Connection, terms: List[str]) -> Dict[str, float]:
    """每个文档的标题/别名与查询词的重合度，归一化到 [0,1]。"""
    q = set(terms)
    prior: Dict[str, float] = {}
    for row in conn.execute('SELECT doc_id, title, aliases FROM docs'):
        text = ' '.join([row['title'] or ''] + json.loads(row['aliases'] or '[]'))
        dterms = set(t.lower() for t in query_terms(text))
        if not dterms:
            continue
        overlap = len(q & dterms) / max(len(q), 1)
        if overlap:
            prior[row['doc_id']] = overlap
    return prior


# ---------------------------------------------------------------- 主检索
def _bm25(conn: sqlite3.Connection, terms: List[str], limit: int) -> List[sqlite3.Row]:
    expr = build_match_expr(terms)
    if not expr:
        return []
    sql = (
        'SELECT s.slide_id, s.doc_id, s.heading, s.body, s.summary, s.tags, '
        '       d.rel_path, bm25(slides_fts, ?, ?, ?, ?, ?) AS score '
        'FROM slides_fts f '
        'JOIN slides s ON s.slide_id = f.rowid '
        'JOIN docs d   ON d.doc_id = s.doc_id '
        'WHERE slides_fts MATCH ? '
        'ORDER BY score LIMIT ?')
    return conn.execute(sql, (*BM25_WEIGHTS, expr, limit)).fetchall()


def _route_docs(prior: Dict[str, float]) -> List[str]:
    """标题/别名与查询高度重合的文档 —— 第一阶段的输出。"""
    if not prior:
        return []
    top = max(prior.values())
    thresh = max(ROUTE_MIN_PRIOR, top * 0.8)
    return [d for d, s in prior.items() if s >= thresh]


def _slides_of_docs(conn: sqlite3.Connection, doc_ids: List[str],
                    per_doc: int) -> List[sqlite3.Row]:
    """取出被路由文档的页（这些页可能完全不含查询词，但仍然是对的答案）。"""
    if not doc_ids:
        return []
    marks = ','.join('?' * len(doc_ids))
    sql = (
        'SELECT s.slide_id, s.doc_id, s.heading, s.body, s.summary, s.tags, '
        '       d.rel_path, s.seq, 0.0 AS score '
        'FROM slides s JOIN docs d ON d.doc_id = s.doc_id '
        'WHERE s.doc_id IN (%s) ORDER BY s.seq' % marks)
    rows = conn.execute(sql, tuple(doc_ids)).fetchall()
    out, per = [], {}
    for r in rows:
        if per.get(r['doc_id'], 0) >= per_doc:
            continue
        per[r['doc_id']] = per.get(r['doc_id'], 0) + 1
        out.append(r)
    return out


def _slides_by_ids(conn: sqlite3.Connection, ids: List[int]) -> List[sqlite3.Row]:
    if not ids:
        return []
    marks = ','.join('?' * len(ids))
    return conn.execute(
        'SELECT s.slide_id, s.doc_id, s.heading, s.body, s.summary, s.tags, '
        '       d.rel_path, s.seq, 0.0 AS score '
        'FROM slides s JOIN docs d ON d.doc_id = s.doc_id '
        'WHERE s.slide_id IN (%s)' % marks, tuple(ids)).fetchall()


# RRF 的票权。bge-m3 的相似度区间被压缩（相关 ~0.74 / 无关 ~0.42），
# 若两种召回等权，向量的弱排序会把 BM25 的强结果压下去 —— 实测等权时
# 任务 B @1 从 81.8% 掉到 40.9%。所以词法为主、向量为辅。
RRF_K = 60
BM25_WEIGHT = 1.0
VEC_WEIGHT = 0.3
VEC_CANDIDATES = 30


def _norm(d: Dict[int, float]) -> Dict[int, float]:
    if not d:
        return {}
    lo, hi = min(d.values()), max(d.values())
    if hi <= lo:
        return {k: 1.0 for k in d}
    return {k: (v - lo) / (hi - lo) for k, v in d.items()}


def _fuse(bm_rows: List[sqlite3.Row], vec_scores: Dict[int, float]) -> List[int]:
    """分数归一化后的加权融合。

    ⚠️ 不用 RRF：RRF 只看名次，一个向量 rank-1 的票就足以掀翻 BM25 的
    rank-1/rank-2（两者只差 0.0003），实测任务 B @1 从 81.8% 掉到 40.9%。
    归一化后按分数相加，向量只能**补 BM25 没召回的**，动不了 BM25 内部次序。
    """
    nb = _norm({r['slide_id']: -float(r['score']) for r in bm_rows})
    nv = _norm(vec_scores)
    ids = set(nb) | set(nv)
    return sorted(ids, key=lambda i: -(nb.get(i, 0.0) + VEC_WEIGHT * nv.get(i, 0.0)))


_EMBEDDER = None


def _embedder():
    """延迟加载 —— 没有向量索引时根本不碰模型。"""
    global _EMBEDDER
    if _EMBEDDER is None:
        from .embed import Embedder
        _EMBEDDER = Embedder()
    return _EMBEDDER


def _rank(rows: List[sqlite3.Row], prior: Dict[str, float]) -> List[Tuple[float, sqlite3.Row]]:
    out = []
    for r in rows:
        sim = -float(r['score'])                     # 转成正分
        sim *= 1.0 + DOC_PRIOR_WEIGHT * prior.get(r['doc_id'], 0.0)
        out.append((sim, r))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


# ---------------------------------------------------------------- 摘录
def snippet(body: str, terms: List[str], width: int = 70) -> str:
    """围绕首个命中词截一段，供 fulltext_matches 使用。"""
    if not body:
        return ''
    low = body.lower()
    for t in terms:
        i = low.find(t.lower())
        if i >= 0:
            s, e = max(0, i - width), min(len(body), i + len(t) + width)
            return ('…' if s else '') + body[s:e].replace('\n', ' ').strip() + ('…' if e < len(body) else '')
    return body[:140].replace('\n', ' ').strip()


# ---------------------------------------------------------------- 实体 / 关联
def _tags_of(row) -> List[str]:
    try:
        return json.loads(row['tags'] or '[]')
    except Exception:
        return []


def _matched_entities(conn: sqlite3.Connection, terms: List[str],
                      ranked: List[Tuple[float, sqlite3.Row]]) -> List[Dict]:
    """在【术语表】里找与查询匹配的术语。

    术语表在建库时已做过归一化与禁词过滤，所以这里不会再冒出
    `导航`/`章节页`/`Accenture内部` 这类词。
    """
    qkeys = [k for k in (normalize_term(t) for t in terms) if len(k) >= 2]
    if not qkeys:
        return []

    hits = []
    for row in conn.execute('SELECT name, doc_count FROM terms'):
        key = normalize_term(row['name'])
        if any(q == key or q in key or key in q for q in qkeys):
            hits.append((row['doc_count'], row['name']))

    hits.sort(key=lambda x: (-x[0], len(x[1])))
    return [{'name': n, 'type': 'entity', 'doc_count': d} for d, n in hits[:8]]


def _related_concepts(conn: sqlite3.Connection,
                      ranked: List[Tuple[float, sqlite3.Row]]) -> List[List]:
    """同页共现 —— 证据可回溯到具体页（旧图是文档级，98% 只有单篇支持）。"""
    ids = [r['slide_id'] for _, r in ranked[:12]]
    if not ids:
        return []
    marks = ','.join('?' * len(ids))
    rows = conn.execute(
        'SELECT t.name, COUNT(*) AS c FROM term_slides ts '
        'JOIN terms t ON t.term_id = ts.term_id '
        'WHERE ts.slide_id IN (%s) GROUP BY t.term_id '
        'ORDER BY c DESC, t.name LIMIT 10' % marks, tuple(ids)).fetchall()
    if not rows:
        return []
    top = rows[0]['c']
    return [[r['name'], round(r['c'] / top, 2)] for r in rows]


# ---------------------------------------------------------------- 对外入口
def search(conn: sqlite3.Connection, query: str, top_k: int = 8) -> Dict:
    """返回与旧 retrieval.py 同构的结果字典。"""
    normalized = normalize_query(query)
    raw_terms = query_terms(normalized)
    if not raw_terms:
        return {'matched_entities': [], 'related_concepts': [],
                'source_passages': [], 'fulltext_matches': []}

    # 别名扩展：把「可观测」→「可观测性」、「MCP」→「Model Context Protocol」这类
    # 写法差异补上。只加召回，不改实体身份。
    terms = expand_aliases(raw_terms) if len(raw_terms) <= EXPAND_MAX_TERMS else raw_terms
    if not terms:
        return {'matched_entities': [], 'related_concepts': [],
                'source_passages': [], 'fulltext_matches': []}

    # ⚠️ 文档路由必须用【用户原话】算，不能用扩展后的词集：
    #    同义词会把 overlap 的分母撑大，0.6 的阈值再也达不到，路由直接失效
    #    （实测：用扩展词集时任务 A 从 100% 掉到 50%）。
    prior = _doc_prior(conn, raw_terms)

    # ---- 阶段 1：文档路由（把标题命中的文档的页直接拉进候选）----
    routed_docs = _route_docs(prior)
    global_rows = _bm25(conn, terms, CANDIDATE_POOL)
    routed_rows = _slides_of_docs(conn, routed_docs, ROUTE_SLIDES_PER_DOC)

    # ---- 阶段 2：合并排序 ----
    # Tier 1 = 路由到的文档的页：按 (文档重合度, 该页 BM25, 页序)
    # Tier 2 = 其余 BM25 命中：按 sim × 文档先验加权
    score_by_slide = {r['slide_id']: -float(r['score']) for r in global_rows}
    tier1 = sorted(routed_rows,
                   key=lambda r: (-prior.get(r['doc_id'], 0.0),
                                  -score_by_slide.get(r['slide_id'], 0.0),
                                  r['seq']))
    ids = {r['slide_id'] for r in tier1}
    tier2 = [r for r in global_rows if r['slide_id'] not in ids]

    # ---- 向量召回 + RRF 融合（有索引时才启用）----
    ordered = tier1 + [r for _, r in _rank(tier2, prior)]
    # ⚠️ 默认**关闭**。实测（22 篇 / 798 页 / 852 段）：
    #    在可自动度量的两个任务上，向量单独 @1 只有 36.4%（B）/ 63.6%（C），
    #    而纯 BM25 是 81.8% / 95.5%；任何非零融合权重都只会拉低结果。
    #    但在**改过说法**的查询上向量明显更准（见 docs/KB_IMPROVEMENT_PLAN.md）。
    #    所以做成显式开关：KB_VECTORS=1 时启用。
    if os.environ.get('KB_VECTORS', '0') == '1':
        try:
            from . import vectors as V
            if V.has_vectors(conn):
                qv = _embedder().encode([normalized])[0]
                vs = V.slide_scores(conn, qv)
                if vs:
                    merged = _fuse(tier2, dict(V.order(vs, VEC_CANDIDATES)))
                    by_id = {r['slide_id']: r for r in tier2}
                    missing = [sid for sid in merged if sid not in by_id]
                    for r in _slides_by_ids(conn, missing):
                        by_id[r['slide_id']] = r
                    tier1_ids = {r['slide_id'] for r in tier1}
                    ordered = tier1 + [by_id[s] for s in merged
                                       if s in by_id and s not in tier1_ids]
        except Exception:
            # 模型缺失 / 维度不符 / ONNX 加载失败 —— 一律退回纯词法
            if os.environ.get('KB_STRICT'):
                raise

    ranked = [(0.0, r) for r in ordered]

    qfold = [fold(t) for t in terms]

    passages = []
    for pos, (_, r) in enumerate(ranked[:top_k]):
        tags = _tags_of(r)
        entity = next((t for t in tags
                       if any(fold(t) == q or q in fold(t) for q in qfold)), '')
        passages.append({
            'doc': r['rel_path'],
            'slide': r['heading'],
            'title': r['heading'],
            'summary': r['summary'] or '',
            'body': r['body'] or '',
            'entity': entity,
        })

    # 补充命中：rank 之外的页，只给片段
    fulltext = [{'doc': r['rel_path'], 'text': snippet(r['body'] or '', terms)}
                for _, r in ranked[top_k:top_k + 3]]

    return {
        'matched_entities': _matched_entities(conn, terms, ranked),
        'related_concepts': _related_concepts(conn, ranked),
        'source_passages': passages,
        'fulltext_matches': fulltext,
    }
