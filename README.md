# 化学源数据采集 V6.2 — 40 刊 OA 来源池

## 版本目标

本阶段先把论文来源池从20刊扩展到40刊，并保留期刊、ISSN、出版机构、OA位置与许可证信息。下一阶段再把采集停止条件从“每刊固定数量”改为“每个化学学科至少20篇，允许多个期刊共同完成配额”。

**V6.2 只处理 OA 内容，不进行付费墙绕过、出版商登录或其他认证流程，也不抓取预印本来源。**403、429、robots.txt 拒绝等情况会记录到报告并跳过。

本阶段只完成期刊来源扩展，尚未启用学科分类和学科配额。视频部分保持 MIT OCW 来源，并保留 SHA-256 文件级去重。

> 关于“顶刊”：这是本项目**人工维护的期刊白名单**，不是机器自动判定期刊影响因子或期刊等级。数据准确性取决于白名单和 OpenAlex 期刊元数据。

## 40刊来源池的组成

- 完整保留 V6.1 的20刊。
- 从上传的四类40刊清单中加入其余20本专业、交叉和综述期刊。
- 继续排除过于宽泛的 `Nature` 和 `Science`，避免仅凭期刊身份混入大量非化学论文。
- 保留清单外但明确属于化学专业来源的 `Communications Chemistry` 和 `Nature Chemical Biology`。

因此，V6.2 的40刊是“清单中的38本 + 2本化学专业补充刊”，不是对原始Excel 40行的原样复制。具体 ISSN、出版机构、来源和出版社域名写在 `config/journals_v6_2.json`。期刊名称和 ISSN 必须唯一。

新增20刊及核验信息见 `docs/V6_2_JOURNAL_EXPANSION.md`。

## 安装与替换

将此包里的文件按相同目录放进 `F:\Ontology_Design_Annotation\get_data\chemistry_source_collector` 项目中。

- `collector.py`：替换旧主程序。
- `config/journals_v6_2.json`：V6.2 的40刊 OA 来源池。
- `crawl/crawl_papers.py`：替换旧论文运行入口；已移除 `--paper-sources arxiv,nature` 参数。
- `crawl/crawl_videos.py`：兼容的 MIT OCW 视频运行入口。
- `requirements.txt`：依赖说明。
- `tests/test_collector_v6_2.py`：40刊数量、唯一性和新增差集测试。
- `docs/V6_2_JOURNAL_EXPANSION.md`：新增20刊与核验记录。

### 第一次运行前的数据说明

**V6.2 本身不会删除、覆盖或自动清空任何历史数据。** 请先在本地备份旧 `chemistry_dataset` 文件夹，再人工确认是否要清理旧文件。

如果现有论文 `manifest.jsonl` 中还有白名单以外的来源，程序会报错并停止，防止把旧数据算进成果。也可以把 `--out` 改成新的空文件夹避免与旧数据混淆。

## 在 PyCharm 中运行

在项目根目录的终端执行：

```powershell
python -m pip install -r requirements.txt
python crawl/crawl_papers.py
```

当前 `crawl/crawl_papers.py` 默认只做每刊1篇候选的元数据预览，不下载PDF。学科分类完成后，再改为按学科目标正式采集。

也可以在终端直接运行：

```powershell
# 扩刊阶段：每刊验证1篇合格 OA 候选，不下载 PDF
python collector.py --categories papers --per-journal-target 1 --dry-run

# 暂时仍可人工测试旧的逐刊逻辑，但不作为V6.2最终验收标准
python collector.py --categories papers --per-journal-target 1 --download --out chemistry_dataset/papers_dataset

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

1. 读入40刊来源池，通过期刊 ISSN 向 OpenAlex 查询 Source ID，并确认返回 ISSN 一致。
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
│   ├── journals_v6_1.json
│   └── journals_v6_2.json
├── crawl/
│   ├── crawl_papers.py
│   └── crawl_videos.py
└── chemistry_dataset/
    ├── papers_dataset/
    │   ├── raw/papers/*.pdf
    │   ├── manifest.jsonl
    │   ├── papers_v6_2_candidates.csv
    │   ├── papers_v6_2_filtered_out.csv
    │   ├── journal_feasibility_v6_2.json
    │   ├── journal_feasibility_v6_2.csv
    │   └── errors.jsonl
    └── videos_dataset/
        ├── raw/videos/*.mp4
        ├── manifest.jsonl
        └── errors.jsonl
```

论文 `manifest.jsonl` 记录包括：`title`、`doi`、`journal`、`journal_issn`、`publisher`、`openalex_id`、`openalex_source_id`、`source_page`、`download_url`、`oa_status`、`oa_version`、`oa_pdf_source`、`oa_pdf_source_page`、`license_note`、`published_at`、`collected_at`、`file`、`bytes`、`sha256`、`status` 和 `collector_version`。

### 当前功能边界

- 新增20刊的 ISSN 已通过 Crossref/期刊官网核对；尚未启动按学科分类和正式PDF采集。
- 不登录任何出版商网站、不处理验证码、不绕过 403/429、付费墙或反爬策略。失败会记录，成功总数可能低于目标。
- SHA-256 只能找出**字节完全一致**的重复 PDF / MP4，不能识别语义内容相同或不同排版版本；同时使用 DOI 等元数据查重可补充论文级去重。
- 论文使用40刊来源池；视频仍来自 MIT OCW，不受论文期刊来源池限制。
