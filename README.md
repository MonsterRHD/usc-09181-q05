# 海外保单风险处置台

出口企业在多个国家投保货运险与政治风险险，理赔员在不同承保人门户重复登记同一事故，
导致赔付进度与追偿责任互相矛盾。本服务把理赔协作集中到**海外保单风险处置台**：
以**追加式事件日志（event sourcing）**为唯一事实源，把保单范围、地区、免赔额、
通知时限与事故证据统一到一条按事故归属的处置链上。

## 设计要点

- **追加不可变**：任何状态更正只能追加事件（`material_corrected` /
  `status_corrected`），原始文件摘要永不替换；被拒绝的操作也写 `rejected` 事件留痕。
- **重复通知可解释**：按「保单+险种+事故时间(UTC)+航次/标的」生成事故指纹，
  跨承保人门户、跨时区重复报案落为 `DUPLICATE` 并指向主事故，不产生第二条赔付线。
- **跨时区可比**：所有带时区时间入库即归一化为 UTC；事故列表按事故发生时间排序，
  断网补传、服务重启重放后仍自动归位。
- **范围/时限研判**：保单批改按「事故发生时」是否生效判定（事故后加保不追溯）；
  超 48h 通知落 `NOTICE_LATE` 可解释状态；超出范围落 `OUT_OF_SCOPE`，赔付冻结。
- **材料管控**：只有该险种认可的证据类型能推进；超范围材料登记留痕但**阻断定损/赔付**。
- **紧急预付**：必须记录授权人与授权上限，累计预付不得突破上限与可赔余额
  （可赔 = 定损 − 免赔额 − 已预付/已付）。
- **断网补传**：客户端带 `client_event_id` 幂等键，重传返回首次结果，不重复登记。
- **并发安全**：关键命令在事件存储全局临界区内完成「重放校验 → 追加」。
- **可审计**：`GET /audit` 给出完整事件链（seq 单调）+ 每起事故的金额、责任人、
  阻塞原因与下一步动作；迟到追偿回执以 `late=true` 补录，归属不变。

## 目录

- `service/domain/events.py` — 事件定义与 UTC 时间工具
- `service/domain/store.py` — 追加式事件存储（JSONL 持久化 + 重放恢复 + 幂等补传）
- `service/domain/models.py` — 保单/事故状态模型与状态枚举
- `service/domain/projection.py` — 事件流 → 当前状态投影（fold 重建）
- `service/domain/service.py` — 全部业务规则与审计 DTO
- `service/main.py` — HTTP 入口（标准库无第三方依赖）
- `tests/test_desk.py` — 覆盖复盘场景的 11 项测试

## 运行

```bash
python3 -m service.main            # 默认 data/eventstore.jsonl，PORT 默认 8000
python3 -m unittest discover -s tests -v
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/policies` | 登记保单（险种/地区/免赔额/通知时限） |
| POST | `/policies/{id}/endorsements` | 保单批改（生效时间决定对事故是否适用） |
| POST | `/incidents` | 报案（重复指纹→DUPLICATE；支持 `client_event_id` 幂等补传） |
| POST | `/incidents/{id}/scope` | 范围与通知时限研判 |
| POST | `/incidents/{id}/materials` | 接收承保人回执/客户材料/证据 |
| POST | `/incidents/{id}/materials/{mid}/corrections` | 材料摘要更正（追加，不覆盖原件） |
| POST | `/incidents/{id}/partial-loss` | 部分损失申报 |
| POST | `/incidents/{id}/adjust` | 定损（记录理赔员、快照免赔额） |
| POST | `/incidents/{id}/advances` | 紧急预付授权（授权人 + 上限） |
| POST | `/incidents/{id}/payments` | 赔付（按可赔余额校验，重复事故禁赔） |
| POST | `/incidents/{id}/acknowledgements` | 承保人回执 |
| POST | `/incidents/{id}/investigation` | 调查节点 |
| POST | `/incidents/{id}/subrogation` | 开立追偿（责任方/目标金额/责任人） |
| POST | `/incidents/{id}/recoveries` | 追偿到账回执（`late=true` 迟到补录） |
| POST | `/incidents/{id}/close` | 结案 |
| POST | `/incidents/{id}/corrections` | 状态更正（追加） |
| GET | `/incidents?order=occurrence` | 事故列表（默认按事故发生时间 UTC 排序） |
| GET | `/incidents/{id}` | 事故详情（含金额、责任人、下一步动作） |
| GET | `/audit` | 复盘：完整处置链 + 事故审计视图 |

规则违例返回 `422 {"code","message"}`；违例同时写入事件链。规则细节以
`service/domain/service.py` 为准。
