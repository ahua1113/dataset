# 化学学科原始文件批量采集项目

## 覆盖四类，目标各 100 份

| CLI 类别 | 数据内容 | 来源与格式 | 说明 |
|---|---|---|---|
| `papers` | 化学相关科研论文 | arXiv `physics.chem-ph`, PDF | 非全部化学子领域；逐篇核对许可 |
| `videos` | 化学公开课视频 | MIT OCW 官方下载链接指向 Internet Archive, MP4 | 100 个可能消耗 10GB 以上空间 |
| `teaching` | 化学课程讲义 PDF | MIT OCW, PDF | **不是 100 本完整教材**；逐章/讲义为一份 |
| `methods` | 化学分析与测试方法 | US EPA SW-846, PDF | 多为指导性测试方法，并非 ISO/GB 强制性标准 |
| `textbooks` | 可选：完整开放教材 | `textbook_seeds.csv` 手动添加已核验的官方 PDF 直链 | 默认 0 项，绝不拿讲义充当整本教材 |

## 安装

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows: .venv\\Scripts\\activate
python -m pip install -r requirements.txt
```

## 先发现 URL，不下载大文件

```bash
python collector.py --limit 100 --contact you@university.edu
```

输出四个 `*_candidates.csv`，可先检查采集清单。此操作也会请求网站元数据页面。

## 确认来源与权利后下载（各成功文件最多 100）

```bash
python collector.py --limit 100 --download --contact you@university.edu
```

更建议先小量运行：

```bash
python collector.py --categories papers,teaching,methods --limit 3 --download --contact you@university.edu
python collector.py --categories videos --limit 1 --download --contact you@university.edu
```

**注意**：如果先测试了 `--limit 3`，之后再运行 `--limit 100`，程序通过已有 `manifest.jsonl` 断点续传，继续补足至 100。下载后 `.part` 临时文件会移除，不覆盖已成功下载的源文件。

## 输出

```text
chemistry_dataset/
  papers_candidates.csv
  videos_candidates.csv
  teaching_candidates.csv
  methods_candidates.csv
  manifest.jsonl            # 成功文件的元数据、URL、SHA256
  errors.jsonl              # 出错文件与原因
  raw/
    papers/*.pdf
    videos/*.mp4
    teaching/*.pdf
    methods/*.pdf
```

不做 OCR，不转码、不改文件内容。有效下载必须通过格式文件头校验。若无法收集满 100 个成功文件，会如实报告。

## 100 本完整教材如何处理？

本工具不能保证从单一公开网站获得 100 本化学教科书。仅当确认整本教材的官方 PDF 下载地址及许可时，将条目填写到 `textbook_seeds.csv`：

```
title,publisher,source_page,download_url,license_note,published_at
...your-verified-record...
```

执行：

```bash
python collector.py --categories textbooks --limit 100 --download --books-csv textbook_seeds.csv --contact you@university.edu
```

可以寻找 OpenStax、LibreTexts/LibreCommons、BCcampus Open Education 等，但须逐本核对当前许可、PDF 是否真正公开以及针对 AI 训练/内容再利用的限制。不要伪造 URL，不要爬盗版镜像。

## 安全、授权与限制

- 只采集可公开访问的 HTTPS PDF/MP4 链接，不绕过账号、付费墙、DRM、反爬机制、验证码和流媒体保护。
- 默认验证 robots.txt；无法查明 robots 规则则不抓取，逐站点缓慢请求，并处理 429/5xx 的等待重试。
- MIT OCW 一般采用 CC BY-NC-SA 4.0，但第三方保留权利内容有单独条款；需非商业使用、署名并检查页面特定声明。
- arXiv 论文不是自动全部 CC BY；需要核对每篇论文的授权范围。
- EPA SW-846 是官方方法库，但不应把所有 PDF 当作强制标准，也不能认为网站上所有第三方素材都属于公有领域。
- 不要把 OpenStax/其他站点的“免费下载”误等同于“可以用于模型训练”；需要逐本查条款。
- 有些源站点可访问但可能偶发 403/429、URL 变更。`errors.jsonl` 保留异常供复核。
- 如果下载超过预设大小或服务器返回 HTML 伪装成 PDF/MP4，会跳过。

## 来源页

- arXiv API: https://info.arxiv.org/help/api/index.html
- MIT OCW chemistry: https://ocw.mit.edu/search/?d=Chemistry
- EPA SW-846: https://www.epa.gov/hw-sw846/sw-846-compendium
- 开放教材: https://openstax.org/subjects/science ; https://chem.libretexts.org/

## 未测试范围

开发环境不具备对外 HTTP 下载能力，因此本工程完成了代码静态检查与离线解析测试，**但尚未在当前环境完成联网端到端下载**。网站的反爬/链接格式可能随时间变化，需要在用户本机联网验证。100 个 MP4 往往至少数 GB，必须预留空间。
