# 海外保单风险处置台

面向出口企业多国投保（货运险、政治风险险）的理赔协作服务。把保单范围、地区、免赔额、通知时限与事故证据集中到一条处置链上，替代理赔员在各承保人门户重复登记的做法，保证赔付进度与追偿责任一致、可审计。

## 设计要点

- **事件溯源**：一切事实（报案、回执、材料、调查节点、核定、预付、赔付、追偿）以追加事件写入事件存储；任何状态更正都是新事件，原始文件摘要不可替换。
- **按事故顺序整理**：每条事件带业务时间与到达序号，处置链按业务时间排序；断网补传与系统恢复（JSONL 重放）后顺序不变。
- **可解释状态**：重复通知（`duplicate_notice`）、跨时区报案（`cross_timezone_normalized`，按绝对时间归一化）、部分损失（`partial_loss`）、事故后生效的批改（`endorsement_not_applicable`）、超范围材料（`material_out_of_scope`）、迟到追偿（`late_subrogation_receipt`）等都以 `notices` 形式落在案件视图上。
- **范围拦截**：案件或材料超出保单范围（有效期/地区/险种/币种）时留痕但不推进赔付；预付、赔付命令直接拒绝并留痕。
- **紧急预付**：必须记录授权人（`authorizer`）与上限（`limit`），超额或缺项拒绝并记入处置链。
- **可审计**：赔付金额（`min(核定损失, 保额) - 免赔额 - 已付`）、责任人、下一步动作全部由处置链推导；`GET /cases/{id}/chain` 输出完整事件序列，含被拒绝的尝试。

## 运行

```bash
python -m service.main          # 默认端口 8000，纯内存存储
PORT=8000 DESK_STORE_PATH=/var/lib/desk/events.jsonl python -m service.main
```

`DESK_STORE_PATH` 指定后事件落盘（JSONL），重启自动恢复。敏感配置请放在本地环境文件中。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/policies` | 登记保单（请求体即条款，可带 `policy_id`） |
| GET | `/policies/{id}` | 保单条款与批改 |
| POST | `/policies/{id}/endorsements` | 登记批改 `{changes, effective_at, note?}` |
| POST | `/reports` | 报案（`incident_ref` 相同的事故自动归并） |
| GET | `/cases` | 案件列表（按事故发生顺序） |
| GET | `/cases/{id}` | 案件视图：状态/赔付/责任人/下一步动作 |
| GET | `/cases/{id}/chain` | 处置链（审计用） |
| POST | `/cases/{id}/materials` · `/receipts` · `/milestones` · `/assessments` · `/prepayments` · `/payouts` · `/subrogation-receipts` · `/corrections` | 处置链事件 |

操作人取自请求体 `actor` 字段或 `X-Actor` 请求头；金额一律为最小货币单位的整数；时间戳必须带时区。

## 目录职责

- `service/desk/events.py` — 追加式事件存储（内存 / JSONL）
- `service/desk/core.py` — 领域核心：命令、保单快照、状态推导
- `service/desk/api.py` — HTTP 路由（仅标准库）
- `service/main.py` — 服务入口
- `tests/` — 领域场景与接口测试（含并发报案、事故后批改、迟到追偿等复盘场景）

## 测试

```bash
python -m unittest discover -s tests -v
```
