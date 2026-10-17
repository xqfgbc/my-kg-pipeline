"""
SQLite 知识库：schema、建库、导出兼容视图。

设计要点
--------
1. **FTS5 独立表**（不设 content=），显式写入 rowid = slide_id。
   省掉同步触发器；全量重建 <0.1s，根本不需要增量逻辑。
2. **两套正文**：`slides.body` 存原文（用于输出与片段），
   `slides_fts.body` 存 jieba 预分词结果（用于匹配）。
3. **向量预留**：`slide_parts` + `part_vec`（bge-m3，1024 维）。
   本阶段不写入，表结构先建好，阶段 4 直接灌。
4. **graph.json 保留为导出视图** —— kb-ingest/verify.py 与 dashboard 仍读它。
"""
import json
import os
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from .chunker import parse_document, scan_documents
from .tokenize import to_index_text

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE docs(
  doc_id       TEXT PRIMARY KEY,
  rel_path     TEXT NOT NULL,          -- 相对 vault，如 `<目录>/<文件>.md`（kg-qa 拼链接用）
  title        TEXT NOT NULL,
  aliases      TEXT NOT NULL DEFAULT '[]',
  doc_type     TEXT,
  source       TEXT,
  pages        INT DEFAULT 0,
  content_hash TEXT,
  updated_at   TEXT
);

CREATE TABLE slides(
  slide_id     INTEGER PRIMARY KEY,
  doc_id       TEXT NOT NULL,          -- 逻辑外键：docs.doc_id
  seq          INT,
  heading      TEXT,
  heading_path TEXT,
  body         TEXT,
  tags         TEXT NOT NULL DEFAULT '[]',
  summary      TEXT,
  chars        INT,
  kind         TEXT,                   -- slide / summary / other
  slide_no     INT
);
CREATE INDEX idx_slides_doc ON slides(doc_id);

-- 页级全文索引。列序固定：bm25 的权重参数按此顺序给。
CREATE VIRTUAL TABLE slides_fts USING fts5(
  doc_id UNINDEXED,
  slide_id UNINDEXED,
  heading,
  tags,
  body,
  tokenize='unicode61'
);

-- 编码段（阶段 4 用）。一页通常 1 段，仅超长页拆 2–3 段。
CREATE TABLE slide_parts(
  part_id    INTEGER PRIMARY KEY,
  slide_id   INT NOT NULL,
  ord        INT,
  text       TEXT,
  char_start INT,
  char_end   INT,
  model      TEXT
);
CREATE INDEX idx_parts_slide ON slide_parts(slide_id);

-- 向量（bge-m3，1024 维，float32 小端 BLOB）。
-- 852 段 × 4KB ≈ 3.4MB，暴力余弦只要 ~2ms，不需要 ANN 索引，也不需要 sqlite-vec。
CREATE TABLE part_vec(
  part_id INTEGER PRIMARY KEY,
  dim     INT,
  vec     BLOB
);

-- 术语（阶段 3 填充）
CREATE TABLE terms(
  term_id     INTEGER PRIMARY KEY,
  name        TEXT UNIQUE NOT NULL,
  kind        TEXT,
  doc_count   INT DEFAULT 0,
  slide_count INT DEFAULT 0
);
CREATE TABLE term_slides(
  term_id  INT NOT NULL,
  slide_id INT NOT NULL,
  weight   REAL DEFAULT 1.0,
  PRIMARY KEY(term_id, slide_id)
);
CREATE TABLE term_aliases(
  alias   TEXT PRIMARY KEY,
  term_id INT,
  source  TEXT                          -- curated / acronym / normalized
);

CREATE TABLE meta(
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def connect(db_path: str, create: bool = False) -> sqlite3.Connection:
    if not create and not os.path.exists(db_path):
        raise FileNotFoundError(
            '知识库不存在：%s\n请先运行 python kg-pipeline/build_kb.py' % db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _populate_terms(conn: sqlite3.Connection, exclude_tags: List[str]) -> int:
    """从页标签构建术语表。

    - **归一化合并**：`AI Agent` 与 `AIagent` 归一到同一个 term
      （语料里实测 23 组纯拼写变体，旧图里各占一个节点）
    - **禁词过滤**：`exclude_tags` 用子串匹配，在建库时就排除
      （旧管道只在回写 frontmatter 时过滤，图里留了 7 个禁用节点）
    """
    from .normalize import normalize_term

    banned = [t.lower() for t in (exclude_tags or [])]
    banned += ['🔗']                      # 结构章节残留
    def is_banned(s: str) -> bool:
        low = s.lower()
        return any(b in low for b in banned)

    groups = {}                           # norm_key -> {surfaces:Counter, slides:set, docs:set}
    for row in conn.execute('SELECT slide_id, doc_id, tags FROM slides'):
        try:
            tags = json.loads(row['tags'] or '[]')
        except json.JSONDecodeError:
            # 只吞「存进去的不是合法 JSON」这一种情况。
            # 早先这里是 `except Exception: continue`，把 row_factory 缺失导致的
            # TypeError 也一起吞了 —— 结果 798 页全部静默跳过，术语数为 0。
            continue
        for tag in tags:
            if not isinstance(tag, str) or is_banned(tag):
                continue
            key = normalize_term(tag)
            if len(key) < 2:
                continue
            g = groups.setdefault(key, {'surfaces': {}, 'slides': set(), 'docs': set()})
            g['surfaces'][tag] = g['surfaces'].get(tag, 0) + 1
            g['slides'].add(row['slide_id'])
            g['docs'].add(row['doc_id'])

    for key, g in groups.items():
        # 规范名取出现最多的写法；并列时取最短
        name = sorted(g['surfaces'].items(), key=lambda x: (-x[1], len(x[0])))[0][0]
        cur = conn.execute(
            'INSERT INTO terms(name,kind,doc_count,slide_count) VALUES(?,?,?,?)',
            (name, 'concept', len(g['docs']), len(g['slides'])))
        tid = cur.lastrowid
        conn.executemany(
            'INSERT OR IGNORE INTO term_slides(term_id,slide_id,weight) VALUES(?,?,1.0)',
            [(tid, sid) for sid in g['slides']])
        conn.executemany(
            'INSERT OR IGNORE INTO term_aliases(alias,term_id,source) VALUES(?,?,?)',
            [(surface, tid, 'tag') for surface in g['surfaces']])
    return len(groups)


def _load_existing_vectors(db_path: str) -> Dict:
    """读旧库里的向量，键 = (doc_id, seq, ord, 正文 md5)。

    为什么需要：build() 是「删库重建」，若不做搬运，每次重建都会把
    852 条 bge-m3 向量一起清掉 —— 重编码要 10 分钟，而且没有任何提示。
    只要正文没变，重建后就能原样搬回来。
    """
    import hashlib
    if not os.path.exists(db_path):
        return {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT v.dim, v.vec, v.part_id, s.doc_id, s.seq, p.ord, p.text '
            'FROM part_vec v '
            'JOIN slide_parts p ON p.part_id = v.part_id '
            'JOIN slides s ON s.slide_id = p.slide_id').fetchall()
        out = {}
        for r in rows:
            key = (r['doc_id'], r['seq'], r['ord'],
                   hashlib.md5((r['text'] or '').encode('utf-8')).hexdigest())
            out[key] = (r['dim'], r['vec'])
        conn.close()
        return out
    except sqlite3.Error:
        return {}


def build(vault: str, db_path: str, exclude_dirs: List[str],
          exclude_patterns: List[str], keep_parts: bool = True,
          exclude_tags: Optional[List[str]] = None) -> Dict:
    """全量重建知识库。返回统计信息。"""
    t0 = time.time()
    carried = _load_existing_vectors(db_path)      # 必须在删库之前读

    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row      # _populate_terms 按列名取值，必须有
    conn.executescript(SCHEMA)

    docs, slides, parts = [], [], []
    next_slide_id = 1
    next_part_id = 1

    for doc_id, rel_path, abs_path in scan_documents(vault, exclude_dirs, exclude_patterns):
        doc, doc_slides = parse_document(abs_path, doc_id, rel_path)
        doc['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        docs.append(doc)
        for s in doc_slides:
            s['slide_id'] = next_slide_id
            next_slide_id += 1
            slides.append(s)
            if keep_parts:
                for ord_, (text, cs, ce) in enumerate(s['parts']):
                    parts.append((next_part_id, s['slide_id'], ord_, text, cs, ce))
                    next_part_id += 1

    conn.executemany(
        'INSERT INTO docs(doc_id,rel_path,title,aliases,doc_type,source,pages,content_hash,updated_at)'
        ' VALUES(?,?,?,?,?,?,?,?,?)',
        [(d['doc_id'], d['rel_path'], d['title'], json.dumps(d['aliases'], ensure_ascii=False),
          d['doc_type'], d['source'], d['pages'], d['content_hash'], d['updated_at'])
         for d in docs])

    conn.executemany(
        'INSERT INTO slides(slide_id,doc_id,seq,heading,heading_path,body,tags,summary,chars,kind,slide_no)'
        ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        [(s['slide_id'], s['doc_id'], s['seq'], s['heading'], s['heading_path'],
          s['body'], json.dumps(s['tags'], ensure_ascii=False), s['summary'],
          s['chars'], s['kind'], s['slide_no']) for s in slides])

    # FTS：rowid = slide_id，正文存 jieba 分词结果
    conn.executemany(
        'INSERT INTO slides_fts(rowid,doc_id,slide_id,heading,tags,body) VALUES(?,?,?,?,?,?)',
        [(s['slide_id'], s['doc_id'], s['slide_id'],
          to_index_text(s['heading']), to_index_text(' '.join(s['tags'])),
          to_index_text(s['body'])) for s in slides])

    if keep_parts:
        conn.executemany(
            'INSERT INTO slide_parts(part_id,slide_id,ord,text,char_start,char_end,model)'
            ' VALUES(?,?,?,?,?,?,NULL)',
            [(p[0], p[1], p[2], p[3], p[4], p[5]) for p in parts])

    # 把没变的段的向量搬回来（正文 md5 相同才算），避免重建时白丢 10 分钟编码
    restored = 0
    if keep_parts and carried:
        import hashlib
        try:
            from .embed import MODEL_TAG
        except Exception:
            MODEL_TAG = None
        slide_key = {s['slide_id']: (s['doc_id'], s['seq']) for s in slides}
        for pid, sid, ord_, text, _cs, _ce in parts:
            dk = slide_key.get(sid)
            if not dk:
                continue
            key = (dk[0], dk[1], ord_,
                   hashlib.md5((text or '').encode('utf-8')).hexdigest())
            hit = carried.get(key)
            if not hit:
                continue
            conn.execute('INSERT OR REPLACE INTO part_vec(part_id,dim,vec) VALUES(?,?,?)',
                         (pid, hit[0], hit[1]))
            if MODEL_TAG:
                conn.execute('UPDATE slide_parts SET model=? WHERE part_id=?',
                             (MODEL_TAG, pid))
            restored += 1

    n_terms = _populate_terms(conn, exclude_tags or [])

    conn.executemany('INSERT INTO meta(key,value) VALUES(?,?)', [
        ('schema_version', '2'),
        ('built_at', time.strftime('%Y-%m-%dT%H:%M:%S')),
        ('vault', vault),
        ('max_part_chars', '4000'),
    ])
    conn.commit()

    stats = {
        'docs': len(docs),
        'slides': len(slides),
        'parts': len(parts),
        'terms': n_terms,
        'vectors_restored': restored,
        'chars': sum(s['chars'] for s in slides),
        'db_bytes': os.path.getsize(db_path),
        'seconds': round(time.time() - t0, 2),
        'excluded_heads': 0,
    }
    conn.close()
    return stats


def export_graph_json(db_path: str, out_path: str) -> Dict:
    """导出 graph.json 兼容视图。

    只含文档节点与「文档 → 页」的结构边 —— 足以让
    kb-ingest/verify.py（文档节点计数）与 convert_for_dashboard.py（要求边有 source/target）
    继续工作，同时不再携带 313k 条共现噪声。
    """
    conn = connect(db_path)
    docs = conn.execute(
        'SELECT doc_id,rel_path,title,aliases FROM docs ORDER BY doc_id').fetchall()
    slides = conn.execute(
        'SELECT s.slide_id, s.heading, d.rel_path FROM slides s '
        'JOIN docs d ON d.doc_id = s.doc_id ORDER BY s.slide_id').fetchall()

    # ⚠️ 节点 id 必须用 rel_path 而非 doc_id：
    #    kb-ingest/verify.py:145-149 断言 `doc:<vault 相对路径>` ∈ 图文档节点
    nodes = [{
        'id': 'doc:%s' % d['rel_path'],
        'type': 'document',
        'title': d['title'],
        'aliases': json.loads(d['aliases'] or '[]'),
    } for d in docs]

    edges = []
    for s in slides:
        nodes.append({
            'id': 'slide:%d' % s['slide_id'],
            'type': 'heading',
            'title': s['heading'],
        })
        edges.append({
            'source': 'doc:%s' % s['rel_path'],
            'target': 'slide:%d' % s['slide_id'],
            'type': 'contains',
            'weight': 1.0,
            'provenance': [{'file': s['rel_path'], 'form': 'slide'}],
        })

    payload = {
        'nodes': nodes,
        'edges': edges,
        'stats': {'total_nodes': len(nodes), 'total_edges': len(edges)},
        'metadata': {
            'source': 'kb.sqlite',
            'note': 'graph.json 现为导出视图；主存储见 kb-pipeline-data/kb.sqlite',
        },
    }
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    conn.close()
    return {'nodes': len(nodes), 'edges': len(edges)}
