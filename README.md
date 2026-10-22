# 化学源数据采集 V6.1 — 20 刊白名单 / 逐刊 20 篇可行性测试

## 版本目标

只从**经过 ISSN 核实的 20 本化学期刊白名单**发现研究论文。V6.1 的阶段性验收标准是**每本期刊至少发现 20 篇合格 OA 候选；正式下载时则要求每刊至少保存 20 份有效、未重复 PDF**。程序不再用一个全局总量掩盖某些期刊为零的问题。

**V6.1 不进行任何付费墙绕过、出版商登录或其他认证流程，也不抓取预印本来源。**403、429、robots.txt 拒绝等情况会记录到报告并跳过。

V6.1 继续保存 PDF 原文件，不做正文解析、OCR、化学知识抽取或论文语义相似度分析。视频部分保持 MIT OCW 来源，并保留视频 SHA-256 文件级去重。

> 关于“顶刊”：这是本项目**人工维护的期刊白名单**，不是机器自动判定期刊影响因子或期刊等级。数据准确性取决于白名单和 OpenAlex 期刊元数据。

## 白名单来源

### 保留 V5.3 的五本 Nature Portfolio 期刊

1. Nature Chemistry
2. Communications Chemistry
3. Nature Catalysis
4. Nature Synthesis
5. Nature Chemical Biology

其中 **Communications Chemistry、Nature Chemical Biology** 并未出现在本次上传的《化学顶刊分类_修订版_四类40刊.xlsx》里，但根据“保留 V5.3 五本”的要求仍然保留。

### 从 Excel 的“第二类 综合化学旗舰期刊”选择五本

6. ACS Central Science
7. Angewandte Chemie International Edition
8. Chem
9. Chemical Science
10. Journal of the American Chemical Society (JACS)

### V6.1 新增十本专业或交叉期刊

11. Nature Energy
12. Nature Materials
13. Nature Nanotechnology
14. Joule
15. Energy & Environmental Science
16. ACS Catalysis
17. Nature Chemical Engineering
18. Advanced Energy Materials
19. ACS Energy Letters
20. ACS Nano

新增期刊均来自《化学顶刊分类_四类40刊.xlsx》。本阶段优先加入能源、材料、纳米、催化和化学工程方向，先扩大化学子学科覆盖；Nature、Science 等宽泛综合刊暂不进入这 20 刊，以免仅凭期刊身份混入大量非化学论文。四类 40 刊是**学科定位分类**，并非官方排名。

具体 ISSN、出版机构、来源、出版社域名写在 `config/journals_v6_1.json`。白名单必须保持 20 本且期刊名称和 ISSN 不得重复。

## 相比 V6.0 的变更

| 项目 | V6.0 | V6.1 |
|---|---|---|
| 论文来源 | 10 本白名单 | 20 本白名单，新增 10 本专业/交叉期刊 |
| 停止条件 | 全局 `--limit` | `--per-journal-target`，逐刊分别达标 |
| 可行性测试 | 只有总量和下载统计 | 逐刊输出候选、尝试、下载、有效 PDF 和失败原因 |
| 版本标记 | `6.0` | `6.1` |
| OpenAlex 认证 | 无专用参数 | 支持 `OPENALEX_API_KEY` 或 `--openalex-api-key` |

## 安装与替换

将此包里的文件按相同目录放进 `F:\Ontology_Design_Annotation\get_data\chemistry_source_collector` 项目中。

- `collector.py`：替换旧主程序。
- `config/journals_v6_1.json`：V6.1 的 20 刊白名单。
- `crawl/crawl_papers.py`：替换旧论文运行入口；已移除 `--paper-sources arxiv,nature` 参数。
- `crawl/crawl_videos.py`：兼容的 MIT OCW 视频运行入口。
- `requirements.txt`：依赖说明。
- `tests/test_collector_v6_1.py`：20 刊逐刊目标自动化测试。
- `docs/V6_1_VALIDATION.md`：真实 Source ID 解析结果与当前 429 配额限制记录。

### 第一次运行前的数据说明

**V6.1 本身不会删除、覆盖或自动清空任何历史数据。** 请先在本地备份旧 `chemistry_dataset` 文件夹，再人工确认是否要清理旧文件。

如果现有论文 `manifest.jsonl` 中还有白名单以外的来源，V6.1 会报错并停止，防止把旧数据算进成果。也可以把 `--out` 改成新的空文件夹避免与旧数据混淆。

## 在 PyCharm 中运行

在项目根目录的终端执行：

```powershell
python -m pip install -r requirements.txt
python crawl/crawl_papers.py
```

在 `crawl/crawl_papers.py` 中修改 `PER_JOURNAL_TARGET = 20` 可设置逐刊最低数量。已有有效文件数会按期刊计入；下载失败或 SHA 重复不计入目标。

也可以在终端直接运行：

```powershell
# 每本期刊预览 20 篇合格 OA 候选，不下载 PDF
python collector.py --categories papers --per-journal-target 20 --dry-run

# 正式采集：20 本期刊分别达到至少 20 份有效 PDF
python collector.py --categories papers --per-journal-target 20 --download --out chemistry_dataset/papers_dataset

# 如果 OA 论文较少，可以适当增加每期刊查询页数
python collector.py --categories papers --per-journal-target 20 --download --max-pages-per-journal 15

# OpenAlex 配额要求 API key 时（PowerShell）
$env:OPENALEX_API_KEY="你的 key"
python crawl/crawl_papers.py

# 核验本地已下载文件的 SHA-256（不执行采集）
python collector.py --categories papers --audit-only --out chemistry_dataset/papers_dataset

# 保留的 MIT OCW 视频功能（100 部）
python crawl/crawl_videos.py

# 离线自动测试（不访问网络）
python -m unittest discover -s tests -v
```

可通过 `--contact 邮箱` 指定礼貌性爬虫身份。它是 User-Agent 联系信息，**不是认证或登录流程**。不要把私人登录凭证写进代码。

## 核心筛选流程

1. 读入 20 刊白名单，通过期刊 ISSN 向 OpenAlex 查询 Source ID，并确认返回 ISSN 一致。
2. **仅检索 `primary_location.source.id` 为目标期刊 Source ID 的论文**；要求 OpenAlex 类型为 `article`，标记 OA 且未撤稿。
3. 要求 DOI 不为空；拒绝明显的 correction / retraction / editorial 等标题。
4. 读取公开 OA PDF 地址：优先出版社公开 PDF，出版社无公开 PDF 时可退到 OpenAlex 识别的**正式发表版或接收稿版**仓储 PDF。排除预印本服务器和提交稿版本。
5. 下载前使用 DOI / OpenAlex ID / 标题 / 下载 URL 查重。每次下载还核验文件签名及大小，流式计算 SHA-256，再和现有 SHA-256 比较。
6. 保存合法下载文件到 `raw/papers/*.pdf`，成功后追加 `manifest.jsonl`；下载失败 / 403 / 429 记录到 `errors.jsonl`，不中断其他期刊的查询。

### 关于“是否原版 PDF”

**公开可下载的 OA PDF 不一定是出版社排版版 PDF。** 本程序优先下载出版社公开的 `publishedVersion`；若只存在合法公开的机构仓储 `acceptedVersion`，仍会允许下载并在 `manifest.jsonl` 里记录 `oa_pdf_source=OA_repository`、`oa_version=acceptedVersion`，确保不把接受稿误称为出版社最终排版版。若将来要求“只允许出版社最终 PDF”，可以在 `candidate_pdf_locations` 中禁止非出版社来源。

**OA 可免费下载 ≠ 自动获得转载、分发、训练或再次发布的全部权限**。每条记录的 `license_note` 有许可证信息（未知时要求人工核验），应按具体论文条款使用。

## 目录与记录结构

```text
chemistry_source_collector/
├── collector.py
├── requirements.txt
├── config/
│   ├── journals_v6.json
│   └── journals_v6_1.json
├── crawl/
│   ├── crawl_papers.py
│   └── crawl_videos.py
└── chemistry_dataset/
    ├── papers_dataset/
    │   ├── raw/papers/*.pdf
    │   ├── manifest.jsonl
    │   ├── papers_v6_1_candidates.csv
    │   ├── papers_v6_1_filtered_out.csv
    │   ├── journal_feasibility_v6_1.json
    │   ├── journal_feasibility_v6_1.csv
    │   └── errors.jsonl
    └── videos_dataset/
        ├── raw/videos/*.mp4
        ├── manifest.jsonl
        └── errors.jsonl
```

论文 `manifest.jsonl` 记录包括：`title`、`doi`、`journal`、`journal_issn`、`publisher`、`openalex_id`、`openalex_source_id`、`source_page`、`download_url`、`oa_status`、`oa_version`、`oa_pdf_source`、`oa_pdf_source_page`、`license_note`、`published_at`、`collected_at`、`file`、`bytes`、`sha256`、`status` 和 `collector_version`。

### 当前功能边界

- 已联机验证 20 个 ISSN 均可解析到 OpenAlex Source ID；本次环境的 Works API 因共享额度返回 429，尚未完成真实 20×20 候选/PDF 验收。详情见 `docs/V6_1_VALIDATION.md`。
- 不登录任何出版商网站、不处理验证码、不绕过 403/429、付费墙或反爬策略。失败会记录，成功总数可能低于目标。
- SHA-256 只能找出**字节完全一致**的重复 PDF / MP4，不能识别语义内容相同或不同排版版本；同时使用 DOI 等元数据查重可补充论文级去重。
- 论文使用 20 刊白名单；视频仍来自 MIT OCW，不受论文期刊白名单限制。
