# V6.2 40刊来源池扩展记录

日期：2026-10-26

## 扩展规则

V6.2 完整保留 V6.1 的20刊，再增加20刊。上传表格中尚未进入V6.1的期刊共有22本；本阶段继续排除范围过宽的 `Nature` 与 `Science`，加入其余20本。这样既保持总数40，也保留此前的 `Communications Chemistry` 和 `Nature Chemical Biology` 两本化学专业补充刊。

期刊是来源和溯源维度，不再是最终配额维度。下一阶段将使用学科目标，例如“电化学20篇”，允许由多本期刊共同完成。

## 新增20刊

| 期刊 | ISSN | OpenAlex Source ID | 当前来源分组 |
|---|---|---|---|
| Angewandte Chemie International Edition | 1521-3773 | S67393510 | 综合化学 |
| Nature Sustainability | 2398-9629 | S4210188283 | 可持续发展 |
| Advanced Materials | 1521-4095 | S99352657 | 材料 |
| Science Advances | 2375-2548 | S2737427234 | 跨学科综合 |
| Nature Communications | 2041-1723 | S64187185 | 跨学科综合 |
| Proceedings of the National Academy of Sciences of the United States of America | 1091-6490 | S125754415 | 跨学科综合 |
| National Science Review | 2053-714X | S2734413303 | 跨学科综合 |
| Materials Today | 1369-7021 | S63322718 | 材料 |
| Advanced Functional Materials | 1616-3028 | S135204980 | 功能材料 |
| Nano Letters | 1530-6992 | S143846845 | 纳米 |
| Chemistry of Materials | 1520-5002 | S66104727 | 材料化学 |
| Journal of Materials Chemistry A | 2050-7496 | S2764437742 | 能源材料 |
| Chemical Engineering Journal | 1385-8947 | S149937609 | 化学工程 |
| Matter | 2590-2385 | S4210178557 | 材料 |
| Advanced Science | 2198-3844 | S2737737698 | 跨学科综合 |
| Chemical Reviews | 1520-6890 | S41143188 | 化学综述 |
| Chemical Society Reviews | 1460-4744 | S316438 | 化学综述 |
| Nature Reviews Chemistry | 2397-3358 | S4210174246 | 化学综述 |
| Accounts of Chemical Research | 1520-4898 | S145378405 | 化学综述 |
| Nature Reviews Materials | 2058-8437 | S2764917516 | 材料综述 |

## 核验说明

- 20个新增 ISSN 已逐一查询 Crossref 期刊接口；
- 20个新增 ISSN 均已成功解析为匹配的 OpenAlex Source ID；
- `Matter` 使用其在线 ISSN `2590-2385`，没有误用 `One Earth` 的 `2590-3322`；
- 配置测试要求40个期刊名称唯一、ISSN唯一、网站为HTTPS且出版社域名非空；
- 当前只完成来源池扩展，不把“每刊20篇”作为V6.2最终采集目标。

## 下一阶段

1. 建立化学学科配置与关键词/概念映射；
2. 为论文生成主学科和次学科标签；
3. 主学科用于配额计数，避免一篇论文重复填满多个学科；
4. 每个学科至少20篇，并保留期刊、ISSN、DOI、OA许可证和下载来源；
5. 输出学科完成表以及学科—期刊来源矩阵。
