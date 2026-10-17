"""
jieba 预分词 —— FTS5 的 unicode61 分词器不切中文，必须先把中文切成空格分隔的 token。

写入侧（建库）与查询侧（retrieval）必须用同一套规则，否则查不到。
"""
import re
from typing import Iterable, List

import jieba

# 中文之外还需要保留的东西：英文词（含数字/连字符）、缩写、版本号
_ASCII_WORD = re.compile(r'[A-Za-z][A-Za-z0-9\-/&\._]*')
_CJK = re.compile(r'[一-鿿]')

# 纯标点 / 单个符号 —— 不进入索引
_PUNCT_ONLY = re.compile(r'^[\W_]+$', re.UNICODE)


def _keep(token: str) -> bool:
    token = token.strip()
    if not token or _PUNCT_ONLY.match(token):
        return False
    # 单字符中文（的/和/是…）不进索引：噪声大、无区分度
    if len(token) == 1 and _CJK.match(token):
        return False
    return True


def tokens(text: str) -> List[str]:
    """把一段文本切成 token 列表（去重保序）。"""
    if not text:
        return []
    out: List[str] = []
    seen = set()
    # jieba 负责中文；英文词由正则兜住，避免 jieba 把 "MCP/A2A" 切碎
    for tok in jieba.cut(text):
        tok = tok.strip()
        if not _keep(tok):
            continue
        for sub in (_ASCII_WORD.findall(tok) or [tok]):
            sub = sub.lower().strip()
            if _keep(sub) and sub not in seen:
                seen.add(sub)
                out.append(sub)
    return out


def to_index_text(text: str) -> str:
    """写入 FTS5 的形态：空格分隔的 token 串。"""
    return ' '.join(tokens(text))


def query_terms(query: str) -> List[str]:
    """查询侧 token —— 与写入侧同源，保证可命中。"""
    return tokens(query)


def quote_fts(term: str) -> str:
    """把 token 包成 FTS5 字符串字面量（防特殊字符破坏查询语法）。"""
    return '"%s"' % term.replace('"', '""')


def build_match_expr(terms: Iterable[str], operator: str = ' OR ') -> str:
    """构造 MATCH 表达式。

    ⚠️ FTS5 对空格分隔的词默认取 AND，长查询会几乎全空；
    检索侧一律用 OR，让 bm25 去做排序。
    """
    parts = [quote_fts(t) for t in dict.fromkeys(terms) if t and len(t) >= 1]
    return operator.join(parts)
