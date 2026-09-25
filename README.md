# 消费贷审慎额度管理

服务用于管理消费贷可负担性、提款额度和困难协商，在促消费与长期偿付风险之间保持清晰边界。

项目当前提供稳定的基础服务入口，便于本地联调和运维巡检。运行 `python3 service.py --check` 可核对服务配置；执行 `python3 service.py --port 8000` 后访问 `/health` 可确认服务身份。

## 协商承诺执行账本

困难协商方案批准后，催收系统不能再只盯着原合同逾期。账本在方案批准时把适用账户、减免项目、付款日历、联系限制和恢复原计划的条件一次性冻结进**不可变版本**，并让自动扣款、人工还款、商户退款走同一个资金入口，在同一余额边界内处理。

### 核心规则

- **方案冻结与版本化**：批准即冻结适用账户，承诺条款与当时披露随版本固化；失业状态更新、再次协商产生新版本（旧版本标记 `superseded`，条款与披露仍可查）；方案违约生成 `default` 版本并恢复原合同催收。
- **资金统一入口与确定性分配**：付款按 `(价值日, 资金编号)` 排序后逐期瀑布冲抵，结论只由方案规则与价值日决定，与资金到达顺序无关；未来价值日的资金不提前冲抵。
- **退款不算履约**：商户退款必须携带 `linked_purchase_id`，只能冲回方案适用账户下的关联消费，累计金额不能超过消费金额，拒绝入账时不会产生任何履约记录。
- **幂等与争议**：`fund_id` 相同且要素完全一致为重传，返回 `outcome=duplicate`；同编号但金额/来源/价值日/关联消费变化则开争议（`outcome=disputed`），原交易不动、新交易不入账，同一变异重传不重复开单。
- **部分到账宽限**：是否宽限由 `grace_rule`（宽限天数 + 最低金额/比例门槛）和当前日期决定，同样适用于并发与乱序场景。
- **联系限制**：催收联系人通过 contact-policy 只能看到当前版本允许的渠道、时段、静默期与禁扰日；越界尝试记录为 `blocked` 并附原因，在 explain 中向客户与合规可查。
- **事务与恢复**：写操作先在状态副本上执行再整体提交，失败状态完全回滚（无半次履约）；设置 `LEDGER_SNAPSHOT_PATH` 后每笔事务原子落盘，服务重启后调用 `POST /v1/recover` 从持久事实重建下一待办与联系抑制状态，资金幂等性不受影响。

### 接口一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/v1/accounts` | 开户（可登记关联消费与原合同联系策略） |
| GET | `/v1/accounts/{id}` | 账户状态（含冻结标记） |
| POST | `/v1/accounts/{id}/plans` | 批准方案 / 失业更新 / 再协商（`reason` 区分） |
| GET | `/v1/accounts/{id}/plans` | 全部版本列表（旧承诺与披露可查） |
| POST | `/v1/accounts/{id}/default` | 方案违约，恢复原计划 |
| POST | `/v1/accounts/{id}/funds` | 资金入账（自动扣款/人工/退款统一入口） |
| GET | `/v1/accounts/{id}/funds` | 资金列表 |
| GET | `/v1/accounts/{id}/disputes` | 账户下所有版本的争议 |
| GET | `/v1/accounts/{id}/payments` | 按价值日的逐期分配与宽限状态（`?as_of=`） |
| GET | `/v1/accounts/{id}/explain` | 客户/合规解释：已履、仍欠、暂缓本金、恢复时点、被阻止联系 |
| GET | `/v1/accounts/{id}/next-todo` | 重建后的下一待办 |
| GET | `/v1/accounts/{id}/contact-policy` | 当前有效的渠道/时段（`?at=`） |
| POST/GET | `/v1/accounts/{id}/contact-attempts` | 记录/查询联系尝试（越界记 blocked） |
| POST | `/v1/recover` | 服务恢复后重建待办与联系抑制 |

资金入账结果通过 `outcome` 区分 `posted` / `duplicate` / `disputed`；领域错误返回 `{"error": {"code", "message"}}` 与对应 HTTP 状态（400/404/409/422）。

### 示例

开户并批准三个月暂缓本金、月供降至 500 的方案：

```bash
curl -s -XPOST localhost:8000/v1/accounts/acct-1 -H 'Content-Type: application/json' -d '{
  "account_id": "acct-1",
  "standard_contact_policy": {"allowed_channels": ["phone","sms","email","letter"],
                              "allowed_hours": {"start": "08:00", "end": "21:00"}},
  "purchases": [{"purchase_id": "pur-1", "amount": "1200.00"}]
}'

curl -s -XPOST localhost:8000/v1/accounts/acct-1/plans -H 'Content-Type: application/json' -d '{
  "effective_date": "2026-04-01",
  "forbearance": {"principal_deferral_months": 3, "deferred_principal_amount": "3000.00"},
  "waivers": [{"code": "FEE-001", "amount": "50.00", "period_no": 1}],
  "payment_calendar": [
    {"period_no": 1, "due_date": "2026-04-10", "amount_due": "500.00"},
    {"period_no": 2, "due_date": "2026-05-11", "amount_due": "500.00"},
    {"period_no": 3, "due_date": "2026-06-10", "amount_due": "500.00"}
  ],
  "grace_rule": {"grace_days": 7, "minimum_ratio": "0.5"},
  "contact_policy": {"allowed_channels": ["sms","email"],
                     "allowed_hours": {"start": "09:00", "end": "18:00"}},
  "resumption": {"condition": "calendar_complete", "resume_date": "2026-08-01",
                 "detail": "三期履行后暂缓本金并入第 8 月账单"}
}'
```

自动扣款、人工还款与退款分别入账（注意退款需要关联消费）：

```bash
curl -s -XPOST localhost:8000/v1/accounts/acct-1/funds -H 'Content-Type: application/json' -d '{
  "fund_id": "tx-auto-1", "source": "autodraft",
  "amount": "450.00", "value_date": "2026-04-10"}'

curl -s -XPOST localhost:8000/v1/accounts/acct-1/funds -H 'Content-Type: application/json' -d '{
  "fund_id": "tx-manual-1", "source": "manual_repayment",
  "amount": "500.00", "value_date": "2026-05-11"}'

curl -s -XPOST localhost:8000/v1/accounts/acct-1/funds -H 'Content-Type: application/json' -d '{
  "fund_id": "tx-refund-1", "source": "merchant_refund",
  "amount": "100.00", "value_date": "2026-04-09", "linked_purchase_id": "pur-1"}'
```

逐期解释与联系策略：

```bash
curl -s 'localhost:8000/v1/accounts/acct-1/explain?as_of=2026-04-10'
curl -s 'localhost:8000/v1/accounts/acct-1/contact-policy?at=2026-04-05T10:00:00'
```

## 测试与构建

执行完整测试：

```bash
npm test
```

执行编译检查：

```bash
python3 -m compileall -q .
```

两条命令都可在单个 Linux 应用容器内直接运行，不需要额外服务。
