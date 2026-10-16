# 从当前项目迁移到 V5

假设当前项目：

F:\Ontology_Design_Annotation\get_data\chemistry_source_collector

## 1. 先备份

在 PyCharm 中复制项目目录，或至少备份：

- collector.py
- crawl_papers.py
- crawl_videos.py
- crawl_textbooks.py
- textbook_seeds.csv
- chemistry_dataset/

## 2. 把现有爬虫移动到 crawl

PowerShell：

```powershell
New-Item -ItemType Directory -Force crawl
Move-Item crawl_papers.py crawl\
Move-Item crawl_videos.py crawl\
Move-Item crawl_textbooks.py crawl\
New-Item -ItemType Directory -Force tools
Move-Item audit_papers.py tools\
```

然后新增：

```text
crawl/crawl_teaching.py
crawl/crawl_methods.py
```

V5 包中已经提供这些脚本。

## 3. 教材种子文件

把原来的：

```text
textbook_seeds.csv
```

复制到：

```text
config/textbook_seeds.csv
```

不要删除原文件，确认新文件内容正确后再决定是否删除。

## 4. 替换 collector.py

用 V5 的：

```text
collector.py
```

替换项目根目录的旧 collector.py。

## 5. 不要删除 chemistry_dataset

V5 不要求重新下载已有数据。

## 6. PyCharm Run Configuration

对于论文：

Script path：

```text
F:\Ontology_Design_Annotation\get_data\chemistry_source_collector\crawl\crawl_papers.py
```

Working directory：

```text
F:\Ontology_Design_Annotation\get_data\chemistry_source_collector
```

Python interpreter：

```text
F:\Ontology_Design_Annotation\get_data\chemistry_source_collector\.venv\Scripts\python.exe
```

## 7. 运行论文采集

直接运行：

```text
crawl/crawl_papers.py
```

默认：

```text
--limit 120
--paper-sources arxiv,nature,science
```

如果已有 100 篇旧论文，本次需要新增约 20 篇，默认配额约为：

```text
arXiv   7
Nature  7
Science 6
```

如果某来源 429 或耗尽，其剩余配额动态转给其他可用来源。

## 8. 如果需要严格指定本轮配额

把 crawl/crawl_papers.py 的命令列表中加入：

```text
--source-targets arxiv=7,nature=7,science=6
```

注意：这是“本次新增”的配额，不是删除旧数据后重新平衡总历史数据。

## 9. Git 提交

确认运行无误后：

```powershell
git status
git add collector.py crawl config docs .gitignore requirements.txt
git commit -m "Refactor crawler structure and add V5 balanced paper collector"
git push
```

确认 `.gitignore` 仍然排除：

```text
chemistry_dataset/**/raw/
*.pdf
*.mp4
```

不要把已有 PDF/MP4 加入 Git。
