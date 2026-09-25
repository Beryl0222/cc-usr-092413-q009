"""协商承诺执行账本的领域规则测试。"""

import tempfile
import threading
import unittest
from datetime import date
from decimal import Decimal

from ledger import (
    EventStore,
    Ledger,
    LedgerError,
    SOURCE_AUTO_DEBIT,
    SOURCE_MANUAL,
    SOURCE_REFUND,
    to_cents,
    yuan,
)

EFF = "2026-04-01"


def standard_plan(**overrides):
    body = {
        "effective_date": EFF,
        "reason": "JOB_LOSS",
        "principal_deferral_months": 3,
        "schedule": [
            {"due_date": "2026-05-01", "amount": "1000.00"},
            {"due_date": "2026-06-01", "amount": "1000.00"},
            {"due_date": "2026-07-01", "amount": "1000.00"},
        ],
        "forgiveness": [{"item": "LATE_FEE", "amount": "200.00"}],
        "contact_policy": {
            "allowed_channels": ["PHONE"],
            "allowed_hours": {"start": "09:00", "end": "18:00"},
            "tz_offset_minutes": 480,  # UTC+8
            "blackout_dates": ["2026-05-01"],
        },
        "grace_days": 5,
        "min_partial_ratio": "0.5",
        "restore_conditions": ["连续两期足额按时履行", "重新就业并提交收入证明"],
        "disclosure": {"text": "暂缓本金3个月，月供降至1000元", "at": "2026-03-28T10:00:00+08:00"},
    }
    body.update(overrides)
    return body


def ready_ledger(plan_overrides=None):
    ledger = Ledger("acct-1")
    ledger.open_account("2026-01-01")
    ledger.record_purchase("p-1", "2026-02-01", "3000.00")
    ledger.approve_plan(**standard_plan(**(plan_overrides or {})))
    return ledger


def pay(ledger, *, key, source, amount, value_date, posting_id=None):
    return ledger.receive_funds(
        posting_id=posting_id or f"post-{key}",
        idempotency_key=key,
        source=source,
        amount=amount,
        value_date=value_date,
    )


class FreezeAndLifecycleTest(unittest.TestCase):
    def test_plan_approval_freezes_terms_and_disclosure(self):
        ledger = ready_ledger()
        v1 = ledger.version(1)
        self.assertEqual(v1["status"], "active")
        self.assertEqual(v1["frozen_accounts"], ["acct-1"])
        self.assertEqual(v1["principal_deferral_months"], 3)
        self.assertEqual([s["amount_yuan"] for s in v1["schedule"]], ["1000.00"] * 3)
        self.assertEqual(v1["forgiveness"][0]["item"], "LATE_FEE")
        self.assertEqual(v1["contact_policy"]["allowed_channels"], ["PHONE"])
        self.assertIn("连续两期足额按时履行", v1["restore_conditions"])
        self.assertIn("暂缓本金3个月", v1["disclosure"]["text"])

    def test_account_is_frozen_during_arrangement(self):
        ledger = ready_ledger()
        with self.assertRaises(LedgerError) as ctx:
            ledger.record_purchase("p-2", "2026-04-15", "100.00")
        self.assertEqual(ctx.exception.code, "account_frozen")

    def test_payment_without_active_plan_is_rejected(self):
        ledger = Ledger("acct-2")
        ledger.open_account("2026-01-01")
        with self.assertRaises(LedgerError) as ctx:
            pay(ledger, key="k1", source=SOURCE_AUTO_DEBIT, amount="100", value_date="2026-03-01")
        self.assertEqual(ctx.exception.code, "hardship_inactive")


class AllocationTest(unittest.TestCase):
    def test_funds_allocate_by_value_date_then_source_regardless_of_call_order(self):
        # 两个账本以相反调用顺序收到同一批资金
        ledgers = [ready_ledger(), ready_ledger()]
        posts_a = [
            ("k-auto", SOURCE_AUTO_DEBIT, "600", "2026-05-01"),
            ("k-manual", SOURCE_MANUAL, "500", "2026-05-01"),
        ]
        for key, source, amount, vd in posts_a:
            pay(ledgers[0], key=key, source=source, amount=amount, value_date=vd)
        for key, source, amount, vd in reversed(posts_a):
            pay(ledgers[1], key=key, source=source, amount=amount, value_date=vd)

        p1 = ledgers[0].periods("2026-05-02")
        p2 = ledgers[1].periods("2026-05-02")
        self.assertEqual(p1, p2)
        # 同一价值日：自动扣款先冲账，人工随后
        self.assertEqual(p1[0]["paid_yuan"], "1000.00")
        self.assertEqual(p1[1]["paid_yuan"], "100.00")
        self.assertEqual(p1[1]["shortfall_yuan"], "900.00")

    def test_value_date_not_arrival_order_controls_allocation(self):
        early = ready_ledger()
        late = ready_ledger()
        # 后到达的人工还款价值日更早（柜台隔日入账）
        pay(early, key="late-auto", source=SOURCE_AUTO_DEBIT, amount="800", value_date="2026-05-02")
        pay(early, key="early-manual", source=SOURCE_MANUAL, amount="800", value_date="2026-05-01")
        pay(late, key="early-manual", source=SOURCE_MANUAL, amount="800", value_date="2026-05-01")
        pay(late, key="late-auto", source=SOURCE_AUTO_DEBIT, amount="800", value_date="2026-05-02")
        self.assertEqual(early.periods("2026-05-03"), late.periods("2026-05-03"))

    def test_surplus_is_tracked_as_unapplied(self):
        ledger = ready_ledger()
        pay(ledger, key="over", source=SOURCE_MANUAL, amount="3500", value_date="2026-05-01")
        explanation = ledger.explain("2026-05-02")
        self.assertEqual(explanation["unapplied_yuan"], "500.00")
        self.assertEqual(explanation["totals"]["performed_yuan"], "3000.00")


class RefundTest(unittest.TestCase):
    def test_refund_offsets_purchase_and_never_counts_as_performance(self):
        ledger = ready_ledger()
        pay(ledger, key="perf", source=SOURCE_AUTO_DEBIT, amount="1000", value_date="2026-05-01")
        ledger.receive_funds(
            posting_id="rf-1",
            idempotency_key="rf-key-1",
            source=SOURCE_REFUND,
            amount="500",
            value_date="2026-05-03",
            purchase_id="p-1",
        )
        explanation = ledger.explain("2026-05-04")
        periods = explanation["periods"]
        self.assertEqual(periods[0]["paid_yuan"], "1000.00")
        self.assertEqual(periods[1]["paid_yuan"], "0.00")
        self.assertEqual(explanation["totals"]["performed_yuan"], "1000.00")
        self.assertEqual(explanation["totals"]["refund_offset_yuan"], "500.00")
        snap = ledger.snapshot()
        self.assertEqual(snap["purchases"][0]["refunded_yuan"], "500.00")
        self.assertEqual(snap["purchases"][0]["refundable_yuan"], "2500.00")

    def test_refund_requires_purchase_and_cannot_exceed_it(self):
        ledger = ready_ledger()
        with self.assertRaises(LedgerError) as ctx:
            ledger.receive_funds(
                posting_id="rf-x",
                idempotency_key="rf-x",
                source=SOURCE_REFUND,
                amount="10",
                value_date="2026-05-01",
            )
        self.assertEqual(ctx.exception.code, "purchase_required")
        with self.assertRaises(LedgerError) as ctx:
            ledger.receive_funds(
                posting_id="rf-y",
                idempotency_key="rf-y",
                source=SOURCE_REFUND,
                amount="999999",
                value_date="2026-05-01",
                purchase_id="p-1",
            )
        self.assertEqual(ctx.exception.code, "refund_exceeds_purchase")

    def test_performance_funds_cannot_target_purchase(self):
        ledger = ready_ledger()
        with self.assertRaises(LedgerError) as ctx:
            ledger.receive_funds(
                posting_id="x",
                idempotency_key="x",
                source=SOURCE_MANUAL,
                amount="10",
                value_date="2026-05-01",
                purchase_id="p-1",
            )
        self.assertEqual(ctx.exception.code, "invalid_source")


class IdempotencyAndDisputeTest(unittest.TestCase):
    def test_identical_retry_is_not_posted_twice(self):
        ledger = ready_ledger()
        first = pay(ledger, key="same", source=SOURCE_AUTO_DEBIT, amount="1000", value_date="2026-05-01")
        retry = pay(ledger, key="same", source=SOURCE_AUTO_DEBIT, amount="1000", value_date="2026-05-01")
        self.assertNotIn("duplicate", first)
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["event_seq"], first["seq"])
        periods = ledger.periods("2026-05-02")
        self.assertEqual(periods[0]["paid_yuan"], "1000.00")  # 不是 2000

    def test_same_key_changed_amount_opens_dispute_and_keeps_original(self):
        ledger = ready_ledger()
        pay(ledger, key="k", posting_id="orig", source=SOURCE_AUTO_DEBIT, amount="500",
            value_date="2026-05-01")
        with self.assertRaises(LedgerError) as ctx:
            pay(ledger, key="k", posting_id="retry", source=SOURCE_AUTO_DEBIT, amount="501",
                value_date="2026-05-01")
        self.assertEqual(ctx.exception.code, "dispute_opened")
        self.assertEqual(len(ledger.disputes()), 1)
        # 原入账不动，仍按 500 履约
        self.assertEqual(ledger.periods("2026-05-02")[0]["paid_yuan"], "500.00")
        # 同样的差异内容反复重传，不重复挂争议
        with self.assertRaises(LedgerError):
            pay(ledger, key="k", posting_id="retry2", source=SOURCE_AUTO_DEBIT, amount="501",
                value_date="2026-05-01")
        self.assertEqual(len(ledger.disputes()), 1)

    def test_same_key_changed_source_opens_dispute(self):
        ledger = ready_ledger()
        pay(ledger, key="k", source=SOURCE_AUTO_DEBIT, amount="500", value_date="2026-05-01")
        with self.assertRaises(LedgerError) as ctx:
            pay(ledger, key="k", source=SOURCE_MANUAL, amount="500", value_date="2026-05-01")
        self.assertEqual(ctx.exception.code, "dispute_opened")


class GraceAndPartialTest(unittest.TestCase):
    def _ledger_with_partial(self):
        ledger = ready_ledger()
        pay(ledger, key="part", source=SOURCE_MANUAL, amount="400", value_date="2026-05-01")
        return ledger

    def test_partial_below_threshold_becomes_delinquent_after_grace(self):
        ledger = self._ledger_with_partial()
        # 40% < 50% 门槛：即便在宽限日内也不算受宽限
        self.assertEqual(ledger.periods("2026-05-02")[0]["status"], "PARTIAL_BELOW_THRESHOLD")
        # 宽限 5 天：到期日 5/1，5/6 起转 DELINQUENT，与调用顺序无关
        self.assertEqual(ledger.periods("2026-05-05")[0]["status"], "PARTIAL_BELOW_THRESHOLD")
        self.assertEqual(ledger.periods("2026-05-06")[0]["status"], "DELINQUENT")
        self.assertTrue(ledger.next_todo("2026-05-06")["has_delinquency"])

    def test_partial_above_threshold_is_graced_then_delinquent(self):
        ledger = ready_ledger()
        pay(ledger, key="part", source=SOURCE_MANUAL, amount="600", value_date="2026-05-01")
        self.assertEqual(ledger.periods("2026-05-05")[0]["status"], "PARTIAL_WITHIN_GRACE")
        self.assertEqual(ledger.periods("2026-05-06")[0]["status"], "DELINQUENT")

    def test_grace_result_does_not_depend_on_call_order(self):
        a = ready_ledger()
        b = ready_ledger()
        pay(a, key="1", source=SOURCE_MANUAL, amount="600", value_date="2026-05-01")
        pay(a, key="2", source=SOURCE_AUTO_DEBIT, amount="1000", value_date="2026-06-01")
        pay(b, key="2", source=SOURCE_AUTO_DEBIT, amount="1000", value_date="2026-06-01")
        pay(b, key="1", source=SOURCE_MANUAL, amount="600", value_date="2026-05-01")
        for day in ("2026-05-02", "2026-05-06", "2026-06-02"):
            self.assertEqual(a.periods(day), b.periods(day), day)


class VersioningTest(unittest.TestCase):
    def test_renegotiation_creates_new_version_and_keeps_old_disclosure(self):
        ledger = ready_ledger()
        pay(ledger, key="old", source=SOURCE_AUTO_DEBIT, amount="1000", value_date="2026-05-01")
        # 客户仍失业、再次协商：6 月起新月供
        ledger.approve_plan(
            **standard_plan(
                effective_date="2026-06-01",
                reason="RENEGOTIATION",
                schedule=[
                    {"due_date": "2026-06-15", "amount": "800.00"},
                    {"due_date": "2026-07-15", "amount": "800.00"},
                ],
            )
        )
        versions = ledger.versions()
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(versions[0]["status"], "superseded")
        self.assertEqual(versions[0]["superseded_by"], 2)
        self.assertEqual(versions[1]["status"], "active")
        # 旧承诺与当时披露文本保持可查
        self.assertIn("暂缓本金3个月", ledger.version(1)["disclosure"]["text"])

        # 历史时点回看：v1 及其履约仍完整
        past = ledger.explain("2026-05-31")
        self.assertEqual(past["current_version"], 1)
        self.assertEqual(past["periods"][0]["paid_yuan"], "1000.00")
        # 当前时点：v2 日历，v1 资金不窜版本
        current = ledger.explain("2026-06-15")
        self.assertEqual(current["current_version"], 2)
        self.assertEqual(len(current["periods"]), 2)
        self.assertEqual(current["periods"][0]["due_yuan"], "800.00")

    def test_new_version_cannot_predate_current(self):
        ledger = ready_ledger()
        with self.assertRaises(LedgerError) as ctx:
            ledger.approve_plan(**standard_plan(effective_date="2026-03-15"))
        self.assertEqual(ctx.exception.code, "invalid_effective_date")

    def test_default_restores_original_terms_and_lifts_contact_suppression(self):
        ledger = ready_ledger()
        events = ledger.default_plan("2026-05-10", "MISSED_PERIOD")
        self.assertEqual(len(events), 2)
        self.assertEqual(ledger.version(1)["status"], "defaulted")
        restored = ledger.version(2)
        self.assertEqual(restored["kind"], "RESTORED_ORIGINAL")
        self.assertEqual(restored["contact_policy"]["mode"], "OPEN")
        self.assertEqual(ledger.periods("2026-05-11"), [])
        todo = ledger.next_todo("2026-05-11")
        self.assertEqual(todo["arrangement"], "RESTORED_ORIGINAL")
        # 催收不再受方案时段限制
        view = ledger.contact_view("2026-05-11T20:00:00+08:00")
        self.assertEqual(view["arrangement"], "RESTORED_ORIGINAL")
        self.assertTrue(all(c["allowed"] for c in view["channels"].values()))


class ContactPolicyTest(unittest.TestCase):
    def test_channel_hours_and_blackout_are_enforced(self):
        ledger = ready_ledger()
        # 允许渠道、允许时段（12:00 UTC = 20:00 北京 → 超出 18:00）
        blocked = ledger.record_contact_attempt("PHONE", "2026-05-02T12:00:00Z")
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["reasons"], ["OUTSIDE_ALLOWED_HOURS"])
        # 02:00 UTC = 10:00 北京
        allowed = ledger.record_contact_attempt("PHONE", "2026-05-02T02:00:00Z")
        self.assertFalse(allowed["blocked"])
        # 非允许渠道
        sms = ledger.record_contact_attempt("SMS", "2026-05-02T02:00:00Z")
        self.assertEqual(sms["reasons"], ["CHANNEL_NOT_ALLOWED"])
        # 禁联日
        blackout = ledger.record_contact_attempt("PHONE", "2026-05-01T02:00:00Z")
        self.assertIn("BLACKOUT_DATE", blackout["reasons"])
        # 被阻止的联系也留痕，可向客户/合规解释
        explanation = ledger.explain("2026-05-02")
        self.assertIn("OUTSIDE_ALLOWED_HOURS", explanation["blocked_contacts"][0]["reasons"])

    def test_contact_view_exposes_only_currently_allowed_channels(self):
        ledger = ready_ledger()
        view = ledger.contact_view("2026-05-02T02:00:00Z")
        self.assertEqual(view["allowed_channels"], ["PHONE"])
        self.assertTrue(view["channels"]["PHONE"]["allowed"])
        self.assertFalse(view["channels"]["SMS"]["allowed"])

    def test_daily_cap_is_version_scoped(self):
        plan = standard_plan()
        plan["contact_policy"]["max_per_day"] = 1
        capped = ready_ledger({"contact_policy": plan["contact_policy"]})
        first = capped.record_contact_attempt("PHONE", "2026-05-02T02:00:00Z")
        second = capped.record_contact_attempt("PHONE", "2026-05-02T03:00:00Z")
        self.assertFalse(first["blocked"])
        self.assertIn("DAILY_CAP_REACHED", second["reasons"])
        # 次日恢复
        third = capped.record_contact_attempt("PHONE", "2026-05-03T02:00:00Z")
        self.assertFalse(third["blocked"])


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_postings_share_one_balance_boundary(self):
        ledger = ready_ledger()
        n = 20
        barrier = threading.Barrier(n)

        def worker(i):
            barrier.wait()
            pay(ledger, key=f"c-{i}", source=SOURCE_AUTO_DEBIT, amount="100",
                value_date="2026-05-01", posting_id=f"post-c-{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        periods = ledger.periods("2026-05-02")
        self.assertEqual(sum(p["paid_cents"] for p in periods), to_cents("2000"))
        self.assertEqual(ledger.explain("2026-05-02")["unapplied_yuan"], "0.00")

    def test_same_key_race_admits_exactly_one_posting(self):
        ledger = ready_ledger()
        n = 16
        barrier = threading.Barrier(n)
        results = {"posted": 0, "duplicate": 0, "errors": []}
        lock = threading.Lock()

        def worker():
            barrier.wait()
            try:
                result = pay(ledger, key="race", source=SOURCE_AUTO_DEBIT, amount="100",
                             value_date="2026-05-01", posting_id="post-race")
                with lock:
                    results["duplicate" if result.get("duplicate") else "posted"] += 1
            except Exception as exc:  # noqa: BLE001
                with lock:
                    results["errors"].append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results["errors"], [])
        self.assertEqual(results["posted"], 1)
        self.assertEqual(results["duplicate"], n - 1)
        self.assertEqual(ledger.periods("2026-05-02")[0]["paid_yuan"], "100.00")

    def test_failed_posting_leaves_no_half_performance(self):
        ledger = ready_ledger()
        before = ledger.periods("2026-05-02")
        with self.assertRaises(LedgerError):
            pay(ledger, key="bad", source=SOURCE_AUTO_DEBIT, amount="-50",
                value_date="2026-05-01")
        self.assertEqual(ledger.periods("2026-05-02"), before)


class RecoveryTest(unittest.TestCase):
    def test_state_is_rebuilt_from_event_log_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(tmp)
            ledger = Ledger("acct-9", store)
            ledger.open_account("2026-01-01")
            ledger.record_purchase("p-1", "2026-02-01", "3000.00")
            ledger.approve_plan(**standard_plan())
            pay(ledger, key="dup", source=SOURCE_AUTO_DEBIT, amount="1000",
                value_date="2026-05-01")
            with self.assertRaises(LedgerError):
                pay(ledger, key="dup", source=SOURCE_AUTO_DEBIT, amount="999",
                    value_date="2026-05-01")
            ledger.record_contact_attempt("SMS", "2026-05-02T02:00:00Z")

            # 模拟服务恢复：新实例重放事件
            rebuilt = Ledger("acct-9", EventStore(tmp))
            self.assertEqual(rebuilt.periods("2026-05-02"), ledger.periods("2026-05-02"))
            self.assertEqual(rebuilt.next_todo("2026-05-06"), ledger.next_todo("2026-05-06"))
            self.assertEqual(rebuilt.contact_view("2026-05-02T02:00:00Z"),
                             ledger.contact_view("2026-05-02T02:00:00Z"))
            # 幂等与争议抑制在重建后依然成立
            retry = pay(rebuilt, key="dup", source=SOURCE_AUTO_DEBIT, amount="1000",
                        value_date="2026-05-01")
            self.assertTrue(retry["duplicate"])
            with self.assertRaises(LedgerError):
                pay(rebuilt, key="dup", source=SOURCE_AUTO_DEBIT, amount="999",
                    value_date="2026-05-01")
            self.assertEqual(len(rebuilt.disputes()), 1)
            self.assertEqual(
                rebuilt.explain("2026-05-06"), ledger.explain("2026-05-06")
            )


class ExplainabilityTest(unittest.TestCase):
    def test_explain_answers_what_is_paid_owed_restored_and_blocked(self):
        ledger = ready_ledger()
        pay(ledger, key="p1", source=SOURCE_AUTO_DEBIT, amount="1000", value_date="2026-05-01")
        ledger.receive_funds(
            posting_id="rf", idempotency_key="rf", source=SOURCE_REFUND,
            amount="300", value_date="2026-05-02", purchase_id="p-1",
        )
        ledger.record_contact_attempt("PHONE", "2026-05-02T12:00:00Z")
        explanation = ledger.explain("2026-05-03")
        self.assertEqual(explanation["arrangement_state"], "HARDSHIP")
        self.assertEqual(explanation["totals"]["performed_yuan"], "1000.00")
        self.assertEqual(explanation["totals"]["outstanding_yuan"], "2000.00")
        self.assertEqual(explanation["totals"]["refund_offset_yuan"], "300.00")
        self.assertEqual(explanation["restore_conditions"][0], "连续两期足额按时履行")
        self.assertEqual(explanation["principal_deferral_months"], 3)
        self.assertEqual(len(explanation["blocked_contacts"]), 1)
        # 每期为什么已履行：分配明细可追溯到交易与来源
        first = explanation["posting_allocations"][0]
        self.assertEqual(first["source"], SOURCE_AUTO_DEBIT)
        self.assertEqual(first["allocation"][0]["amount_yuan"], "1000.00")


class MoneyHelperTest(unittest.TestCase):
    def test_integer_cents_and_display(self):
        self.assertEqual(to_cents("1000.00"), 100000)
        self.assertEqual(to_cents(0.1), 10)
        self.assertEqual(yuan(100000), "1000.00")
        with self.assertRaises(LedgerError):
            to_cents("abc")


if __name__ == "__main__":
    unittest.main()
