#!/usr/bin/env python3
"""
为 slide_parts 编码向量（bge-m3，本地 ONNX）。

用法:
    python kg-pipeline/build_vectors.py              # 全量编码未编的段
    python kg-pipeline/build_vectors.py --limit 8    # 先测 8 段，量一下速度
    python kg-pipeline/build_vectors.py --force      # 无视 model 标记，全部重编

设计:
  - 编码文本 = 「文档标题 > 幻灯片标题」 + 正文（上下文头，成本很低）
  - 只编 `slide_parts.model` 与当前模型名不一致的段 —— 增量天然成立
  - 向量以 float32 BLOB 存进 part_vec；检索侧一次读进 numpy 做暴力余弦
"""
import argparse
import os
import sqlite3
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kb.embed import Embedder, DEFAULT_MODEL_DIR, MODEL_TAG   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'kg-pipeline-data', 'kb.sqlite')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DB)
    ap.add_argument('--model-dir', default=DEFAULT_MODEL_DIR)
    ap.add_argument('--batch-size', type=int, default=4)
    ap.add_argument('--limit', type=int, default=0, help='只编码前 N 段（测速用）')
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit('知识库不存在：%s\n请先运行 build_kb.py' % args.db)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    where = '' if args.force else "WHERE p.model IS NULL OR p.model != '%s'" % MODEL_TAG
    limit = ' LIMIT %d' % args.limit if args.limit else ''
    rows = conn.execute(
        'SELECT p.part_id, p.text, s.heading, d.title '
        'FROM slide_parts p '
        'JOIN slides s ON s.slide_id = p.slide_id '
        'JOIN docs d ON d.doc_id = s.doc_id '
        '%s ORDER BY p.part_id%s' % (where, limit)).fetchall()

    if not rows:
        print('没有待编码的段（模型标记已是 %s）' % MODEL_TAG)
        return

    texts = ['%s > %s\n%s' % (r['title'] or '', r['heading'] or '', r['text'] or '')
             for r in rows]
    print('待编码 %d 段，加载模型…' % len(rows))
    t0 = time.time()
    emb = Embedder(args.model_dir)
    print('模型就绪（%.1fs），维度 %d' % (time.time() - t0, emb.dim))

    t0 = time.time()
    vecs = []
    for i in range(0, len(texts), args.batch_size):
        chunk = texts[i:i + args.batch_size]
        vecs.append(emb.encode(chunk, batch_size=args.batch_size))
        done = min(i + args.batch_size, len(texts))
        rate = done / max(time.time() - t0, 1e-6)
        eta = (len(texts) - done) / max(rate, 1e-6)
        print('  %4d/%d  %.1f 段/s  预计剩余 %.0fs' % (done, len(texts), rate, eta),
              flush=True)

    mat = np.vstack(vecs).astype(np.float32)
    print('编码完成：%s，用时 %.0fs' % (mat.shape, time.time() - t0))

    conn.executemany(
        'INSERT OR REPLACE INTO part_vec(part_id, dim, vec) VALUES(?,?,?)',
        [(rows[i]['part_id'], int(mat.shape[1]), mat[i].tobytes())
         for i in range(len(rows))])
    conn.executemany('UPDATE slide_parts SET model=? WHERE part_id=?',
                     [(MODEL_TAG, r['part_id']) for r in rows])
    conn.commit()

    size = conn.execute('SELECT SUM(LENGTH(vec)) FROM part_vec').fetchone()[0] or 0
    print('写入 part_vec：%.2f MB；总库 %.2f MB'
          % (size / 1024 / 1024, os.path.getsize(args.db) / 1024 / 1024))
    conn.close()


if __name__ == '__main__':
    main()
