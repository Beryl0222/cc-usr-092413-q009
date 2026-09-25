"""协商承诺执行账本。

方案批准后，账本冻结适用账户并固化减免项目、付款日历、联系限制与恢复
原计划的条件；所有资金（自动扣款、人工还款、商户退款）都在同一余额
边界内按业务来源与价值日入账。

设计约束：

* 资金编号相同视为同一笔交易重传：要素完全一致则幂等返回，金额或来源
  变化则进入争议，绝不重复入账；
* 商户退款只能冲回其关联消费，永远不计入客户履约；
* 每期是否宽限只取决于方案规则、价值日与金额，不取决于调用到达顺序；
* 失业状态更新、再次协商、方案违约都会产生不可变的新版本，旧承诺与
  当时披露长期可查；
* 催收联系人只能看到当前有效的联系渠道与时段，越界尝试会被记录为
  已阻止；
* 每次写操作都是一个事务：先在草稿上执行，成功后整体提交并落盘，
  失败不留下半次履约。
"""

import copy
import fcntl
import json
import os
import threading
from datetime import date, datetime, time, timedelta
from decimal import Decimal

LEDGER_SNAPSHOT_VERSION = 1

# 资金业务来源。
SOURCE_AUTODRAFT = "autodraft"          # 自动扣款
SOURCE_MANUAL = "manual_repayment"      # 人工还款
SOURCE_REFUND = "merchant_refund"       # 商户退款
PAYMENT_SOURCES = (SOURCE_AUTODRAFT, SOURCE_MANUAL)

# 版本产生原因。
REASON_APPROVAL = "hardship_approval"
REASON_UNEMPLOYMENT = "unemployment_update"
REASON_RENEGOTIATION = "renegotiation"
REASON_DEFAULT = "default"
HARDSHIP_REASONS = (REASON_APPROVAL, REASON_UNEMPLOYMENT, REASON_RENEGOTIATION)

# 版本状态。
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_BREACHED = "breached"
STATUS_COMPLETED = "completed"


class LedgerError(Exception):
    """领域错误，携带稳定错误码与 HTTP 状态。"""

    status = 400
    code = "bad_request"

    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status


class NotFoundError(LedgerError):
    status = 404
    code = "not_found"


class ConflictError(LedgerError):
    status = 409
    code = "conflict"


class UnprocessableError(LedgerError):
    status = 422
    code = "unprocessable"


# ---------------------------------------------------------------------------
# 基础类型解析
# ---------------------------------------------------------------------------

def to_cents(value, field="amount"):
    """把金额转换成整数分，拒绝超过两位小数或为负的输入。"""
    if isinstance(value, bool):
        raise LedgerError(f"{field} 必须是金额")
    try:
        decimal_value = Decimal(str(value))
    except Exception as exc:
        raise LedgerError(f"{field} 必须是金额") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise LedgerError(f"{field} 不能为负")
    cents = (decimal_value * 100).to_integral_value()
    if cents / 100 != decimal_value:
        raise LedgerError(f"{field} 最多保留两位小数")
    cents = int(cents)
    if cents <= 0:
        raise LedgerError(f"{field} 必须大于零")
    return cents


def cents_to_money(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def parse_day(value, field="date"):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def parse_moment(value, field="at"):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"{field} 必须是 ISO-8601 时间") from exc


def parse_hhmm(value, field):
    try:
        hour, minute = str(value).split(":")
        parsed = time(int(hour), int(minute))
    except (ValueError, AttributeError) as exc:
        raise LedgerError(f"{field} 必须是 HH:MM") from exc
    return parsed


def _today():
    return date.today()


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------

class Ledger:
    """协商承诺执行账本；所有方法都在同一余额边界内串行提交。"""

    def __init__(self, path=None):
        self.path = path
        self._lock = threading.RLock()
        self._file_handle = None
        self._state = self._empty_state()
        if path:
            self._load()

    @staticmethod
    def _empty_state():
        return {
            "snapshot_version": LEDGER_SNAPSHOT_VERSION,
            "accounts": {},
            "plans": [],
            "plans_by_id": {},
            "funds": {},
            "fund_index": {},          # fund_id -> (account_id, fund_id)
            "disputes": {},
            "contact_attempts": {},
            "counters": {"plan": 0, "dispute": 0},
        }

    # -- 持久化与事务 -------------------------------------------------------

    def _load(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        # 用一个长期持有的文件锁协调同机多进程；线程内再用 RLock。
        self._file_handle = open(self.path, "a+", encoding="utf-8")
        fcntl.flock(self._file_handle.fileno(), fcntl.LOCK_EX)
        self._file_handle.seek(0)
        raw = self._file_handle.read()
        if raw.strip():
            state = json.loads(raw)
            if state.get("snapshot_version") != LEDGER_SNAPSHOT_VERSION:
                raise LedgerError("账本快照版本不受支持", "bad_snapshot", 500)
            state.setdefault("plans_by_id", {
                plan["version_id"]: plan for plan in state.get("plans", [])
            })
            state.setdefault("fund_index", {})
            self._state = state
        else:
            self._persist_locked(self._state)

    def _persist_locked(self, state):
        if not self.path:
            return
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)

    class _Transaction:
        def __init__(self, ledger):
            self.ledger = ledger
            self.draft = None

        def __enter__(self):
            self.ledger._lock.acquire()
            # 在副本上执行；失败时原状态不受影响，不会留下半次履约。
            self.draft = copy.deepcopy(self.ledger._state)
            return self.draft

        def __exit__(self, exc_type, exc, _tb):
            try:
                if exc_type is None:
                    self.ledger._state = self.draft
                    self.ledger._persist_locked(self.draft)
            finally:
                self.ledger._lock.release()
            return False

    def transaction(self):
        return self._Transaction(self)

    def shutdown(self):
        if self._file_handle is not None:
            fcntl.flock(self._file_handle.fileno(), fcntl.LOCK_UN)
            self._file_handle.close()
            self._file_handle = None

    # -- 查询辅助（读已提交状态的深拷贝，避免调用方篡改） -------------------

    def _account(self, state, account_id):
        account = state["accounts"].get(account_id)
        if account is None:
            raise NotFoundError(f"账户 {account_id} 不存在")
        return account

    def _current_plan(self, state, account_id):
        account = self._account(state, account_id)
        plan_id = account.get("current_version_id")
        if plan_id is None:
            return None
        return state["plans_by_id"][plan_id]

    def _require_current_plan(self, state, account_id):
        plan = self._current_plan(state, account_id)
        if plan is None:
            raise ConflictError(
                f"账户 {account_id} 当前没有生效的协商承诺", "no_active_plan"
            )
        return plan

    def _public_account(self, account):
        return copy.deepcopy(account)

    # -- 账户与方案 ---------------------------------------------------------

    def create_account(self, payload):
        account_id = payload.get("account_id")
        if not account_id or not isinstance(account_id, str):
            raise LedgerError("account_id 必填")
        currency = payload.get("currency", "CNY")
        purchases = []
        for item in payload.get("purchases", []):
            purchases.append(
                {
                    "purchase_id": require_text(item, "purchase_id", "消费标识"),
                    "amount": to_cents(item.get("amount"), "消费金额"),
                    "refunded": 0,
                }
            )
        policy = payload.get("standard_contact_policy")
        if policy is not None:
            _validate_contact_policy(policy)
        with self.transaction() as state:
            if account_id in state["accounts"]:
                raise ConflictError(f"账户 {account_id} 已存在", "account_exists")
            state["accounts"][account_id] = {
                "account_id": account_id,
                "currency": currency,
                "frozen": False,
                "current_version_id": None,
                "standard_contact_policy": copy.deepcopy(policy),
                "purchases": purchases,
                "created_at": _now_iso(),
            }
        return self.get_account(account_id)

    def get_account(self, account_id):
        with self._lock:
            account = self._account(self._state, account_id)
            return self._public_account(account)

    def approve_plan(self, account_id, payload):
        """批准协商方案（或因失业更新/再协商产生新版本）。"""
        reason = payload.get("reason", REASON_APPROVAL)
        if reason not in HARDSHIP_REASONS:
            raise LedgerError(
                "reason 必须是 hardship_approval/unemployment_update/renegotiation"
            )
        effective_date = parse_day(payload.get("effective_date"), "effective_date")
        calendar = _parse_calendar(payload.get("payment_calendar"))
        grace_rule = _parse_grace_rule(payload.get("grace_rule"))
        waivers = _parse_waivers(payload.get("waivers", []))
        contact_policy = payload.get("contact_policy")
        if contact_policy is None:
            raise LedgerError("contact_policy 必填：方案必须明确联系限制")
        _validate_contact_policy(contact_policy)
        resumption = _parse_resumption(payload.get("resumption"))
        forbearance = _parse_forbearance(payload.get("forbearance", {}))
        applicable = payload.get("applicable_account_ids") or [account_id]
        if account_id not in applicable:
            raise LedgerError("适用账户必须包含履约账户本身")
        calendar_periods = {row["period_no"] for row in calendar}
        for waiver in waivers:
            if waiver["period_no"] not in calendar_periods:
                raise LedgerError(
                    f"减免 {waiver['code']} 指向的分期 {waiver['period_no']} 不在付款日历中"
                )
            target = next(
                row for row in calendar if row["period_no"] == waiver["period_no"]
            )
            if waiver["amount_cents"] > target["amount_due_cents"]:
                raise LedgerError(f"减免 {waiver['code']} 金额超过对应分期应还金额")

        with self.transaction() as state:
            owner = self._account(state, account_id)
            for applicable_id in applicable:
                self._account(state, applicable_id)
            parent_id = payload.get("parent_version_id")
            parent = None
            current = self._current_plan(state, account_id)
            if current is not None:
                if reason == REASON_APPROVAL:
                    raise ConflictError(
                        "账户已存在生效承诺，再次协商请使用 renegotiation",
                        "plan_already_active",
                    )
                if current["reason"] == REASON_DEFAULT:
                    raise ConflictError(
                        "原计划已因违约恢复，不能在违约版本上直接续作", "plan_breached"
                    )
                if parent_id is not None and current["version_id"] != parent_id:
                    raise ConflictError(
                        "parent_version_id 与当前版本不一致", "version_conflict"
                    )
                parent = current
            elif reason != REASON_APPROVAL:
                raise ConflictError(
                    "账户尚无承诺版本，首个版本必须是 hardship_approval",
                    "no_active_plan",
                )

            # 冻结适用账户：冻结期间原合同逾期视图被承诺版本接管。
            owner_current_id = owner.get("current_version_id")
            for applicable_id in applicable:
                applicable_account = state["accounts"][applicable_id]
                if applicable_id != account_id:
                    bound = applicable_account.get("current_version_id")
                    if bound is not None and bound != owner_current_id:
                        raise ConflictError(
                            f"适用账户 {applicable_id} 已绑定其他承诺版本",
                            "applicable_account_busy",
                        )
                applicable_account["frozen"] = True

            state["counters"]["plan"] += 1
            sequence = state["counters"]["plan"]
            version_id = f"plan-{account_id}-v{sequence}"
            plan = {
                "version_id": version_id,
                "account_id": account_id,
                "sequence": sequence,
                "reason": reason,
                "parent_version_id": parent["version_id"] if parent else None,
                "status": STATUS_ACTIVE,
                "created_at": _now_iso(),
                "effective_date": effective_date.isoformat(),
                "applicable_account_ids": list(applicable),
                "forbearance": forbearance,
                "waivers": waivers,
                "payment_calendar": calendar,
                "grace_rule": grace_rule,
                "contact_policy": copy.deepcopy(contact_policy),
                "resumption": resumption,
                "fund_ids": [],
                "disclosure": payload.get("disclosure")
                or _build_disclosure(
                    reason, effective_date, forbearance, waivers, calendar,
                    grace_rule, contact_policy, resumption,
                ),
            }
            state["plans"].append(plan)
            state.setdefault("plans_by_id", {})[version_id] = plan
            if parent is not None:
                parent["status"] = STATUS_SUPERSEDED
                parent["superseded_by"] = version_id
            for applicable_id in applicable:
                state["accounts"][applicable_id]["current_version_id"] = version_id
            result = copy.deepcopy(plan)
        return result

    def record_default(self, account_id, payload):
        """方案违约：旧承诺标记违约，生成恢复原计划的新版本。"""
        at = parse_day(payload.get("at"), "at")
        with self.transaction() as state:
            current = self._require_current_plan(state, account_id)
            if current["reason"] == REASON_DEFAULT:
                raise ConflictError("账户已处于违约恢复版本", "plan_breached")
            owner = self._account(state, account_id)
            standard_policy = owner.get("standard_contact_policy")
            if standard_policy is None:
                raise UnprocessableError(
                    "账户缺少原合同联系策略，无法恢复普通催收", "missing_standard_policy"
                )
            state["counters"]["plan"] += 1
            sequence = state["counters"]["plan"]
            version_id = f"plan-{account_id}-v{sequence}"
            restored = {
                "version_id": version_id,
                "account_id": account_id,
                "sequence": sequence,
                "reason": REASON_DEFAULT,
                "parent_version_id": current["version_id"],
                "status": STATUS_ACTIVE,
                "created_at": _now_iso(),
                "effective_date": at.isoformat(),
                "applicable_account_ids": list(current["applicable_account_ids"]),
                "forbearance": {},
                "waivers": [],
                "payment_calendar": [],
                "grace_rule": None,
                "contact_policy": copy.deepcopy(standard_policy),
                "resumption": {
                    "condition": "original_contract_resumed",
                    "resume_date": at.isoformat(),
                    "detail": "方案违约，自即日起恢复原合同还款与普通催收",
                },
                "fund_ids": [],
                "disclosure": {
                    "type": "default_notice",
                    "breached_version": current["version_id"],
                    "resumed_at": at.isoformat(),
                },
            }
            state["plans"].append(restored)
            state.setdefault("plans_by_id", {})[version_id] = restored
            current["status"] = STATUS_BREACHED
            current["superseded_by"] = version_id
            for applicable_id in current["applicable_account_ids"]:
                account = state["accounts"][applicable_id]
                account["frozen"] = False
                account["current_version_id"] = version_id
            return copy.deepcopy(restored)

    def list_plans(self, account_id):
        with self._lock:
            self._account(self._state, account_id)
            return [
                copy.deepcopy(plan)
                for plan in self._state["plans"]
                if plan["account_id"] == account_id
                or account_id in plan.get("applicable_account_ids", [])
            ]

    # -- 资金入账 -----------------------------------------------------------

    def post_fund(self, account_id, payload):
        """资金统一入口：自动扣款、人工还款、商户退款走同一余额边界。"""
        fund_id = require_text(payload, "fund_id", "资金交易编号")
        source = require_text(payload, "source", "业务来源")
        if source not in (SOURCE_AUTODRAFT, SOURCE_MANUAL, SOURCE_REFUND):
            raise LedgerError(
                "source 必须是 autodraft/manual_repayment/merchant_refund"
            )
        amount = to_cents(payload.get("amount"))
        value_date = parse_day(payload.get("value_date"), "value_date")
        linked_purchase = payload.get("linked_purchase_id")
        if source == SOURCE_REFUND and not linked_purchase:
            raise UnprocessableError(
                "商户退款必须携带 linked_purchase_id，且只能冲回关联消费",
                "refund_requires_purchase",
            )
        if source != SOURCE_REFUND and linked_purchase:
            raise LedgerError("只有商户退款才能关联消费")

        with self.transaction() as state:
            account = self._account(state, account_id)
            existing = state["fund_index"].get(fund_id)
            if existing is not None:
                return self._handle_retransmission(
                    state, account_id, fund_id, existing, source, amount,
                    value_date, linked_purchase,
                )

            plan = self._require_current_plan(state, account_id)
            if plan["reason"] == REASON_DEFAULT:
                raise ConflictError(
                    "方案已违约并恢复原合同，承诺账本不再接受该账户入账",
                    "plan_breached",
                )

            if source == SOURCE_REFUND:
                purchase = self._find_purchase(state, linked_purchase, plan)
                if purchase is None:
                    raise UnprocessableError(
                        f"退款关联消费 {linked_purchase} 不存在或不属于方案适用账户",
                        "purchase_not_found",
                    )
                if purchase["refunded"] + amount > purchase["amount"]:
                    raise UnprocessableError(
                        "退款累计金额超过关联消费金额，拒绝入账",
                        "refund_exceeds_purchase",
                    )
                purchase["refunded"] += amount
                allocation = [{
                    "target": "refund_chargeback",
                    "purchase_id": purchase["purchase_id"],
                    "amount": cents_to_money(amount),
                }]
            else:
                allocation = []  # 履约分配由价值日投影统一计算，见 project_payments。

            fund = {
                "fund_id": fund_id,
                "account_id": account_id,
                "plan_version_id": plan["version_id"],
                "source": source,
                "amount_cents": amount,
                "amount": cents_to_money(amount),
                "value_date": value_date.isoformat(),
                "linked_purchase_id": linked_purchase,
                "status": "posted",
                "posted_at": _now_iso(),
                "allocation_at_post": allocation,
            }
            state["funds"][fund_id] = fund
            state["fund_index"][fund_id] = (account_id, fund_id)
            plan["fund_ids"].append(fund_id)
            self._complete_if_done(state, plan)
            return self._fund_view(state, fund)

    def _handle_retransmission(self, state, account_id, fund_id, existing,
                               source, amount, value_date, linked_purchase):
        owner_id, existing_id = existing
        if owner_id != account_id:
            raise ConflictError(
                f"资金编号 {fund_id} 已属于其他账户", "fund_id_cross_account"
            )
        original = state["funds"][existing_id]
        same_attributes = (
            original["source"] == source
            and original["amount_cents"] == amount
            and original["value_date"] == value_date.isoformat()
            and original.get("linked_purchase_id") == linked_purchase
        )
        if same_attributes:
            view = self._fund_view(state, original)
            view["outcome"] = "duplicate"
            return view

        # 编号相同但金额或来源变化：进入争议，原交易不动、新交易不入账。
        disputes = state["disputes"].setdefault(original["plan_version_id"], [])
        for dispute in disputes:
            if dispute["fund_id"] == fund_id and dispute["status"] == "open":
                view = self._fund_view(state, original)
                view["outcome"] = "disputed"
                view["dispute_id"] = dispute["dispute_id"]
                return view
        state["counters"]["dispute"] += 1
        dispute = {
            "dispute_id": f"disp-{state['counters']['dispute']}",
            "fund_id": fund_id,
            "plan_version_id": original["plan_version_id"],
            "status": "open",
            "original": {
                "source": original["source"],
                "amount": original["amount"],
                "value_date": original["value_date"],
                "linked_purchase_id": original.get("linked_purchase_id"),
            },
            "incoming": {
                "source": source,
                "amount": cents_to_money(amount),
                "value_date": value_date.isoformat(),
                "linked_purchase_id": linked_purchase,
            },
            "created_at": _now_iso(),
        }
        disputes.append(dispute)
        view = self._fund_view(state, original)
        view["outcome"] = "disputed"
        view["dispute_id"] = dispute["dispute_id"]
        return view

    def _find_purchase(self, state, purchase_id, plan):
        for applicable_id in plan["applicable_account_ids"]:
            for purchase in state["accounts"][applicable_id].get("purchases", []):
                if purchase["purchase_id"] == purchase_id:
                    return purchase
        return None

    def list_funds(self, account_id):
        with self._lock:
            self._account(self._state, account_id)
            return [
                self._fund_view(self._state, self._state["funds"][fid])
                for fid, (owner, _key) in self._state["fund_index"].items()
                if owner == account_id
            ]

    def list_disputes(self, account_id):
        with self._lock:
            plan = self._current_plan(self._state, account_id)
            if plan is None:
                return []
            result = []
            for version in self._state["plans"]:
                if version["account_id"] == account_id:
                    result.extend(
                        copy.deepcopy(self._state["disputes"].get(version["version_id"], []))
                    )
            return result

    def _fund_view(self, state, fund):
        view = copy.deepcopy(fund)
        view.pop("amount_cents", None)
        view.setdefault("outcome", "posted")
        return view

    # -- 履约投影（与调用顺序无关） -----------------------------------------

    def _plan_funds(self, state, plan):
        funds = [state["funds"][fid] for fid in plan["fund_ids"]]
        return [f for f in funds if f["status"] == "posted"]

    def project_payments(self, account_id, as_of=None):
        """按价值日对全部付款做确定性瀑布分配。

        排序键为 (价值日, 资金编号)，与交易到达顺序无关；退款根本不参与
        履约分配。返回每期资金明细与宽限判定。
        """
        as_of_day = parse_day(as_of, "as_of") if as_of else _today()
        with self._lock:
            plan = self._current_plan(self._state, account_id)
            if plan is None:
                raise NotFoundError(f"账户 {account_id} 没有承诺版本")
            return self._project_locked(self._state, plan, as_of_day)

    def _project_locked(self, state, plan, as_of_day):
        periods = {
            row["period_no"]: {
                "period_no": row["period_no"],
                "due_date": row["due_date"],
                "due_cents": row["amount_due_cents"],
                "waiver_cents": 0,
                "paid_cents": 0,
                "contributions": [],
            }
            for row in plan["payment_calendar"]
        }
        for waiver in plan["waivers"]:
            if waiver.get("period_no") is not None:
                periods[waiver["period_no"]]["waiver_cents"] += waiver["amount_cents"]

        payments = sorted(
            (
                fund for fund in self._plan_funds(state, plan)
                if fund["source"] in PAYMENT_SOURCES
            ),
            key=lambda fund: (fund["value_date"], fund["fund_id"]),
        )
        refunds = [
            fund for fund in self._plan_funds(state, plan)
            if fund["source"] == SOURCE_REFUND
        ]

        unapplied = []
        postdated = []
        ordered_periods = sorted(periods.values(), key=lambda p: (p["due_date"], p["period_no"]))
        for fund in payments:
            value_day = parse_day(fund["value_date"])
            if value_day > as_of_day:
                # 价值日未到的资金不提前冲抵任何分期。
                postdated.append({
                    "fund_id": fund["fund_id"],
                    "source": fund["source"],
                    "value_date": fund["value_date"],
                    "amount": fund["amount"],
                })
                continue
            remaining = fund["amount_cents"]
            for period in ordered_periods:
                need = period["due_cents"] - period["waiver_cents"] - period["paid_cents"]
                if need <= 0:
                    continue
                applied = min(need, remaining)
                period["paid_cents"] += applied
                remaining -= applied
                period["contributions"].append({
                    "fund_id": fund["fund_id"],
                    "source": fund["source"],
                    "value_date": fund["value_date"],
                    "counts_as_performance": True,
                    "amount": cents_to_money(applied),
                })
                if remaining == 0:
                    break
            if remaining > 0:
                unapplied.append({
                    "fund_id": fund["fund_id"],
                    "source": fund["source"],
                    "value_date": fund["value_date"],
                    "amount": cents_to_money(remaining),
                })

        period_views = []
        for period in ordered_periods:
            period_views.append(
                self._period_view(period, plan.get("grace_rule"), as_of_day)
            )
        return {
            "account_id": plan["account_id"],
            "version_id": plan["version_id"],
            "as_of": as_of_day.isoformat(),
            "periods": period_views,
            "unapplied": unapplied,
            "postdated": postdated,
            "refund_chargebacks": [
                {
                    "fund_id": fund["fund_id"],
                    "purchase_id": fund["linked_purchase_id"],
                    "value_date": fund["value_date"],
                    "amount": fund["amount"],
                    "counts_as_performance": False,
                }
                for fund in sorted(refunds, key=lambda f: (f["value_date"], f["fund_id"]))
            ],
        }

    def _period_view(self, period, grace_rule, as_of_day):
        due = period["due_cents"]
        paid = period["paid_cents"]
        waived = period["waiver_cents"]
        due_day = parse_day(period["due_date"])
        satisfied = paid + waived >= due
        status = "scheduled"
        if satisfied:
            status = "satisfied"
        elif as_of_day >= due_day:
            grace_days = grace_rule["grace_days"] if grace_rule else 0
            threshold = _minimum_cents(due, grace_rule)
            grace_end = due_day + timedelta(days=grace_days)
            if as_of_day <= grace_end and paid + waived >= threshold:
                status = "within_grace"
            elif as_of_day > grace_end:
                status = "past_due"
            else:
                status = "due"
        outstanding = max(0, due - paid - waived)
        return {
            "period_no": period["period_no"],
            "due_date": period["due_date"],
            "amount_due": cents_to_money(due),
            "paid": cents_to_money(paid),
            "waived": cents_to_money(waived),
            "outstanding": cents_to_money(outstanding),
            "status": status,
            "funds": copy.deepcopy(period["contributions"]),
        }

    def explain(self, account_id, as_of=None):
        """面向客户与合规的逐期解释：为何已履行、仍欠多少、何时恢复。"""
        as_of_day = parse_day(as_of, "as_of") if as_of else _today()
        with self._lock:
            state = self._state
            plans = [
                plan for plan in state["plans"]
                if plan["account_id"] == account_id
            ]
            if not plans:
                raise NotFoundError(f"账户 {account_id} 没有承诺版本")
            current = self._current_plan(state, account_id)
            projection = self._project_locked(state, current, as_of_day)
            blocked = [
                attempt for attempt in state["contact_attempts"].get(account_id, [])
                if attempt["decision"] == "blocked"
            ]
            total_paid = sum(parse_money(p["paid"]) for p in projection["periods"])
            total_waived = sum(
                parse_money(p["waived"]) for p in projection["periods"]
            )
            total_outstanding = sum(
                parse_money(p["outstanding"]) for p in projection["periods"]
            )
            return {
                "account_id": account_id,
                "current_version_id": current["version_id"],
                "plan_status": current["status"],
                "reason": current["reason"],
                "totals": {
                    "due": cents_to_money(
                        sum(row["amount_due_cents"] for row in current["payment_calendar"])
                    ),
                    "paid": cents_to_money(total_paid),
                    "waived": cents_to_money(total_waived),
                    "outstanding": cents_to_money(total_outstanding),
                    "unapplied": cents_to_money(
                        sum(parse_money(x["amount"]) for x in projection["unapplied"])
                    ),
                    "postdated": cents_to_money(
                        sum(parse_money(x["amount"]) for x in projection.get("postdated", []))
                    ),
                    "deferred_principal_at_resumption": cents_to_money(
                        current["forbearance"].get("deferred_principal_cents", 0)
                    ),
                },
                "resumption": current["resumption"],
                "periods": projection["periods"],
                "unapplied": projection["unapplied"],
                "postdated": projection.get("postdated", []),
                "refund_chargebacks": projection["refund_chargebacks"],
                "blocked_contacts": copy.deepcopy(blocked),
                "as_of": as_of_day.isoformat(),
            }

    def _project_period_rows(self, plan):
        return plan["payment_calendar"]

    def next_todo(self, account_id, as_of=None):
        """重建下一待办：下一个未足额履行的分期与恢复普通还款时点。"""
        as_of_day = parse_day(as_of, "as_of") if as_of else _today()
        with self._lock:
            plan = self._require_current_plan(self._state, account_id)
            projection = self._project_locked(self._state, plan, as_of_day)
            next_period = next(
                (p for p in projection["periods"] if p["status"] != "satisfied"),
                None,
            )
            return {
                "account_id": account_id,
                "version_id": plan["version_id"],
                "plan_status": plan["status"],
                "next_period": next_period,
                "resumption": plan["resumption"],
                "frozen": self._account(self._state, account_id)["frozen"],
                "as_of": as_of_day.isoformat(),
            }

    def _complete_if_done(self, state, plan):
        if not plan["payment_calendar"] or plan["reason"] == REASON_DEFAULT:
            return
        as_of_day = _today()
        projection = self._project_locked(state, plan, as_of_day)
        if all(p["status"] == "satisfied" for p in projection["periods"]):
            plan["status"] = STATUS_COMPLETED
            plan["completed_at"] = _now_iso()
            for applicable_id in plan["applicable_account_ids"]:
                state["accounts"][applicable_id]["frozen"] = False

    # -- 联系限制 -----------------------------------------------------------

    def effective_contact_policy(self, account_id, at=None):
        """催收联系人只能取到当前允许的渠道与时段。"""
        moment = parse_moment(at) if at else datetime.now()
        with self._lock:
            account = self._account(self._state, account_id)
            plan = self._current_plan(self._state, account_id)
            if plan is None or plan["status"] in (STATUS_COMPLETED, STATUS_BREACHED) \
                    or plan["reason"] == REASON_DEFAULT:
                policy = account.get("standard_contact_policy")
                source = "standard_contract"
                version_id = plan["version_id"] if plan else None
                if policy is None:
                    raise NotFoundError("账户没有可用的联系策略")
            else:
                policy = plan["contact_policy"]
                source = "hardship_plan"
                version_id = plan["version_id"]
            return {
                "account_id": account_id,
                "version_id": version_id,
                "source": source,
                "frozen": account["frozen"],
                "allowed_channels": list(policy["allowed_channels"]),
                "allowed_hours": copy.deepcopy(policy.get("allowed_hours")),
                "suppressed_until": policy.get("suppressed_until"),
                "blackout_dates": list(policy.get("blackout_dates", [])),
                "evaluated_at": moment.isoformat(),
            }

    def record_contact_attempt(self, account_id, payload):
        attempt_id = require_text(payload, "attempt_id", "联系尝试编号")
        channel = require_text(payload, "channel", "联系渠道")
        moment = parse_moment(payload.get("at"), "at")
        with self.transaction() as state:
            attempts = state["contact_attempts"].setdefault(account_id, [])
            for prior in attempts:
                if prior["attempt_id"] == attempt_id:
                    return copy.deepcopy(prior)
            account = self._account(state, account_id)
            plan = self._current_plan(state, account_id)
            if plan is None or plan["status"] in (STATUS_COMPLETED, STATUS_BREACHED) \
                    or plan["reason"] == REASON_DEFAULT:
                policy = account.get("standard_contact_policy")
                version_id = plan["version_id"] if plan else None
            else:
                policy = plan["contact_policy"]
                version_id = plan["version_id"]
            if policy is None:
                raise UnprocessableError("账户没有可用的联系策略", "missing_contact_policy")
            allowed, reasons = _evaluate_contact(policy, channel, moment)
            attempt = {
                "attempt_id": attempt_id,
                "account_id": account_id,
                "plan_version_id": version_id,
                "channel": channel,
                "requested_at": moment.isoformat(),
                "decision": "allowed" if allowed else "blocked",
                "blocked_reasons": reasons,
            }
            attempts.append(attempt)
            return copy.deepcopy(attempt)

    def list_contact_attempts(self, account_id, decision=None):
        with self._lock:
            self._account(self._state, account_id)
            attempts = self._state["contact_attempts"].get(account_id, [])
            if decision:
                attempts = [a for a in attempts if a["decision"] == decision]
            return copy.deepcopy(attempts)

    # -- 恢复 ---------------------------------------------------------------

    def recover(self):
        """服务恢复后从快照重建：下一待办与联系抑制全部由持久事实派生。"""
        with self._lock:
            state = self._state
            # 重建资金编号索引并校验完整性。
            rebuilt_index = {}
            for fund_id, fund in state["funds"].items():
                rebuilt_index[fund_id] = (fund["account_id"], fund_id)
            state["fund_index"] = rebuilt_index
            # 重建版本索引，保证服务重启后“当前版本”指针仍可解析。
            state["plans_by_id"] = {
                plan["version_id"]: plan for plan in state["plans"]
            }
            summary = {"accounts": [], "open_disputes": 0}
            for account_id, account in state["accounts"].items():
                todo = None
                suppression = None
                plan = self._current_plan(state, account_id)
                if plan is not None and account_id == plan["account_id"]:
                    projection = self._project_locked(state, plan, _today())
                    todo = next(
                        (p for p in projection["periods"] if p["status"] != "satisfied"),
                        None,
                    )
                    restriction = self.effective_contact_policy_locked(account_id, state)
                    suppression = {
                        "source": restriction["source"],
                        "allowed_channels": restriction["allowed_channels"],
                        "allowed_hours": restriction["allowed_hours"],
                        "suppressed_until": restriction["suppressed_until"],
                    } if restriction is not None else None
                summary["accounts"].append({
                    "account_id": account_id,
                    "frozen": account["frozen"],
                    "current_version_id": account.get("current_version_id"),
                    "next_todo": todo,
                    "contact_restriction": suppression,
                })
            for disputes in state["disputes"].values():
                summary["open_disputes"] += sum(
                    1 for d in disputes if d["status"] == "open"
                )
            return copy.deepcopy(summary)

    def effective_contact_policy_locked(self, account_id, state):
        account = state["accounts"][account_id]
        plan = self._current_plan(state, account_id)
        if plan is None or plan["status"] in (STATUS_COMPLETED, STATUS_BREACHED) \
                or plan["reason"] == REASON_DEFAULT:
            policy = account.get("standard_contact_policy")
            source = "standard_contract"
            if policy is None:
                return None
        else:
            policy = plan["contact_policy"]
            source = "hardship_plan"
        return {
            "source": source,
            "allowed_channels": list(policy["allowed_channels"]),
            "allowed_hours": copy.deepcopy(policy.get("allowed_hours")),
            "suppressed_until": (policy or {}).get("suppressed_until"),
        }


# ---------------------------------------------------------------------------
# 解析与校验辅助
# ---------------------------------------------------------------------------

def require_text(payload, field, label):
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{label}（{field}）必填")
    return value


def _parse_calendar(raw):
    if not isinstance(raw, list) or not raw:
        raise LedgerError("payment_calendar 至少包含一期")
    periods = []
    seen = set()
    for row in raw:
        period_no = row.get("period_no")
        if not isinstance(period_no, int) or period_no <= 0 or period_no in seen:
            raise LedgerError("period_no 必须是唯一正整数")
        seen.add(period_no)
        periods.append({
            "period_no": period_no,
            "due_date": parse_day(row.get("due_date"), "due_date").isoformat(),
            "amount_due_cents": to_cents(row.get("amount_due"), "amount_due"),
        })
        periods[-1]["amount_due"] = cents_to_money(periods[-1]["amount_due_cents"])
    return periods


def _parse_grace_rule(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LedgerError("grace_rule 必须是对象")
    grace_days = raw.get("grace_days", 0)
    if not isinstance(grace_days, int) or grace_days < 0:
        raise LedgerError("grace_days 必须是非负整数")
    rule = {"grace_days": grace_days}
    if "minimum_amount" in raw:
        rule["minimum_cents"] = to_cents(raw["minimum_amount"], "minimum_amount")
    elif "minimum_ratio" in raw:
        ratio = raw["minimum_ratio"]
        try:
            decimal_ratio = Decimal(str(ratio))
        except Exception as exc:
            raise LedgerError("minimum_ratio 必须是 0~1 之间的数") from exc
        if not 0 <= decimal_ratio <= 1:
            raise LedgerError("minimum_ratio 必须在 0~1 之间")
        rule["minimum_ratio"] = str(decimal_ratio)
    else:
        rule["minimum_cents"] = 0
    return rule


def _minimum_cents(due_cents, grace_rule):
    if not grace_rule:
        return 0
    if "minimum_cents" in grace_rule:
        return grace_rule["minimum_cents"]
    ratio = Decimal(grace_rule.get("minimum_ratio", "0"))
    return int((Decimal(due_cents) * ratio).to_integral_value())


def _parse_waivers(raw):
    if not isinstance(raw, list):
        raise LedgerError("waivers 必须是数组")
    waivers = []
    for row in raw:
        waiver = {
            "code": require_text(row, "code", "减免项目编号"),
            "description": row.get("description", ""),
            "amount_cents": to_cents(row.get("amount"), "减免金额"),
        }
        waiver["amount"] = cents_to_money(waiver["amount_cents"])
        period_no = row.get("period_no")
        if not isinstance(period_no, int) or period_no <= 0:
            raise LedgerError("减免项目必须通过 period_no 指明冲减哪一期")
        waiver["period_no"] = period_no
        waivers.append(waiver)
    return waivers


def _parse_forbearance(raw):
    if not isinstance(raw, dict):
        raise LedgerError("forbearance 必须是对象")
    result = {}
    months = raw.get("principal_deferral_months", 0)
    if not isinstance(months, int) or months < 0:
        raise LedgerError("principal_deferral_months 必须是非负整数")
    result["principal_deferral_months"] = months
    if "deferred_principal_amount" in raw:
        result["deferred_principal_cents"] = to_cents(
            raw["deferred_principal_amount"], "deferred_principal_amount"
        )
    return result


def _parse_resumption(raw):
    if not isinstance(raw, dict) or "condition" not in raw:
        raise LedgerError("resumption.condition 必填：必须写清恢复原计划的条件")
    resume_date = raw.get("resume_date")
    if resume_date is not None:
        parse_day(resume_date, "resume_date")
    return {
        "condition": str(raw["condition"]),
        "resume_date": resume_date,
        "detail": raw.get("detail", ""),
    }


def _validate_contact_policy(policy):
    if not isinstance(policy, dict):
        raise LedgerError("contact_policy 必须是对象")
    channels = policy.get("allowed_channels")
    if not isinstance(channels, list) or not channels or not all(
        isinstance(c, str) and c for c in channels
    ):
        raise LedgerError("contact_policy.allowed_channels 至少包含一个渠道")
    hours = policy.get("allowed_hours")
    if hours is not None:
        start = parse_hhmm(hours.get("start"), "allowed_hours.start")
        end = parse_hhmm(hours.get("end"), "allowed_hours.end")
        if start >= end:
            raise LedgerError("allowed_hours 开始时间必须早于结束时间")
    suppressed_until = policy.get("suppressed_until")
    if suppressed_until is not None:
        parse_day(suppressed_until, "suppressed_until")
    blackout = policy.get("blackout_dates", [])
    if not isinstance(blackout, list):
        raise LedgerError("blackout_dates 必须是数组")
    for day in blackout:
        parse_day(day, "blackout_dates")


def _evaluate_contact(policy, channel, moment):
    reasons = []
    if channel not in policy["allowed_channels"]:
        reasons.append("channel_not_allowed")
    hours = policy.get("allowed_hours")
    if hours is not None:
        start = parse_hhmm(hours["start"], "allowed_hours.start")
        end = parse_hhmm(hours["end"], "allowed_hours.end")
        now = moment.time().replace(second=0, microsecond=0)
        if not (start <= now < end):
            reasons.append("outside_allowed_hours")
    suppressed_until = policy.get("suppressed_until")
    if suppressed_until and moment.date() <= parse_day(suppressed_until):
        reasons.append("suppressed")
    if moment.date().isoformat() in policy.get("blackout_dates", []):
        reasons.append("blackout_date")
    return (not reasons), reasons


def _build_disclosure(reason, effective_date, forbearance, waivers, calendar,
                      grace_rule, contact_policy, resumption):
    """固化方案批准时向客户披露的内容，旧版本披露长期可查。"""
    return {
        "type": "hardship_terms",
        "reason": reason,
        "effective_date": effective_date.isoformat(),
        "forbearance": copy.deepcopy(forbearance),
        "waivers": copy.deepcopy(waivers),
        "payment_calendar": copy.deepcopy(calendar),
        "grace_rule": copy.deepcopy(grace_rule),
        "contact_policy": copy.deepcopy(contact_policy),
        "resumption": copy.deepcopy(resumption),
        "disclosed_at": _now_iso(),
    }


def parse_money(text):
    return int(Decimal(text) * 100)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")
