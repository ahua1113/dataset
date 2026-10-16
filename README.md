# Chemistry Ontology Design & Annotation Dataset

化学学科本体设计与标注数据集的原始资料采集项目。

## V5 论文采集器

V5 不再采用：

```text
arXiv → 失败 → Nature/Science 兜底
```

而采用：

```text
arXiv ───────┐
Nature ──────┼──→ 平衡轮询 → 统一化学验证 → 全局查重 → 下载 → SHA-256
Science ─────┘
```

### 默认论文来源

- arXiv `physics.chem-ph`
- Nature Portfolio：Nature Chemistry、Communications Chemistry、Nature Catalysis、Nature Synthesis、Nature Chemical Biology、Nature Communications
- Science family：Science Advances

Nature/Science 路线通过 OpenAlex 获取论文元数据和 OA PDF 位置，不绕过付费墙。

### 平衡策略

默认把“本次还需要补的数量”平均分配到启用的来源，然后 round-robin 轮询。

例如还需要 20 篇：

```text
arXiv       7
Nature      7
Science     6
```

也可以显式指定：

```text
--source-targets arxiv=7,nature=7,science=6
```

注意：你已有的旧 arXiv 文件会保留并计入总文件数；V5 平衡的是新增采集部分，不会为了“比例”删除历史数据。

### 全局查重

跨来源使用：

1. DOI
2. arXiv ID
3. OpenAlex ID
4. 标题规范化
5. 下载 URL

下载完成后再使用 SHA-256 做内容级最终去重。

因此同一篇论文从 arXiv 和 Nature OA 位置同时出现时，只保留一份源文件。

## 项目结构

```text
dataset/
├── collector.py                 # 核心采集器
├── crawl/                       # 所有爬取入口
│   ├── crawl_papers.py
│   ├── crawl_videos.py
│   ├── crawl_textbooks.py
│   ├── crawl_teaching.py
│   └── crawl_methods.py
├── tools/                       # 审计/分析等非爬取工具
│   └── audit_papers.py
├── config/
│   └── textbook_seeds.csv
├── chemistry_dataset/
│   ├── papers_dataset/
│   ├── videos_dataset/
│   ├── textbooks_dataset/
│   ├── teaching_dataset/
│   └── methods_dataset/
├── docs/
├── requirements.txt
└── .gitignore
```

## PyCharm 中运行

PyCharm 的 Run Configuration 建议选择：

```text
Script path:
F:\...\dataset\crawl\crawl_papers.py
```

Working directory：

```text
F:\...\dataset
```

直接运行即可。

## 论文目标

`crawl/crawl_papers.py` 默认：

```text
target = 120
sources = arxiv,nature,science
```

如果已有 100 篇历史论文，则本次只尝试补齐剩余 20 篇。

如果某来源 429 或暂时不可用，只暂停该来源；其他来源继续。

## Git

GitHub 仓库只保存代码、配置和轻量元数据。

PDF/MP4 等原始文件由 `.gitignore` 排除，不建议直接提交到普通 Git 仓库。
