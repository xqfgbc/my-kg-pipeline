# My KG Pipeline

AI/IT 运维领域知识图谱构建与检索管道。

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
│   ├── retrieval.py          # 检索器
│   ├── config.yaml           # 默认配置
│   ├── extractors/           # 内容提取器
│   ├── graph/                # 图谱构建模块
│   └── output/               # 输出模块
├── check_changes.py          # 变更检测
├── convert_for_dashboard.py  # Dashboard 同步
├── kg_pipeline_config.py     # 统一配置加载
├── config.local.yaml.example # 本地配置模板
└── requirements.txt          # 依赖声明
```

## 配置

| 方式 | 优先级 | 说明 |
|------|--------|------|
| 环境变量 `KG_VAULT_PATH` | 最高 | `export KG_VAULT_PATH=/path/to/vault` |
| `config.local.yaml` | 中 | 本地文件，不提交到 Git |
| `kg-pipeline/config.yaml` | 低 | 默认值，提交到 Git |

## License

MIT
