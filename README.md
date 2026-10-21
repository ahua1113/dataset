# 化学源数据采集 V6.0 — 十刊白名单 / OA 优先 / 文件与元数据去重

## 版本目标

只从**经过 ISSN 核实的 10 本化学顶刊**发现研究论文，优先下载无需订阅登录、具有公开 PDF 链接的 OA 论文。**V6 不进行任何付费墙绕过、出版商登录或其他认证流程，也不抓取预印本来源。**

V6 继续保存 PDF 原文件，不做正文解析、OCR、化学知识抽取或论文语义相似度分析。视频部分保持 MIT OCW 来源，并补上视频 SHA-256 文件级去重。

> 关于“顶刊”：这是本项目**人工维护的期刊白名单**，不是机器自动判定期刊影响因子或期刊等级。数据准确性取决于白名单和 OpenAlex 期刊元数据。

## 白名单来源

### 保留 V5.3 的五本 Nature Portfolio 期刊

1. Nature Chemistry
2. Communications Chemistry
3. Nature Catalysis
4. Nature Synthesis
5. Nature Chemical Biology

其中 **Communications Chemistry、Nature Chemical Biology** 并未出现在本次上传的《化学顶刊分类_修订版_四类40刊.xlsx》里，但根据“保留 V5.3 五本”的要求仍然保留。

### 从 Excel 的“第二类 综合化学旗舰期刊”新选五本

6. ACS Central Science
7. Angewandte Chemie International Edition
8. Chem
9. Chemical Science
10. Journal of the American Chemical Society (JACS)

选择原因：相比 Nature / Science / Nature Communications 等跨学科综合刊，这五本以化学为学科核心，适合直接按期刊身份完成学科级筛选；其中 ACS Central Science 与 Chemical Science 为开放获取取向，有助于 V6 早期积累 OA PDF。四类 40 刊是**学科定位分类**，并非官方排名。

具体 ISSN、出版机构、来源、出版社域名写在 `config/journals_v6.json`。若以后调整期刊，只修改 JSON（必须保持 10 本），无需动爬虫主逻辑。

## 相比 V5.3 的变更

| 项目 | V5.3 | V6.0 |
|---|---|---|
| 论文来源 | arXiv + 5 本 Nature | 严格 10 本期刊白名单；完全取消预印本来源发现 |
| 学科判断 | arXiv 分类或 Nature 期刊白名单 | ISSN → OpenAlex Source ID → 论文 primary journal Source ID 一致 |
| 论文类型 | 主要按来源 | 仅 `article`，排除明显的勘误、撤稿、编者按；缺 DOI 的作品不收 |
| OA 要求 | 主要查可获取 PDF | `open_access.is_oa` + 逐个核验 OA 位置，优先出版社 PDF |
| 付费/认证 | 不使用 | 不使用；遇 401/403/付费登录跳转跳过 |
| 元数据去重 | DOI/标题/OpenAlex/URL 等 | DOI、规范化标题、OpenAlex ID、下载 URL |
| 文件级去重 | PDF SHA-256 | PDF 和 MP4 下载后均对已有 SHA-256 查重 |
| 溯源 | manifest.jsonl | 延续并增加来源期刊 ISSN、OA 版本、实际 PDF 来源、筛选证据 |

## 安装与替换

将此包里的文件按相同目录放进 `F:\Ontology_Design_Annotation\get_data\chemistry_source_collector` 项目中。

- `collector.py`：替换旧主程序。
- `config/journals_v6.json`：新增十刊白名单。
- `crawl/crawl_papers.py`：替换旧论文运行入口；已移除 `--paper-sources arxiv,nature` 参数。
- `crawl/crawl_videos.py`：兼容的 MIT OCW 视频运行入口。
- `requirements.txt`：依赖说明。
- `tests/test_collector_v6.py`：离线自动化测试。

### 第一次运行前的数据说明

清空 V5.3 以前的数据。**V6 本身不会删除、覆盖或自动清空任何历史数据。** 请先在本地**备份**旧 `chemistry_dataset` 文件夹，然后人工确认要清空哪些 `raw/` 文件、`manifest.jsonl` 和候选/错误记录。

如果现有论文 `manifest.jsonl` 中还有白名单以外的来源，V6 会报错并停止，防止把旧数据算进 V6 成果。也可以把 `--out` 改成新的空文件夹避免与旧数据混淆。

## 在 PyCharm 中运行

在项目根目录的终端执行：

```powershell
python -m pip install -r requirements.txt
python crawl/crawl_papers.py
```

在 `crawl/crawl_papers.py` 中修改 `TARGET = 300` 即可设置**累计成功下载、未重复的 PDF 数量**。已有有效文件数计入目标。下载失败或 SHA 重复不会消耗目标配额。

也可以在终端直接运行：

```powershell
# 预览 10 篇可用的顶刊 OA 候选，不下载 PDF
python collector.py --categories papers --limit 10 --dry-run

# 正式累计采集 300 篇
python collector.py --categories papers --limit 300 --download --out chemistry_dataset/papers_dataset

# 如果 OA 论文较少，可以适当增加每期刊查询页数
python collector.py --categories papers --limit 300 --download --max-pages-per-journal 15

# 核验本地已下载文件的 SHA-256（不执行采集）
python collector.py --categories papers --audit-only --out chemistry_dataset/papers_dataset

# 保留的 MIT OCW 视频功能（100 部）
python crawl/crawl_videos.py

# 离线自动测试（不访问网络）
python -m unittest discover -s tests -v
```

可通过 `--contact 邮箱` 指定礼貌性爬虫身份。它是 User-Agent 联系信息，**不是认证或登录流程**。不要把私人登录凭证写进代码。

## 核心筛选流程

1. 读入十刊白名单，通过期刊 ISSN 向 OpenAlex 查询 Source ID，并确认返回 ISSN 一致。
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
│   └── journals_v6.json
├── crawl/
│   ├── crawl_papers.py
│   └── crawl_videos.py
└── chemistry_dataset/
    ├── papers_dataset/
    │   ├── raw/papers/*.pdf
    │   ├── manifest.jsonl
    │   ├── papers_v6_candidates.csv
    │   ├── papers_v6_filtered_out.csv
    │   └── errors.jsonl
    └── videos_dataset/
        ├── raw/videos/*.mp4
        ├── manifest.jsonl
        └── errors.jsonl
```

论文 `manifest.jsonl` 记录包括：`title`、`doi`、`journal`、`journal_issn`、`publisher`、`openalex_id`、`openalex_source_id`、`source_page`、`download_url`、`oa_status`、`oa_version`、`oa_pdf_source`、`oa_pdf_source_page`、`license_note`、`published_at`、`collected_at`、`file`、`bytes`、`sha256`、`status` 和 `collector_version`。

### 当前功能边界

- 这是**代码与离线验证版本**，没有在此执行真实 OpenAlex/出版社联机采集。不能预先保证每期刊都有足够 OA PDF，也不能保证出版社不拒绝访问。
- 不登录任何出版商网站、不处理验证码、不绕过 403/429、付费墙或反爬策略。失败会记录，成功总数可能低于目标。
- SHA-256 只能找出**字节完全一致**的重复 PDF / MP4，不能识别语义内容相同或不同排版版本；同时使用 DOI 等元数据查重可补充论文级去重。
- 本版只把论文来源切换到十刊；视频仍来自 MIT OCW，不受论文期刊白名单限制。
