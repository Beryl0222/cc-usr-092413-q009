"""协商承诺执行账本。

客户接受困难协商方案后，自动扣款、人工还款、商户退款仍会从不同渠道进入。
本账本把"方案承诺"冻结为不可变版本，并让所有资金在同一余额边界内、
按业务来源与价值日（value date）确定性入账：

* 方案批准即冻结：适用账户、减免项目、付款日历、联系限制、恢复原计划条件；
* 退款只能冲回关联消费，永远不计为客户履约；
* 相同交易重传不重复入账；标识相同但金额/来源变化进入争议，原入账不动；
* 部分到账是否宽限只取决于方案规则与价值日，不取决于调用先后；
* 失业状态更新、再次协商、方案违约都产生新版本，旧版本及当时披露永久可查；
* 每次入账、联系判定都是纯函数式派生，服务恢复后重放事件即可重建
  "下一期待办"与"联系抑制"状态。

事件只追加（append-only），金额一律以"分"整数保存，杜绝浮点误差。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 常量与值对象
# ---------------------------------------------------------------------------

SOURCE_AUTO_DEBIT = "AUTO_DEBIT"          # 自动扣款
SOURCE_MANUAL = "MANUAL"                  # 人工还款
SOURCE_REFUND = "MERCHANT_REFUND"         # 商户退款
PERFORMANCE_SOURCES = (SOURCE_AUTO_DEBIT, SOURCE_MANUAL)
REFUND_SOURCES = (SOURCE_REFUND,)

# 同一价值日多笔资金的确定性次序：与调用顺序无关，只看业务来源与交易标识
_SOURCE_RANK = {SOURCE_AUTO_DEBIT: 0, SOURCE_MANUAL: 1}

KNOWN_CHANNELS = ("PHONE", "SMS", "EMAIL", "POST_MAIL")

REASON_JOB_LOSS = "JOB_LOSS"                     # 失业
REASON_INCOME_UPDATE = "INCOME_UPDATE"           # 失业/收入状态更新
REASON_RENEGOTIATION = "RENEGOTIATION"           # 再次协商
REASON_OTHER = "OTHER"
HARDSHIP_REASONS = (
    REASON_JOB_LOSS,
    REASON_INCOME_UPDATE,
    REASON_RENEGOTIATION,
    REASON_OTHER,
)

KIND_HARDSHIP = "HARDSHIP"
KIND_RESTORED = "RESTORED_ORIGINAL"

CONTACT_MODE_RESTRICTED = "RESTRICTED"
CONTACT_MODE_OPEN = "OPEN"
_OPEN_CONTACT_POLICY = {"mode": CONTACT_MODE_OPEN}

_CENTS = Decimal("100")


class LedgerError(ValueError):
    """领域规则被违反，错误码通过 code 暴露给接口层。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def to_cents(value: Any) -> int:
    """把元（数字或字符串）换成整数分。"""
    if isinstance(value, bool):
        raise LedgerError("invalid_amount", "金额不能是布尔值")
    try:
        return int((Decimal(str(value)) * _CENTS).to_integral_value())
    except Exception as exc:  # noqa: BLE001 - 统一转成领域错误
        raise LedgerError("invalid_amount", f"无法解析金额: {value!r}") from exc


def yuan(cents: int) -> str:
    """整数分转回两位小数字符串，仅用于对外展示。"""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise LedgerError("invalid_date", f"无法解析日期(应为 YYYY-MM-DD): {value!r}") from exc


def parse_ts(value: str) -> datetime:
    """解析 ISO8601 时间；不带时区的时间按 UTC 处理。"""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            text = value.replace("Z", "+00:00") if isinstance(value, str) else value
            dt = datetime.fromisoformat(text)
        except (TypeError, ValueError, AttributeError) as exc:
            raise LedgerError("invalid_timestamp", f"无法解析时间戳: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 派生状态（由事件重放得到，绝不直接对外修改）
# ---------------------------------------------------------------------------


@dataclass
class _State:
    opened: bool = False
    opened_date: Optional[date] = None
    versions: list[dict] = field(default_factory=list)      # 全部承诺版本
    purchases: dict[str, dict] = field(default_factory=dict)
    performance: list[dict] = field(default_factory=list)   # 客户履约资金
    refunds: list[dict] = field(default_factory=list)       # 退款（冲消费）
    idempotency: dict[str, dict] = field(default_factory=dict)
    disputes: list[dict] = field(default_factory=list)
    contacts: list[dict] = field(default_factory=list)
    defaults: list[dict] = field(default_factory=list)
    seq: int = 0


class Ledger:
    """单账户协商承诺执行账本（线程安全、事件可持久化）。"""

    def __init__(self, account_id: str, store: Optional["EventStore"] = None):
        self.account_id = account_id
        self._store = store
        self._lock = threading.RLock()
        self.state = _State()
        if store is not None:
            for event in store.load(account_id):
                self._apply(event, replay=True)

    # ---- 事件基底 ---------------------------------------------------------

    def _append(self, event_type: str, payload: dict) -> dict:
        event = {"seq": self.state.seq + 1, "type": event_type, **payload}
        self._apply(event, replay=False)
        if self._store is not None:
            self._store.append(self.account_id, event)
        return event

    def _apply(self, event: dict, replay: bool) -> None:
        st = self.state
        st.seq = event["seq"]
        etype = event["type"]
        if etype == "account_opened":
            st.opened = True
            st.opened_date = parse_day(event["opened_date"])
        elif etype == "plan_version_created":
            st.versions.append(_freeze_version(event))
            # 新版本接替：把此前仍处于 active 的版本标记为 superseded（内容保留可查）
            for previous in reversed(st.versions[:-1]):
                if previous.get("status") == "active":
                    previous["status"] = "superseded"
                    previous["superseded_by"] = event["version"]
                    break
        elif etype == "arrangement_defaulted":
            st.defaults.append(dict(event))
            # 精确标记违约版本；未来才生效的新版本不受影响
            for version in st.versions:
                if version["version"] == event["version"] and version.get("status") != "defaulted":
                    version["status"] = "defaulted"
                    version["default_event_seq"] = event["seq"]
                    break
        elif etype == "purchase_recorded":
            st.purchases[event["purchase_id"]] = {
                "purchase_id": event["purchase_id"],
                "date": parse_day(event["date"]),
                "amount_cents": event["amount_cents"],
                "refunded_cents": 0,
            }
        elif etype == "funds_received":
            st.performance.append(dict(event))
            st.idempotency[event["idempotency_key"]] = {
                "hash": event["payload_hash"],
                "event_type": event["type"],
                "posting_id": event["posting_id"],
                "event_seq": event["seq"],
            }
        elif etype == "refund_received":
            st.refunds.append(dict(event))
            purchase = st.purchases[event["purchase_id"]]
            purchase["refunded_cents"] += event["amount_cents"]
            st.idempotency[event["idempotency_key"]] = {
                "hash": event["payload_hash"],
                "event_type": event["type"],
                "posting_id": event["posting_id"],
                "event_seq": event["seq"],
            }
        elif etype == "dispute_opened":
            st.disputes.append(dict(event))
        elif etype == "contact_attempted":
            st.contacts.append(dict(event))
        else:  # pragma: no cover - 防御未知事件
            raise LedgerError("unknown_event", f"未知事件类型: {etype}")

    # ---- 命令 -------------------------------------------------------------

    def open_account(self, opened_date: str) -> dict:
        with self._lock:
            if self.state.opened:
                raise LedgerError("account_exists", "账户已开户")
            return self._append(
                "account_opened", {"account_id": self.account_id, "opened_date": opened_date}
            )

    def record_purchase(self, purchase_id: str, when: str, amount: Any) -> dict:
        """登记一笔关联消费。方案生效后账户被冻结，不得新增消费。"""
        with self._lock:
            self._require_open()
            if purchase_id in self.state.purchases:
                raise LedgerError("purchase_exists", f"消费已存在: {purchase_id}")
            cents = to_cents(amount)
            if cents <= 0:
                raise LedgerError("invalid_amount", "消费金额必须为正")
            in_force = self._hardship_in_force(parse_day(when))
            if in_force is not None:
                raise LedgerError(
                    "account_frozen",
                    f"账户处于协商方案 v{in_force['version']} 冻结期，不能新增消费",
                )
            return self._append(
                "purchase_recorded",
                {
                    "account_id": self.account_id,
                    "purchase_id": purchase_id,
                    "date": when,
                    "amount_cents": cents,
                },
            )

    def approve_plan(
        self,
        *,
        effective_date: str,
        reason: str,
        principal_deferral_months: int,
        schedule: list[dict],
        forgiveness: list[dict],
        contact_policy: dict,
        grace_days: int,
        min_partial_ratio: Any,
        restore_conditions: list[Any],
        disclosure: dict,
    ) -> dict:
        """批准（或再次协商）困难方案：把全部承诺条款冻结为新版本。"""
        with self._lock:
            self._require_open()
            if reason not in HARDSHIP_REASONS:
                raise LedgerError("invalid_reason", f"未知方案原因: {reason}")
            eff = parse_day(effective_date)

            frozen_schedule = self._freeze_schedule(schedule)
            if not frozen_schedule:
                raise LedgerError("empty_schedule", "付款日历不能为空")
            frozen_forgiveness = self._freeze_forgiveness(forgiveness)
            policy = self._freeze_contact_policy(contact_policy)
            if int(principal_deferral_months) < 0:
                raise LedgerError("invalid_terms", "暂缓本金月数不能为负")
            if int(grace_days) < 0:
                raise LedgerError("invalid_terms", "宽限天数不能为负")
            ratio = Decimal(str(min_partial_ratio))
            if not 0 < ratio <= 1:
                raise LedgerError("invalid_terms", "部分到账宽限比例须在 (0,1] 之间")
            if not restore_conditions:
                raise LedgerError("invalid_terms", "必须冻结恢复原计划的条件")
            self._validate_disclosure(disclosure)

            if self.state.versions:
                latest = self.state.versions[-1]
                # 新版本生效日不得早于任何既有版本（含尚未到生效日的未来版本）
                if eff < latest["effective_date"]:
                    raise LedgerError(
                        "invalid_effective_date",
                        f"新版本生效日不得早于既有版本 v{latest['version']} "
                        f"({latest['effective_date'].isoformat()})",
                    )
            predecessor = self.state.versions[-1]["version"] if self.state.versions else None
            version_no = len(self.state.versions) + 1
            return self._append(
                "plan_version_created",
                {
                    "account_id": self.account_id,
                    "version": version_no,
                    "predecessor_version": predecessor,
                    "kind": KIND_HARDSHIP,
                    "status": "active",
                    "reason": reason,
                    "effective_date": effective_date,
                    "frozen_accounts": [self.account_id],
                    "principal_deferral_months": int(principal_deferral_months),
                    "schedule": frozen_schedule,
                    "forgiveness": frozen_forgiveness,
                    "contact_policy": policy,
                    "grace_days": int(grace_days),
                    "min_partial_ratio": str(ratio),
                    "restore_conditions": [
                        c if isinstance(c, str) else dict(c) for c in restore_conditions
                    ],
                    "disclosure": {
                        "text": str(disclosure["text"]),
                        "at": parse_ts(disclosure["at"]).isoformat(),
                    },
                    "created_at": utc_now_iso(),
                },
            )

    def default_plan(self, when: str, reason: str) -> list[dict]:
        """记录方案违约：旧承诺标记违约并保留，同时生成"恢复原合同"新版本，
        联系抑制随之解除。"""
        with self._lock:
            day = parse_day(when)
            active = self._hardship_in_force(day)
            if active is None:
                raise LedgerError("no_active_plan", "当日没有生效中的困难方案，无法记录违约")
            events = [
                self._append(
                    "arrangement_defaulted",
                    {
                        "account_id": self.account_id,
                        "version": active["version"],
                        "date": when,
                        "reason": str(reason),
                    },
                )
            ]
            events.append(
                self._append(
                    "plan_version_created",
                    {
                        "account_id": self.account_id,
                        "version": len(self.state.versions) + 1,
                        "predecessor_version": active["version"],
                        "kind": KIND_RESTORED,
                        "status": "active",
                        "reason": "ARRANGEMENT_DEFAULT",
                        "effective_date": when,
                        "frozen_accounts": [],
                        "principal_deferral_months": 0,
                        "schedule": [],
                        "forgiveness": [],
                        "contact_policy": dict(_OPEN_CONTACT_POLICY),
                        "grace_days": 0,
                        "min_partial_ratio": "1",
                        "restore_conditions": [],
                        "disclosure": {
                            "text": f"困难方案 v{active['version']} 违约，自 {when} 起恢复原合同条款",
                            "at": utc_now_iso(),
                        },
                        "created_at": utc_now_iso(),
                    },
                )
            )
            return events

    def receive_funds(
        self,
        *,
        posting_id: str,
        idempotency_key: str,
        source: str,
        amount: Any,
        value_date: str,
        received_at: Optional[str] = None,
        purchase_id: Optional[str] = None,
    ) -> dict:
        """资金入账。自动扣款/人工还款计入履约；商户退款只冲关联消费。

        校验与事件追加在同一把锁内完成：失败不会留下半次履约。
        """
        with self._lock:
            key = str(idempotency_key)
            cents = to_cents(amount)
            if cents <= 0:
                raise LedgerError("invalid_amount", "入账金额必须为正")
            day = parse_day(value_date)
            arrived = parse_ts(received_at) if received_at else datetime.now(timezone.utc)

            payload_hash = _payload_hash(source, cents, value_date, purchase_id)
            duplicate = self._check_idempotency(key, payload_hash, posting_id)
            if duplicate is not None:
                return duplicate  # 重传：原样返回，不重复入账

            if source in PERFORMANCE_SOURCES:
                if purchase_id is not None:
                    raise LedgerError(
                        "invalid_source",
                        "关联消费只允许通过商户退款冲回，还款资金不得指定消费",
                    )
                version = self._hardship_in_force(day)
                if version is None:
                    raise LedgerError(
                        "hardship_inactive", "价值日没有生效中的协商方案，资金应走原合同渠道"
                    )
                event = self._append(
                    "funds_received",
                    {
                        "account_id": self.account_id,
                        "posting_id": posting_id,
                        "idempotency_key": key,
                        "payload_hash": payload_hash,
                        "source": source,
                        "amount_cents": cents,
                        "value_date": value_date,
                        "received_at": arrived.isoformat(),
                        "plan_version": version["version"],
                    },
                )
            elif source in REFUND_SOURCES:
                if not purchase_id:
                    raise LedgerError("purchase_required", "商户退款必须关联原消费")
                purchase = self.state.purchases.get(purchase_id)
                if purchase is None:
                    raise LedgerError("unknown_purchase", f"关联消费不存在: {purchase_id}")
                remaining = purchase["amount_cents"] - purchase["refunded_cents"]
                if cents > remaining:
                    raise LedgerError(
                        "refund_exceeds_purchase",
                        f"退款 {yuan(cents)} 超过该消费可冲回余额 {yuan(remaining)}",
                    )
                event = self._append(
                    "refund_received",
                    {
                        "account_id": self.account_id,
                        "posting_id": posting_id,
                        "idempotency_key": key,
                        "payload_hash": payload_hash,
                        "source": SOURCE_REFUND,
                        "purchase_id": purchase_id,
                        "amount_cents": cents,
                        "value_date": value_date,
                        "received_at": arrived.isoformat(),
                    },
                )
            else:
                raise LedgerError("invalid_source", f"未知资金来源: {source}")

            return event

    def record_contact_attempt(self, channel: str, ts: str, attempt_id: Optional[str] = None) -> dict:
        """催收联系人尝试联系：按当前承诺的联系限制判定并留痕（含被阻止的）。"""
        with self._lock:
            when = parse_ts(ts)
            decision = self.evaluate_contact(channel, when, lock_held=True)
            event = self._append(
                "contact_attempted",
                {
                    "account_id": self.account_id,
                    "attempt_id": attempt_id or f"ct-{self.state.seq + 1}",
                    "channel": channel,
                    "ts": when.isoformat(),
                    "blocked": bool(decision["blocked"]),
                    "reasons": decision["reasons"],
                    "plan_version": decision.get("plan_version"),
                },
            )
            return event

    # ---- 查询 / 派生 ------------------------------------------------------

    def evaluate_contact(self, channel: str, when: datetime, lock_held: bool = False) -> dict:
        """返回某时刻某渠道是否允许联系。纯派生：恢复后重放即可得到同样结果。"""

        def _eval() -> dict:
            day = when.date()
            version = self._version_in_force(day)
            if version is None or version["kind"] != KIND_HARDSHIP:
                return {"blocked": False, "reasons": [], "policy": _OPEN_CONTACT_POLICY}
            policy = version["contact_policy"]
            reasons: list[str] = []
            if channel not in policy["allowed_channels"]:
                reasons.append("CHANNEL_NOT_ALLOWED")
            offset = timedelta(minutes=policy["tz_offset_minutes"])
            local = when.astimezone(timezone.utc) + offset
            if local.date().isoformat() in policy["blackout_dates"]:
                reasons.append("BLACKOUT_DATE")
            start = _hm(policy["allowed_hours"]["start"])
            end = _hm(policy["allowed_hours"]["end"])
            now_minutes = local.hour * 60 + local.minute
            if not (start <= now_minutes < end):
                reasons.append("OUTSIDE_ALLOWED_HOURS")
            max_per_day = policy.get("max_per_day")
            if max_per_day is not None:
                used = sum(
                    1
                    for c in self.state.contacts
                    if not c["blocked"]
                    and c.get("plan_version") == version["version"]
                    and c["channel"] == channel
                    and (parse_ts(c["ts"]) + offset).date() == local.date()
                )
                if used >= max_per_day:
                    reasons.append("DAILY_CAP_REACHED")
            return {
                "blocked": bool(reasons),
                "reasons": reasons,
                "plan_version": version["version"],
                "policy": policy,
                "local_time": local.strftime("%Y-%m-%d %H:%M"),
            }

        if lock_held:
            return _eval()
        with self._lock:
            return _eval()

    def contact_view(self, ts: str) -> dict:
        """催收联系人端视图：只能看到当前允许的渠道与时段。"""
        with self._lock:
            when = parse_ts(ts)
            version = self._version_in_force(when.date())
            channels = {}
            for channel in KNOWN_CHANNELS:
                decision = self.evaluate_contact(channel, when, lock_held=True)
                channels[channel] = {"allowed": not decision["blocked"], "reasons": decision["reasons"]}
            if version is None:
                return {
                    "account_id": self.account_id,
                    "arrangement": "NONE",
                    "allowed_channels": list(KNOWN_CHANNELS),
                    "channels": channels,
                }
            if version["kind"] == KIND_RESTORED:
                return {
                    "account_id": self.account_id,
                    "arrangement": "RESTORED_ORIGINAL",
                    "plan_version": version["version"],
                    "allowed_channels": list(KNOWN_CHANNELS),
                    "channels": channels,
                    "notice": "困难方案已结束，按原合同联系，无时段限制",
                }
            policy = version["contact_policy"]
            return {
                "account_id": self.account_id,
                "arrangement": KIND_HARDSHIP,
                "plan_version": version["version"],
                "allowed_channels": list(policy["allowed_channels"]),
                "allowed_hours": policy["allowed_hours"],
                "blackout_dates": list(policy["blackout_dates"]),
                "channels": channels,
            }

    def periods(self, as_of: str) -> list[dict]:
        """逐期履行视图：每期应付、已付、欠多少、是否宽限——只由价值日决定。"""
        with self._lock:
            day = parse_day(as_of)
            version = self._hardship_in_force(day)
            if version is None:
                return []
            allocations = self._allocate(day)
            paid_per_period: dict[int, int] = {}
            for posting in allocations["postings"]:
                for applied in posting["allocation"]:
                    seq = applied["seq"]
                    paid_per_period[seq] = paid_per_period.get(seq, 0) + applied["amount_cents"]
            result = []
            ratio_min = Decimal(version["min_partial_ratio"])
            for item in version["schedule"]:
                due = parse_day(item["due_date"])
                paid = paid_per_period.get(item["seq"], 0)
                ratio = Decimal(paid) / Decimal(item["amount_cents"]) if item["amount_cents"] else Decimal(1)
                grace_end = due + timedelta(days=version["grace_days"])
                if paid >= item["amount_cents"]:
                    status = "PAID"
                elif due > day:
                    status = "FUTURE"
                elif due == day:
                    status = "DUE_TODAY"
                elif day < grace_end and ratio >= ratio_min:
                    status = "PARTIAL_WITHIN_GRACE"
                elif day < grace_end:
                    status = "PARTIAL_BELOW_THRESHOLD"
                else:
                    status = "DELINQUENT"
                result.append(
                    {
                        "version": version["version"],
                        "seq": item["seq"],
                        "due_date": item["due_date"],
                        "due_cents": item["amount_cents"],
                        "due_yuan": yuan(item["amount_cents"]),
                        "paid_cents": paid,
                        "paid_yuan": yuan(paid),
                        "shortfall_cents": max(0, item["amount_cents"] - paid),
                        "shortfall_yuan": yuan(max(0, item["amount_cents"] - paid)),
                        "paid_ratio": str(ratio.quantize(Decimal("0.0001"))),
                        "status": status,
                        "grace_ends_on": grace_end.isoformat(),
                    }
                )
            return result

    def next_todo(self, as_of: str) -> dict:
        """重建下一待办：最早逾期未清 → 当日到期 → 最近未来期。"""
        with self._lock:
            views = self.periods(as_of)
            if not views:
                version = self._version_in_force(parse_day(as_of))
                return {
                    "account_id": self.account_id,
                    "as_of": as_of,
                    "todo": "NONE",
                    "arrangement": (
                        "RESTORED_ORIGINAL"
                        if version is not None and version["kind"] == KIND_RESTORED
                        else "NONE"
                    ),
                }
            order = {
                "DELINQUENT": 0,
                "PARTIAL_BELOW_THRESHOLD": 1,
                "PARTIAL_WITHIN_GRACE": 2,
                "DUE_TODAY": 3,
                "FUTURE": 4,
                "PAID": 5,
            }
            pending = [p for p in views if p["status"] != "PAID"]
            pending.sort(key=lambda p: (order[p["status"]], p["due_date"]))
            target = pending[0] if pending else None
            return {
                "account_id": self.account_id,
                "as_of": as_of,
                "todo": "COLLECT_OR_WAIT_PERIOD" if target else "ALL_PAID",
                "period": target,
                "has_delinquency": any(p["status"] == "DELINQUENT" for p in views),
            }

    def explain(self, as_of: str) -> dict:
        """面向客户与合规的完整解释：每期状态、退款、恢复条件、被阻止的联系。"""
        with self._lock:
            day = parse_day(as_of)
            version = self._hardship_in_force(day)
            allocations = self._allocate(day)
            period_views = self.periods(as_of)
            total_due = sum(p["due_cents"] for p in period_views)
            total_paid = sum(p["paid_cents"] for p in period_views)
            refund_total = sum(
                r["amount_cents"]
                for r in self.state.refunds
                if parse_day(r["value_date"]) <= day
            )
            current = self._version_in_force(day)
            return {
                "account_id": self.account_id,
                "as_of": as_of,
                "current_version": current["version"] if current else None,
                "arrangement_state": (
                    "HARDSHIP"
                    if version is not None
                    else ("RESTORED_ORIGINAL" if current is not None else "NONE")
                ),
                "periods": period_views,
                "totals": {
                    "scheduled_cents": total_due,
                    "scheduled_yuan": yuan(total_due),
                    "performed_cents": total_paid,
                    "performed_yuan": yuan(total_paid),
                    "outstanding_cents": max(0, total_due - total_paid),
                    "outstanding_yuan": yuan(max(0, total_due - total_paid)),
                    "refund_offset_cents": refund_total,
                    "refund_offset_yuan": yuan(refund_total),
                    "refund_note": "商户退款仅冲回关联消费，不计为客户履约",
                },
                "posting_allocations": allocations["postings"],
                "unapplied_cents": allocations["unapplied_cents"],
                "unapplied_yuan": yuan(allocations["unapplied_cents"]),
                "restore_conditions": version["restore_conditions"] if version else [],
                "principal_deferral_months": (
                    version["principal_deferral_months"] if version else 0
                ),
                "forgiveness": version["forgiveness"] if version else [],
                "blocked_contacts": [
                    {
                        "attempt_id": c["attempt_id"],
                        "ts": c["ts"],
                        "channel": c["channel"],
                        "reasons": c["reasons"],
                        "plan_version": c.get("plan_version"),
                    }
                    for c in self.state.contacts
                    if c["blocked"] and parse_ts(c["ts"]).date() <= day
                ],
                "disputes": list(self.state.disputes),
            }

    def versions(self) -> list[dict]:
        with self._lock:
            return [_version_view(v) for v in self.state.versions]

    def version(self, number: int) -> dict:
        with self._lock:
            for v in self.state.versions:
                if v["version"] == number:
                    return _version_view(v)
            raise LedgerError("version_not_found", f"承诺版本不存在: v{number}")

    def disputes(self) -> list[dict]:
        with self._lock:
            return list(self.state.disputes)

    def snapshot(self) -> dict:
        with self._lock:
            current = self._version_in_force(date.today())
            return {
                "account_id": self.account_id,
                "opened_date": self.state.opened_date.isoformat() if self.state.opened_date else None,
                "state": (
                    "HARDSHIP_ACTIVE"
                    if current is not None and current["kind"] == KIND_HARDSHIP
                    else ("RESTORED_ORIGINAL" if current is not None else "OPEN")
                ),
                "current_version": current["version"] if current else None,
                "versions": len(self.state.versions),
                "purchases": [
                    {
                        "purchase_id": p["purchase_id"],
                        "amount_yuan": yuan(p["amount_cents"]),
                        "refunded_yuan": yuan(p["refunded_cents"]),
                        "refundable_yuan": yuan(p["amount_cents"] - p["refunded_cents"]),
                    }
                    for p in self.state.purchases.values()
                ],
            }

    # ---- 内件 -------------------------------------------------------------

    def _require_open(self) -> None:
        if not self.state.opened:
            raise LedgerError("account_not_open", "账户尚未开户")

    def _version_in_force(self, day: date) -> Optional[dict]:
        """某日实际生效的承诺版本（含违约后的恢复版本）。"""
        candidate = None
        for version in self.state.versions:
            if version["effective_date"] <= day:
                candidate = version
        return candidate

    def _hardship_in_force(self, day: date) -> Optional[dict]:
        candidate = self._version_in_force(day)
        if candidate is not None and candidate["kind"] == KIND_HARDSHIP:
            return candidate
        return None

    def _allocate(self, as_of: date) -> dict:
        """按价值日把履约资金确定性分配到当期付款日历。

        集合确定、次序确定（价值日 → 来源等级 → 交易标识），
        因此重传乱序或服务重放都不会改变每期"已履行/仍欠"结论。
        商户退款不参与分配。
        """
        version = self._hardship_in_force(as_of)
        if version is None:
            return {"postings": [], "unapplied_cents": 0}
        postings = [
            p
            for p in self.state.performance
            if p["plan_version"] == version["version"]
            and parse_day(p["value_date"]) <= as_of
        ]
        postings.sort(
            key=lambda p: (
                p["value_date"],
                _SOURCE_RANK.get(p["source"], 9),
                p["posting_id"],
            )
        )
        remaining = {item["seq"]: item["amount_cents"] for item in version["schedule"]}
        seq_order = [item["seq"] for item in version["schedule"]]
        enriched = []
        unapplied = 0
        for posting in postings:
            left = posting["amount_cents"]
            allocation = []
            for seq in seq_order:
                if left <= 0:
                    break
                if remaining[seq] > 0:
                    part = min(left, remaining[seq])
                    remaining[seq] -= part
                    left -= part
                    allocation.append((seq, part))
            if left:
                unapplied += left
            enriched.append(
                {
                    "posting_id": posting["posting_id"],
                    "source": posting["source"],
                    "value_date": posting["value_date"],
                    "amount_yuan": yuan(posting["amount_cents"]),
                    "allocation": [
                        {"seq": seq, "amount_cents": amount, "amount_yuan": yuan(amount)}
                        for seq, amount in allocation
                    ],
                    "unapplied_yuan": yuan(left),
                }
            )
        return {"postings": enriched, "unapplied_cents": unapplied}

    def _check_idempotency(self, key: str, payload_hash: str, posting_id: str) -> Optional[dict]:
        existing = self.state.idempotency.get(key)
        if existing is None:
            return None
        if existing["hash"] == payload_hash:
            # 相同交易重传：幂等成功，返回已入账事件，绝不重复入账
            return {
                "duplicate": True,
                "idempotency_key": key,
                "posting_id": existing["posting_id"],
                "event_seq": existing["event_seq"],
            }
        # 标识相同但金额/来源/价值日/关联消费变化：进入争议，维持原入账。
        # 同一差异内容反复重传只保留一条争议。
        already = any(
            d["idempotency_key"] == key and d["retry_hash"] == payload_hash
            for d in self.state.disputes
        )
        if not already:
            self._append(
                "dispute_opened",
                {
                    "account_id": self.account_id,
                    "idempotency_key": key,
                    "original_posting_id": existing["posting_id"],
                    "original_hash": existing["hash"],
                    "retry_posting_id": posting_id,
                    "retry_hash": payload_hash,
                    "opened_at": utc_now_iso(),
                    "resolution": "PENDING",
                },
            )
        raise LedgerError(
            "dispute_opened",
            f"幂等键 {key} 对应交易内容发生变化，已挂起争议，原入账保持有效",
        )

    @staticmethod
    def _freeze_schedule(schedule: list[dict]) -> list[dict]:
        if not isinstance(schedule, list):
            raise LedgerError("invalid_schedule", "付款日历必须是列表")
        frozen = []
        previous_due = None
        for index, item in enumerate(schedule, start=1):
            due = parse_day(item["due_date"])
            cents = to_cents(item["amount"])
            if cents < 0:
                raise LedgerError("invalid_amount", "期供金额不能为负")
            if previous_due and due <= previous_due:
                raise LedgerError("invalid_schedule", "付款日历必须按到期日严格递增")
            previous_due = due
            frozen.append({"seq": index, "due_date": due.isoformat(), "amount_cents": cents})
        return frozen

    @staticmethod
    def _freeze_forgiveness(forgiveness: list[dict]) -> list[dict]:
        if not isinstance(forgiveness, list):
            raise LedgerError("invalid_forgiveness", "减免项目必须是列表")
        frozen = []
        for item in forgiveness:
            cents = to_cents(item["amount"])
            if cents < 0:
                raise LedgerError("invalid_amount", "减免金额不能为负")
            frozen.append({"item": str(item["item"]), "amount_cents": cents})
        return frozen

    @staticmethod
    def _freeze_contact_policy(policy: dict) -> dict:
        if not isinstance(policy, dict):
            raise LedgerError("invalid_contact_policy", "联系限制必须是对象")
        channels = policy.get("allowed_channels")
        if not isinstance(channels, list) or not channels:
            raise LedgerError("invalid_contact_policy", "必须至少保留一个允许的联系渠道")
        bad = [c for c in channels if c not in KNOWN_CHANNELS]
        if bad:
            raise LedgerError("invalid_contact_policy", f"未知联系渠道: {bad}")
        hours = policy.get("allowed_hours")
        if not isinstance(hours, dict) or "start" not in hours or "end" not in hours:
            raise LedgerError("invalid_contact_policy", "必须冻结允许联系时段 start/end")
        start, end = _hm(hours["start"]), _hm(hours["end"])
        if not (0 <= start < 1440 and 0 < end <= 1440 and start < end):
            raise LedgerError("invalid_contact_policy", "联系时段格式应为 HH:MM 且 start<end")
        blackout = policy.get("blackout_dates", [])
        if not isinstance(blackout, list):
            raise LedgerError("invalid_contact_policy", "禁联日期必须是列表")
        for d in blackout:
            parse_day(d)
        max_per_day = policy.get("max_per_day")
        if max_per_day is not None and int(max_per_day) <= 0:
            raise LedgerError("invalid_contact_policy", "每日联系上限必须为正")
        return {
            "mode": CONTACT_MODE_RESTRICTED,
            "allowed_channels": list(channels),
            "allowed_hours": {"start": hours["start"], "end": hours["end"]},
            "tz_offset_minutes": int(policy.get("tz_offset_minutes", 0)),
            "blackout_dates": list(blackout),
            "max_per_day": int(max_per_day) if max_per_day is not None else None,
        }

    @staticmethod
    def _validate_disclosure(disclosure: dict) -> None:
        if not isinstance(disclosure, dict) or not disclosure.get("text") or not disclosure.get("at"):
            raise LedgerError("invalid_disclosure", "必须冻结向客户披露的文本与时间")
        parse_ts(disclosure["at"])


# ---------------------------------------------------------------------------
# 版本快照与展示
# ---------------------------------------------------------------------------


def _freeze_version(event: dict) -> dict:
    version = {k: v for k, v in event.items() if k not in ("account_id", "type", "seq")}
    version["effective_date"] = parse_day(version["effective_date"])
    return version


def _version_view(version: dict) -> dict:
    view = dict(version)
    view["effective_date"] = version["effective_date"].isoformat()
    view["schedule"] = [
        {
            **item,
            "amount_yuan": yuan(item["amount_cents"]),
        }
        for item in version["schedule"]
    ]
    view["forgiveness"] = [
        {**item, "amount_yuan": yuan(item["amount_cents"])} for item in version["forgiveness"]
    ]
    return view


def _hm(value: str) -> int:
    try:
        hour, minute = str(value).split(":")
        result = int(hour) * 60 + int(minute)
        if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            raise ValueError
        return result
    except (ValueError, AttributeError) as exc:
        raise LedgerError("invalid_contact_policy", f"时间格式应为 HH:MM: {value!r}") from exc


def _payload_hash(source: str, cents: int, value_date: str, purchase_id: Optional[str]) -> str:
    material = json.dumps(
        {
            "source": source,
            "amount_cents": cents,
            "value_date": value_date,
            "purchase_id": purchase_id,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    import hashlib

    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 事件存储（JSONL，崩溃/重启后重放重建）
# ---------------------------------------------------------------------------


class EventStore:
    """每个账户一个 JSONL 文件；append 后 fsync，保证重启可重建。"""

    def __init__(self, directory: str):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, account_id: str) -> Path:
        if "/" in account_id or ".." in account_id:
            raise LedgerError("invalid_account_id", "账户ID含有非法路径字符")
        return self.directory / f"{account_id}.jsonl"

    def load(self, account_id: str) -> list[dict]:
        path = self._path(account_id)
        if not path.exists():
            return []
        events = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        events.sort(key=lambda e: e["seq"])
        return events

    def append(self, account_id: str, event: dict) -> None:
        path = self._path(account_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            import os

            os.fsync(handle.fileno())


class LedgerRegistry:
    """按账户懒加载账本；同一账户共享实例与余额边界锁。"""

    def __init__(self, store: Optional[EventStore] = None):
        self._store = store
        self._ledgers: dict[str, Ledger] = {}
        self._registry_lock = threading.Lock()

    def get(self, account_id: str) -> Ledger:
        with self._registry_lock:
            ledger = self._ledgers.get(account_id)
            if ledger is None:
                ledger = Ledger(account_id, store=self._store)
                self._ledgers[account_id] = ledger
            return ledger

    def open(self, account_id: str, opened_date: str) -> Ledger:
        ledger = self.get(account_id)
        ledger.open_account(opened_date)
        return ledger
