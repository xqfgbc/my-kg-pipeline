#!/usr/bin/env python3
"""
从 vault 里挖「全称（缩写）」对，产出 aliases.mined.yaml。

只认一种模式：**英文全称 + 括号缩写**，且必须通过首字母校验。

    Model Context Protocol (MCP)          ✅
    Operationalize-Accelerate-Modernize (OAM)  ✅
    同属企业技术蓝图 (ETB)                  ❌ 中文模式噪声太大，实测会捞出句子碎片

实测：当前 22 篇语料可挖出 141 对可信候选。
产出文件**不自动生效** —— 需要人工过一眼，再决定是否并入 aliases.curated.yaml。

用法:
    python kg-pipeline/mine_aliases.py [--min-count 2]
"""
import argparse
import os
import re
import sys
from collections import Counter

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kg_pipeline_config import get_config, get_vault_path   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aliases.mined.yaml')

# 英文全称（2+ 个词）后跟括号内的全大写缩写
PAIR = re.compile(
    r'\b([A-Z][A-Za-z0-9]*(?:[\s\-][A-Za-z0-9]+){1,6})\s*[（(]\s*([A-Z][A-Z0-9/&\-]{1,7})\s*[)）]')

# 全称里不允许出现的噪声词（这些是句子片段而不是术语）
STOPWORDS = {'the', 'and', 'for', 'with', 'from', 'this', 'that', 'our', 'your'}


def initials_match(full: str, abbr: str) -> bool:
    """缩写的每个字母都应出现在全称各词的首字母序列里。"""
    words = re.findall(r'[A-Za-z0-9]+', full)
    if len(words) < 2:
        return False
    seq = ''.join(w[0] for w in words).upper()
    # 允许跳过虚词：把缩写的字母按顺序在 seq 里匹配
    i = 0
    for ch in abbr:
        if ch in '/&-':
            continue
        j = seq.find(ch, i)
        if j < 0:
            return False
        i = j + 1
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--min-count', type=int, default=2)
    args = ap.parse_args()

    vault = get_vault_path()
    cfg = get_config()
    from kg_pipeline_config import scan_vault_md_files
    files = scan_vault_md_files(vault, cfg.get('exclude_dirs', []) or [],
                                cfg.get('exclude_patterns', []) or [])

    pairs = Counter()
    for rel_path, abs_path in files.items():
        try:
            text = open(abs_path, encoding='utf-8', errors='ignore').read()
        except Exception:
            continue
        for m in PAIR.finditer(text):
            full, abbr = m.group(1).strip(), m.group(2).strip()
            if not initials_match(full, abbr):
                continue
            # ⚠️ 用正则数词，不能用 split()：
            #    `Operationalize-Accelerate-Modernize` 是纯连字符写法，
            #    split() 只切出 1 个词，会把它误判为「不是词组」而丢掉。
            words = re.findall(r'[A-Za-z0-9]+', full)
            if len(words) < 2 or len(full) < 6:
                continue
            if words[0].lower() in STOPWORDS:
                continue
            pairs[(full, abbr)] += 1

    # 同一缩写指向多个全称时，只保留出现最多的那个（避免一缩写多义）
    best = {}
    for (full, abbr), n in pairs.items():
        if n < args.min_count:
            continue
        if abbr not in best or n > best[abbr][1]:
            best[abbr] = (full, n)

    data = {abbr: full for abbr, (full, n) in sorted(best.items())}
    header = (
        "# 自动挖掘的「全称 → 缩写」对（**待人工审核**）\n"
        "#\n"
        "# 来源：vault 里的 `Full Name (ABBR)` 模式 + 首字母校验。\n"
        "# 这个文件**不会被 kb 层自动加载**；审核后把确认的条目并入 aliases.curated.yaml。\n"
        "# 重新生成：python kg-pipeline/mine_aliases.py\n\n")
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(header)
        yaml.safe_dump({'abbreviations': data}, f, allow_unicode=True, sort_keys=True)

    print('候选 %d 对（出现次数 ≥%d）→ %s' % (len(data), args.min_count, OUT))
    for abbr, full in list(data.items())[:20]:
        print('   %-8s ← %s' % (abbr, full))


if __name__ == '__main__':
    main()
