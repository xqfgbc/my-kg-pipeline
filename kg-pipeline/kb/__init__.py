"""
kb —— 知识库层（SQLite 单文件）。

与旧的 graph/ 包并列，不互相依赖：
  graph/  旧的 networkx 图模型（保留，逐步退为导出视图）
  kb/     新的存储、切片、分词与检索

模块职责：
  normalize.py  术语/查询归一化（NFKC、大小写、分隔符）
  tokenize.py   jieba 预分词（FTS5 用）
  chunker.py    Markdown → 文档 / 页 / 编码段
  store.py      SQLite schema、建库、导出 graph.json 兼容视图
"""
