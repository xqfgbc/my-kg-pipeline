#!/usr/bin/env python3
"""
建立 / 重建 SQLite 知识库。

用法:
    python kg-pipeline/build_kb.py                 # 建库 + 导出 graph.json 视图
    python kg-pipeline/build_kb.py --no-graph      # 只建库
    python kg-pipeline/build_kb.py --no-parts      # 不生成向量编码段（阶段 4 之前用不到）
    python kg-pipeline/build_kb.py --db <path>     # 指定输出

说明:
    - 全量重建 < 0.1s，因此**不做增量**；每次重来即可。
    - 不修改 vault：只读 .md。
    - graph.json 是导出视图，供 kb-ingest/verify.py 与 dashboard 消费。
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))      # kg-pipeline/
_ROOT = os.path.dirname(_HERE)                          # 仓库根
sys.path.insert(0, _HERE)                               # 为了 import kb.*
sys.path.insert(0, _ROOT)                               # 为了 import kg_pipeline_config

from kb.store import build, export_graph_json          # noqa: E402
from kg_pipeline_config import get_config, get_vault_path  # noqa: E402

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'kg-pipeline-data', 'kb.sqlite')
DEFAULT_GRAPH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'kg-pipeline-data', 'graph.json')


def main():
    ap = argparse.ArgumentParser(description='建立 SQLite 知识库')
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--graph-out', default=DEFAULT_GRAPH)
    ap.add_argument('--no-graph', action='store_true', help='不导出 graph.json')
    ap.add_argument('--no-parts', action='store_true', help='不生成编码段')
    args = ap.parse_args()

    cfg = get_config()
    vault = get_vault_path()
    exclude_dirs = cfg.get('exclude_dirs', []) or []
    exclude_patterns = cfg.get('exclude_patterns', []) or []

    os.makedirs(os.path.dirname(args.db), exist_ok=True)

    print('vault: %s' % vault)
    print('排除目录: %s' % exclude_dirs)
    print('排除模式: %s' % exclude_patterns)
    print('输出: %s' % args.db)
    print()

    stats = build(vault, args.db, exclude_dirs, exclude_patterns,
                  keep_parts=not args.no_parts,
                  exclude_tags=cfg.get('exclude_tags', []) or [])

    print('文档 %d · 页 %d · 编码段 %d · 术语 %d'
          % (stats['docs'], stats['slides'], stats['parts'], stats['terms']))
    if stats.get('vectors_restored'):
        print('向量已从旧库搬运: %d 段（正文未变的段不会重编码）'
              % stats['vectors_restored'])
    print('正文 %s 字符' % format(stats['chars'], ','))
    print('建库 %.2fs · %.2f MB' % (stats['seconds'], stats['db_bytes'] / 1024 / 1024))

    if not args.no_graph:
        g = export_graph_json(args.db, args.graph_out)
        print('graph.json 视图: %d 节点 / %d 边 → %s'
              % (g['nodes'], g['edges'], args.graph_out))


if __name__ == '__main__':
    main()
