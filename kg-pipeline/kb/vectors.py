"""
向量召回（bge-m3）。

规模判断：852 段 × 1024 维 × float32 ≈ 3.5MB，**暴力余弦只要 ~2ms**。
所以不引入 sqlite-vec / ANN 索引 —— 直接把矩阵读进 numpy 算一遍，
少一个二进制依赖，行为也更可预测。

页的向量得分 = 该页各编码段相似度的**最大值**（一页通常只有 1 段）。
"""
import sqlite3
from typing import Dict, Optional, Tuple

import numpy as np


def has_vectors(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute('SELECT 1 FROM part_vec LIMIT 1').fetchone() is not None
    except sqlite3.Error:
        return False


def load_index(conn: sqlite3.Connection) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (part_ids, slide_ids, matrix[N, dim])。"""
    rows = conn.execute(
        'SELECT v.part_id, p.slide_id, v.dim, v.vec '
        'FROM part_vec v JOIN slide_parts p ON p.part_id = v.part_id').fetchall()
    if not rows:
        return (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64),
                np.zeros((0, 0), dtype=np.float32))
    dim = rows[0]['dim']
    mat = np.frombuffer(b''.join(r['vec'] for r in rows), dtype=np.float32).reshape(-1, dim)
    part_ids = np.array([r['part_id'] for r in rows], dtype=np.int64)
    slide_ids = np.array([r['slide_id'] for r in rows], dtype=np.int64)
    return part_ids, slide_ids, mat


def slide_scores(conn: sqlite3.Connection, query_vec: np.ndarray) -> Dict[int, float]:
    """查询向量 → {slide_id: 最高段相似度}。查询向量需已 L2 归一化。"""
    _, slide_ids, mat = load_index(conn)
    if mat.size == 0:
        return {}
    q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
    if q.shape[0] != mat.shape[1]:
        return {}
    sims = mat @ q                               # 两边都已归一化 → 点积即余弦
    out: Dict[int, float] = {}
    for sid, s in zip(slide_ids.tolist(), sims.tolist()):
        if s > out.get(sid, -1.0):
            out[sid] = float(s)
    return out


def order(scores: Dict[int, float], top_k: int = 50):
    """按分数降序返回 [(slide_id, score)]。"""
    return sorted(scores.items(), key=lambda x: -x[1])[:top_k]
