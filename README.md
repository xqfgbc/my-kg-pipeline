# My KG Pipeline

AI/IT 运维领域知识图谱构建与检索管道。

## 检索效果

| 指标 | 说明 |
|------|------|
| **jieba 分词** | 中文分词替代 n-gram 暴力切词，token 精准度大幅提升 |
| **TF-IDF 匹配** | char_wb 字符级 n-gram + cosine similarity，消除短词偏差 |
| **双语扩展** | 复用 EntityResolver 42 条中英别名映射，打通跨语言检索 |

> 基准测试：实体命中 +33%，原文段落覆盖 +52%，跨语言检索提升最显著。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置本地环境
cp config.local.yaml.example config.local.yaml
# 编辑 config.local.yaml，将 vault_path 改为你的 Obsidian vault 路径

# 3. 检查变更
python check_changes.py

# 4. 运行知识图谱管道
python kg-pipeline/pipeline.py

# 5. 检索问答
python kg-pipeline/retrieval.py "你的问题"

# 6. 同步 Dashboard
python convert_for_dashboard.py
```

## 项目结构

```
.
├── kg-pipeline/              # 知识图谱管道核心
│   ├── pipeline.py           # 主管道脚本
│   ├── retrieval.py          # 检索器 (v2: jieba + TF-IDF + 双语扩展)
│   ├── config.yaml           # 默认配置
│   ├── extractors/           # 内容提取器 (frontmatter/heading/inline-tag/wiki-link/outline)
│   ├── graph/                # 图谱构建 (entity_resolver/cooccurrence/graph_model)
│   └── output/               # 输出 (frontmatter_writer/json_export/wiki-link_injector)
├── check_changes.py          # 变更检测 (对比 index_state.json)
├── convert_for_dashboard.py  # Dashboard 可视化数据导出
├── kg_pipeline_config.py     # 统一配置加载 (三层优先级)
├── config.local.yaml.example # 本地配置模板
└── requirements.txt          # Python 依赖
```

## 配置

三层优先级，后者覆盖前者：

| 优先级 | 方式 | 说明 |
|--------|------|------|
| 最高 | 环境变量 `KG_VAULT_PATH` | `export KG_VAULT_PATH=/path/to/vault` |
| 中 | `config.local.yaml` | 本地文件，不提交到 Git |
| 低 | `kg-pipeline/config.yaml` | 默认值与实体别名映射，提交到 Git |

## 工作原理

```
PPT/文档 → pipeline.py → graph.json
                              ↓
用户问题 → retrieval.py → 匹配实体 + 关联概念 + 原文段落 → JSON
                              ↓
                         kg-qa skill → 回答 + 来源追溯
```

### 检索流程 (retrieval.py v2)

```
用户查询
  → jieba 分词 (中文精确切词 + 英文提取)
  → expand_bilingual (中→英 / 英→中 别名扩展)
  → EntityMatcher (TF-IDF char_wb + cosine similarity)
  → 图谱共现边扩展 (cooccurs_with)
  → 原文定位 (provenance → 幻灯片摘要)
  → 全文补充 (关键词匹配摘要行)
  → JSON 输出
```

## 实体别名

在 `kg-pipeline/config.yaml` 的 `entity_aliases` 中维护中英双语映射。检索时会自动将中文查询词扩展为英文别名（反之亦然），实现跨语言检索。

当前支持：AgenticAI, 自主化运维, Observability, AIOps, ServiceNow, Dynatrace, MTTR, GenAI, IT运维, DevOps, SRE 等 11 组规范实体及其 42 条别名。

## License

MIT
