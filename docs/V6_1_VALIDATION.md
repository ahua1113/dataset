# V6.1 验证记录（2026-10-22）

## 结论

- 20 本期刊配置均通过格式、HTTPS、期刊名唯一性和 ISSN 唯一性校验。
- 真实 OpenAlex `sources/issn:{ISSN}` 请求全部成功，20 个 ISSN 均解析为有效 Source ID。
- 当前执行环境的 OpenAlex Works 共享额度已耗尽，20 本期刊的论文查询均收到 HTTP 429；因此本次没有宣称完成真实的逐刊 20 篇采集。
- V6.1 已支持 `OPENALEX_API_KEY`/`--openalex-api-key`。配置可用额度后，可分别运行元数据可行性测试和正式 PDF 下载。
- 离线自动测试模拟了 20 本期刊、每刊 20 篇候选，共 400 条；程序仅在每一本期刊达到目标后停止。

## OpenAlex Source ID 解析结果

| 期刊 | ISSN | OpenAlex Source ID |
|---|---|---|
| Nature Chemistry | 1755-4349 | S202193212 |
| Communications Chemistry | 2399-3669 | S4210191625 |
| Nature Catalysis | 2520-1158 | S4210229375 |
| Nature Synthesis | 2731-0582 | S4210215815 |
| Nature Chemical Biology | 1552-4469 | S51309854 |
| ACS Central Science | 2374-7951 | S2765035057 |
| Angewandte Chemie International Edition | 1521-3773 | S67393510 |
| Chem | 2451-9294 | S2898106599 |
| Chemical Science | 2041-6539 | S184645833 |
| Journal of the American Chemical Society | 1520-5126 | S111155417 |
| Nature Energy | 2058-7546 | S2764528046 |
| Nature Materials | 1476-4660 | S103895331 |
| Nature Nanotechnology | 1748-3395 | S7822423 |
| Joule | 2542-4351 | S2898305631 |
| Energy & Environmental Science | 1754-5706 | S117082959 |
| ACS Catalysis | 2155-5435 | S37391459 |
| Nature Chemical Engineering | 2948-1198 | S4390963334 |
| Advanced Energy Materials | 1614-6840 | S2502627819 |
| ACS Energy Letters | 2380-8195 | S2764414965 |
| ACS Nano | 1936-0851 | S145476921 |

## 复现命令

仅验证每刊是否能发现 20 篇合格 OA 候选，不下载 PDF：

```powershell
$env:OPENALEX_API_KEY="你的 key"
python collector.py --categories papers --per-journal-target 20 --dry-run
```

正式下载，并要求每刊至少保存 20 份有效、未重复 PDF：

```powershell
$env:OPENALEX_API_KEY="你的 key"
python collector.py --categories papers --per-journal-target 20 --download
```

结果写入 `journal_feasibility_v6_1.json` 和 `journal_feasibility_v6_1.csv`。HTTP 403、429、robots.txt 拒绝、无合法 PDF、重复 DOI/标题/OpenAlex ID/URL/SHA-256 等情况不会计入成功数。
