# 消费贷审慎额度管理

服务用于管理消费贷可负担性、提款额度和困难协商，在促消费与长期偿付风险之间保持清晰边界。

当前模块为**协商承诺执行账本**：客户接受"暂缓本金 + 降低月供"的困难方案后，自动扣款、人工还款、商户退款仍从不同渠道进入。账本在方案批准时冻结全部承诺条款，让每笔资金在同一余额边界内按业务规则入账，避免催收系统只看到原合同逾期、"一边履约一边继续被联系"。

## 核心规则

* **方案即冻结**：批准时冻结适用账户（冻结期不得新增消费）、减免项目、付款日历、联系限制（渠道/时段/禁联日/每日上限）、恢复原计划条件，以及向客户披露的文本与时间。
* **资金按价值日确定性分配**：同一价值日内按业务来源（自动扣款先于人工还款）再按交易标识排序，结论与调用先后、实际到达时间无关。
* **退款只冲关联消费**：商户退款必须指定原消费且不得超过可冲余额，永远不计为客户履约。
* **幂等与争议**：相同交易（幂等键 + 内容哈希）重传不重复入账；标识相同但金额/来源/价值日/关联消费变化，挂起争议并维持原入账。
* **部分到账宽限**：是否宽限只由方案的宽限天数与最低比例决定，不被调用顺序改变；宽限期满仍不足额即转逾期。
* **版本可追溯**：失业状态更新、再次协商生成新版本（旧版本标记 `superseded` 但条款与披露永久可查）；方案违约生成 `RESTORED_ORIGINAL` 版本并解除联系抑制。
* **同一余额边界**：校验与事件追加在同一把锁内完成，并发扣款/人工入账不会产生半次履约；同键竞争只有一笔入账成功。
* **崩溃可重建**：状态全部由 append-only 事件（JSONL，落盘 fsync）重放得到，服务恢复后下一期待办、联系抑制、幂等索引、争议状态一致恢复。
* **可解释**：`explain` 视图逐期回答"为何已履行、仍欠多少、何时恢复普通还款、哪些联系曾被阻止"。

## 运行

```bash
python3 service.py --check            # 配置自检
python3 service.py --port 8000        # 启动，事件日志默认写入 ./data
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/accounts` | 开户 |
| POST | `/accounts/<id>/purchases` | 登记关联消费（冻结期拒绝） |
| POST | `/accounts/<id>/plans` | 批准困难方案 / 状态更新 / 再次协商（生成新版本） |
| POST | `/accounts/<id>/default` | 记录方案违约，恢复原合同版本 |
| POST | `/accounts/<id>/funds` | 资金入账（`AUTO_DEBIT` / `MANUAL` / `MERCHANT_REFUND`） |
| POST | `/accounts/<id>/contacts` | 催收联系尝试，按当前策略判定并留痕（含被阻止的） |
| GET | `/accounts/<id>/periods?as_of=` | 逐期应付/已付/欠付/宽限状态 |
| GET | `/accounts/<id>/next-todo?as_of=` | 重建的下一期待办 |
| GET | `/accounts/<id>/contact-view?ts=` | 催收端：仅当前允许的渠道与时段 |
| GET | `/accounts/<id>/versions[/{n}]` | 全部（或指定）承诺版本及当时披露 |
| GET | `/accounts/<id>/disputes` | 争议清单 |
| GET | `/accounts/<id>/explain?as_of=` | 客户/合规完整解释视图 |

### 资金入账示例

```bash
curl -X POST localhost:8000/accounts/acct-1/funds -H 'Content-Type: application/json' -d '{
  "posting_id": "f-20260501-01",
  "idempotency_key": "channel-x:txn-7788",
  "source": "AUTO_DEBIT",
  "amount": "1000.00",
  "value_date": "2026-05-01"
}'
# 同键同体重传 -> 200 {"result": {"duplicate": true, ...}}，不重复入账
# 同键金额/来源变化 -> 409 dispute_opened，原入账保持有效

curl -X POST localhost:8000/accounts/acct-1/funds -H 'Content-Type: application/json' -d '{
  "posting_id": "rf-01",
  "idempotency_key": "refund-01",
  "source": "MERCHANT_REFUND",
  "amount": "300.00",
  "value_date": "2026-05-02",
  "purchase_id": "p-1"
}'
```

## 测试与构建

执行完整测试（领域规则 + HTTP 契约 + 健康检查，共 32 项）：

```bash
npm test
```

执行编译检查：

```bash
python3 -m compileall -q .
```

两条命令都可在单个 Linux 应用容器内直接运行，不需要额外服务。事件日志目录 `data/` 为运行时数据，不入库。
