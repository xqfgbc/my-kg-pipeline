#!/usr/bin/env python3
"""
检查知识库文件变更状态。
对比 vault 中 .md 文件与 index_state.json 记录，
列出新增、已修改、已删除和未变化的文件。
"""
import os
import sys
import json
import hashlib

# 动态加载本地配置
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PROJECT_ROOT)
from kg_pipeline_config import get_config, get_vault_path, scan_vault_md_files

VAULT_PATH = get_vault_path()
STATE_FILE = os.path.join(_PROJECT_ROOT, "kg-pipeline-data", "index_state.json")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}


def scan_vault():
    # 复用 pipeline 的扫描逻辑（含 exclude_dirs / exclude_patterns）。
    # 两边必须扫出同一个文件集，否则会出现「check 说有变更、管道却无文件可处理」。
    config = get_config()
    md_files = scan_vault_md_files(
        VAULT_PATH,
        config.get('exclude_dirs', []),
        config.get('exclude_patterns', []),
    )

    files = {}
    for rel_path, abs_path in md_files.items():
        with open(abs_path, 'rb') as f:
            h = hashlib.md5(f.read()).hexdigest()
        files[rel_path] = {'mtime': os.path.getmtime(abs_path), 'hash': h}
    return files


def main():
    state = load_state()
    current = scan_vault()

    new_files = []
    modified_files = []
    unchanged_files = []
    deleted_files = []

    for rel_path, info in current.items():
        if rel_path not in state:
            new_files.append(rel_path)
        elif info['hash'] != state[rel_path].get('hash', ''):
            modified_files.append(rel_path)
        else:
            unchanged_files.append(rel_path)

    for rel_path in state:
        if rel_path not in current:
            deleted_files.append(rel_path)

    has_changes = new_files or modified_files or deleted_files

    print(f"Vault: {VAULT_PATH}")
    print(f"文件总数: {len(current)} (state 记录: {len(state)})\n")

    if new_files:
        print(f"🆕 新增 ({len(new_files)}):")
        for f in sorted(new_files):
            print(f"  + {f}")
    if modified_files:
        print(f"✏️  已修改 ({len(modified_files)}):")
        for f in sorted(modified_files):
            print(f"  ~ {f}")
    if deleted_files:
        print(f"🗑  已删除 ({len(deleted_files)}):")
        for f in sorted(deleted_files):
            print(f"  - {f}")
    if unchanged_files:
        print(f"✅ 未变化 ({len(unchanged_files)}):")
        for f in sorted(unchanged_files):
            print(f"  = {f}")

    print()
    if has_changes:
        print("⚠️  有变更，需要运行管道更新图谱。")
    else:
        print("✅ 无变更。")

    return 0 if not has_changes else 1


if __name__ == '__main__':
    sys.exit(main())
