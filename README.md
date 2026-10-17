# Chemistry Source Collector V5.3（arXiv + Nature）

## 1. 版本定位

V5.3

当前阶段不再使用 Science / Science Advances，仅用arXiv和Nature

```text
arXiv ───────┐
             ├──→ 平衡轮询 → 全局查重 → 化学领域验证 → 下载 → SHA-256
Nature ──────┘
```

不会删除或覆盖已经采集的数据。

---

## 2. 当前论文来源

### arXiv

使用官方：

```text
physics.chem-ph
```

因此不需要再做复杂的文章级化学关键词判断。

### Nature Portfolio

V5.3 暂时只启用化学学科定位非常明确的期刊：

- Nature Chemistry
- Communications Chemistry
- Nature Catalysis
- Nature Synthesis
- Nature Chemical Biology

这些期刊在来源层面已经属于化学核心期刊，因此 V5.3 不再要求：

```text
title/abstract 必须命中 chemistry / chemical / molecule ...
```

也就是说，**期刊身份本身就是化学领域证据**。

暂时移除 Nature Communications，因为它属于综合性期刊，需要额外的文章级筛选；当前目标是先稳定、快速地建立化学论文原始语料。

---

## 3. 为什么比之前的 V5.3 快

### ① 删除 Science

不再访问：

```text
Science Advances
```

因此不会再出现：

```text
0 chemistry-qualified
```

这一类问题。

### ② Nature 不再做复杂的文章级化学筛选

对于上述 5 个化学核心期刊：

```text
ISSN / OpenAlex Source
        ↓
期刊身份
        ↓
直接认为 Chemistry
        ↓
只检查 OA + PDF
```

### ③ Nature 查询采用惰性方式

不是启动时把所有 Nature 子刊全部查一遍。

例如：

```text
先查 Nature Chemistry
    ↓
已经找到足够的新论文
    ↓
不继续查 Communications Chemistry / Nature Catalysis ...
```

只有前面的期刊候选不足时才继续访问下一个期刊。

### ④ API 快速失败

V5.3 不再针对 HTTP 429 做长时间等待。

OpenAlex / arXiv 暂时不可用时，只暂停对应来源并把未完成配额转给另一个来源。

### ⑤ 下载失败不消耗来源配额

例如：

```text
Nature 候选 A
↓
403 / timeout
↓
不消耗 Nature 配额
↓
继续找 Nature 候选 B
```

---

## 4. 查重

保留 V5.2 的全局查重机制：

1. DOI
2. arXiv ID
3. OpenAlex ID
4. 规范化标题
5. 下载 URL
6. SHA-256 内容级查重

所以同一篇论文：

```text
arXiv
    +
Nature OA
```

只会保留一份。

---

## 5. 配额逻辑

V5.3 的来源配额指的是：

> **成功下载的、非重复文件数量**

例如现在已有：

```text
127 篇
```

目标：

```text
130 篇
```

需要：

```text
3 篇
```

默认：

```text
arxiv=2
nature=1
```

调度顺序：

```text
arXiv
  ↓
Nature
  ↓
arXiv
```

如果某篇重复：

```text
[DUP]
```

不会消耗配额。

如果下载失败：

```text
[SKIP]
```

也不会消耗配额。

---

## 6. PyCharm 中运行

直接运行：

```text
crawl/crawl_papers.py
```

当前脚本：

```python
TARGET = 130
```

以后例如需要 200 篇：

```python
TARGET = 200
```

即可。

---

## 7. 数据保存

原有目录不变：

```text
chemistry_dataset/
└── papers_dataset/
    ├── raw/
    │   └── papers/
    ├── manifest.jsonl
    ├── errors.jsonl
    ├── papers_v5_3_candidates.csv
    └── papers_v5_3_filtered_out.csv
```

V5.3 不删除旧的 `raw/` 文件。

---

## 8. V5.3 与当前问题的对应关系

| 问题 | V5.3 处理 |
|---|---|
| Science Advances 化学验证过严 | 直接移除 Science |
| Nature 化学验证复杂 | 化学核心期刊直接按期刊身份验证 |
| `signature_ok is not defined` | 保留 V5.2 的完整 `signature_ok()` |
| 下载失败消耗配额 | 不消耗 |
| SHA-256 重复 | 不消耗 |
| arXiv/Nature 互相重复 | 全局查重 |
| API 429 等待太久 | 快速失败 |
| Nature 所有期刊一次性查询 | 惰性查询 |
| 旧数据 | 保留 |
| 来源比例 | arXiv/Nature 两来源平衡轮询 |

