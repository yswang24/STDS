# STMS 数据治理报告

> 生成时间:2026-08-04
> 数据来源:stms.db (566 条 stds_record, 64 chartcode, 3322 formula 行)

## 1. 公式两表不同步(formula_chart vs formula)

**仅 1 个 chartcode**: `050 051`

| 来源 | 公式 |
|---|---|
| formula_chart | `((V2*0.008936)+(V6*0.00023323)+(V3*0.0003675)+(V4*0.0002866)` |
| formula.ChartFormula | `(((V2*0.008936)+(V6*0.00023323)+(V3*0.0003675)+(V4*0.0002866)` |

**差异**:formula_chart 少一层左括号。

**处理**:以 `formula.ChartFormula` 为准(代码已在 charts_loader 实现)。`formula_chart` 仅作交叉校验。

## 2. 孤儿码(动作代码不在 formula_chart)

| 孤儿码 | stds_record 条数 | 说明 |
|---|---|---|
| `020 12Z` | 18 | 可能是新动作,未录入标准库 |
| `111 051` | 4 | 可能是拼写错误(应为 `051 091`?) |
| `201 020` | 1 | 可能是拼写错误(应为 `202 010`?) |

**合计**:23 条,占 566 条的 4.1%

**处理**:resolver 中 `chart is None` 时返回 `unresolved + needs_review`,不静默放过。

## 3. 空码(动作代码为空或 NULL)

| 类型 | 条数 |
|---|---|
| NULL | 56 |
| 空字符串 | 24 |

**合计**:80 条,占 14.1%

**处理**:同上,进 unresolved 复核队列。

## 4. 公式残留 '=' 前缀

**0 条** -- 加载时 `lstrip("=")` 已覆盖(无实际脏数据,防护在位)。

## 5. 明文凭据

**stds_record 表无飞书相关列** -- 凭据不在 DB 中。

**代码检查**:`config/settings.py` 从 `.env` 读取,无硬编码。飞书凭据在 Dify 工作流的 `写入飞书` 节点(不在本项目代码中)。

## 6. SQL 参数化审计

**全部参数化**:`repo.py` 使用 `?` 占位符,无字符串拼接。

## 7. 已知数据坑(手册 §1.2)

| 坑 | 状态 |
|---|---|
| 历史时间是 Dify LLM 口算,不可信 | 已在方案中明确,以公式为准 |
| token ≠ abbrev (如 18IN vs 46CMX) | 已在 decode 中处理(L1/L2/L3 多层匹配) |
| ValueMetricAbbrev 可 null | 已在 decode 中处理(默认值策略) |
| range 双键 bug | 已在 candidates() 中修复 |
| 050 051 不同步 | 已记录,以 formula.ChartFormula 为准 |

## 总结

| 问题 | 数量 | 处理方式 |
|---|---|---|
| 公式不同步 | 1 个 chartcode | 以 formula.ChartFormula 为准 |
| 孤儿码 | 23 条(4.1%) | unresolved + 复核 |
| 空码 | 80 条(14.1%) | unresolved + 复核 |
| 公式残留 '=' | 0 条 | 防护在位 |
| 明文凭据 | 0 处 | 配置外置 |
| SQL 注入 | 0 处 | 全部参数化 |

**脏数据占比**:23 + 80 = 103 条(18.2%)需人工复核。剩余 463 条可自动化处理。
