"""协商承诺执行账本的端到端契约。

覆盖消保场景的关键不变量：冻结、版本留痕、资金按来源与价值日分配、
退款不算履约、幂等与争议、部分到账宽限与调用顺序无关、联系限制、
并发同一余额边界、失败无半次履约、快照恢复重建。
"""

import json
import os
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from ledger import (
    Ledger, LedgerError, SOURCE_AUTODRAFT, SOURCE_MANUAL, SOURCE_REFUND,
    REASON_RENEGOTIATION, REASON_UNEMPLOYMENT,
)

HARDSHIP_POLICY = {
    "allowed_channels": ["sms", "email"],
    "allowed_hours": {"start": "09:00", "end": "18:00"},
}
STANDARD_POLICY = {
    "allowed_channels": ["phone", "sms", "email", "letter"],
    "allowed_hours": {"start": "08:00", "end": "21:00"},
}


def standard_plan_payload(**overrides):
    due = date(2026, 4, 10)
    payload = {
        "effective_date": "2026-04-01",
        "forbearance": {
            "principal_deferral_months": 3,
            "deferred_principal_amount": "3000.00",
        },
        "waivers": [
            {"code": "FEE-001", "description": "减免逾期费",
             "amount": "50.00", "period_no": 1},
        ],
        "payment_calendar": [
            {"period_no": 1, "due_date": due.isoformat(), "amount_due": "500.00"},
            {"period_no": 2, "due_date": (due + timedelta(days=31)).isoformat(),
             "amount_due": "500.00"},
            {"period_no": 3, "due_date": (due + timedelta(days=61)).isoformat(),
             "amount_due": "500.00"},
        ],
        "grace_rule": {"grace_days": 7, "minimum_ratio": "0.5"},
        "contact_policy": HARDSHIP_POLICY,
        "resumption": {
            "condition": "calendar_complete",
            "resume_date": "2026-08-01",
            "detail": "三期按时履行后恢复普通还款，暂缓本金并入第 8 月账单",
        },
    }
    payload.update(overrides)
    return payload


def open_ledger_with_account(ledger=None, account_id="acct-1"):
    ledger = ledger or Ledger()
    ledger.create_account({
        "account_id": account_id,
        "standard_contact_policy": STANDARD_POLICY,
        "purchases": [
            {"purchase_id": "pur-1", "amount": "1200.00"},
            {"purchase_id": "pur-2", "amount": "800.00"},
        ],
    })
    return ledger


def approved_ledger():
    ledger = open_ledger_with_account()
    ledger.approve_plan("acct-1", standard_plan_payload())
    return ledger


class PlanApprovalTest(unittest.TestCase):
    def test_approval_freezes_account_and_freezes_terms(self):
        ledger = approved_ledger()
        account = ledger.get_account("acct-1")
        self.assertTrue(account["frozen"])
        versions = ledger.list_plans("acct-1")
        self.assertEqual(len(versions), 1)
        plan = versions[0]
        self.assertEqual(plan["status"], "active")
        self.assertEqual(plan["version_id"], "plan-acct-1-v1")
        self.assertEqual(plan["forbearance"]["principal_deferral_months"], 3)
        self.assertEqual(plan["waivers"][0]["amount"], "50.00")
        self.assertEqual(plan["contact_policy"]["allowed_channels"], ["sms", "email"])
        self.assertEqual(plan["resumption"]["resume_date"], "2026-08-01")
        # 披露内容随版本固化。
        self.assertEqual(plan["disclosure"]["type"], "hardship_terms")
        self.assertEqual(plan["disclosure"]["payment_calendar"][0]["amount_due"], "500.00")

    def test_second_approval_without_renegotiation_is_rejected(self):
        ledger = approved_ledger()
        with self.assertRaises(LedgerError) as error:
            ledger.approve_plan("acct-1", standard_plan_payload())
        self.assertEqual(error.exception.code, "plan_already_active")
        # 失败事务不能改动冻结状态或版本数量。
        self.assertEqual(len(ledger.list_plans("acct-1")), 1)

    def test_waiver_must_target_existing_period(self):
        ledger = open_ledger_with_account()
        payload = standard_plan_payload(waivers=[
            {"code": "X", "amount": "10.00", "period_no": 9},
        ])
        with self.assertRaises(LedgerError):
            ledger.approve_plan("acct-1", payload)
        self.assertFalse(ledger.get_account("acct-1")["frozen"])


class FundPostingTest(unittest.TestCase):
    def test_retransmission_is_idempotent(self):
        ledger = approved_ledger()
        first = ledger.post_fund("acct-1", {
            "fund_id": "tx-1", "source": SOURCE_AUTODRAFT,
            "amount": "500.00", "value_date": "2026-04-10",
        })
        self.assertEqual(first["outcome"], "posted")
        second = ledger.post_fund("acct-1", {
            "fund_id": "tx-1", "source": SOURCE_AUTODRAFT,
            "amount": "500.00", "value_date": "2026-04-10",
        })
        self.assertEqual(second["outcome"], "duplicate")
        self.assertEqual(len(ledger.list_funds("acct-1")), 1)

    def test_same_id_changed_amount_or_source_opens_dispute(self):
        ledger = approved_ledger()
        ledger.post_fund("acct-1", {
            "fund_id": "tx-1", "source": SOURCE_AUTODRAFT,
            "amount": "500.00", "value_date": "2026-04-10",
        })
        changed = ledger.post_fund("acct-1", {
            "fund_id": "tx-1", "source": SOURCE_MANUAL,
            "amount": "480.00", "value_date": "2026-04-10",
        })
        self.assertEqual(changed["outcome"], "disputed")
        self.assertTrue(changed["dispute_id"].startswith("disp-"))
        # 争议中的新交易绝不入账，原交易金额保持不变。
        funds = ledger.list_funds("acct-1")
        self.assertEqual(len(funds), 1)
        self.assertEqual(funds[0]["amount"], "500.00")
        disputes = ledger.list_disputes("acct-1")
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0]["status"], "open")
        self.assertEqual(disputes[0]["incoming"]["amount"], "480.00")
        # 同一变异重传不重复开争议。
        again = ledger.post_fund("acct-1", {
            "fund_id": "tx-1", "source": SOURCE_MANUAL,
            "amount": "480.00", "value_date": "2026-04-10",
        })
        self.assertEqual(again["dispute_id"], changed["dispute_id"])
        self.assertEqual(len(ledger.list_disputes("acct-1")), 1)

    def test_refund_only_charges_back_purchase_and_never_performs(self):
        ledger = approved_ledger()
        with self.assertRaises(LedgerError) as error:
            ledger.post_fund("acct-1", {
                "fund_id": "r-1", "source": SOURCE_REFUND,
                "amount": "100.00", "value_date": "2026-04-09",
            })
        self.assertEqual(error.exception.code, "refund_requires_purchase")
        ledger.post_fund("acct-1", {
            "fund_id": "r-1", "source": SOURCE_REFUND,
            "amount": "100.00", "value_date": "2026-04-09",
            "linked_purchase_id": "pur-1",
        })
        # 退款超过关联消费余额必须拒绝。
        with self.assertRaises(LedgerError) as error:
            ledger.post_fund("acct-1", {
                "fund_id": "r-2", "source": SOURCE_REFUND,
                "amount": "1200.00", "value_date": "2026-04-09",
                "linked_purchase_id": "pur-1",
            })
        self.assertEqual(error.exception.code, "refund_exceeds_purchase")
        projection = ledger.project_payments("acct-1", as_of="2026-04-10")
        self.assertEqual(projection["periods"][0]["paid"], "0.00")
        chargebacks = projection["refund_chargebacks"]
        self.assertEqual(len(chargebacks), 1)
        self.assertEqual(chargebacks[0]["purchase_id"], "pur-1")
        self.assertFalse(chargebacks[0]["counts_as_performance"])

    def test_failed_posting_leaves_no_half_performance(self):
        ledger = approved_ledger()
        with self.assertRaises(LedgerError):
            ledger.post_fund("acct-1", {
                "fund_id": "bad", "source": SOURCE_MANUAL,
                "amount": "-1.00", "value_date": "2026-04-10",
            })
        with self.assertRaises(LedgerError) as error:
            ledger.post_fund("acct-1", {
                "fund_id": "r-x", "source": SOURCE_REFUND,
                "amount": "99999.00", "value_date": "2026-04-09",
                "linked_purchase_id": "pur-1",
            })
        self.assertEqual(error.exception.code, "refund_exceeds_purchase")
        self.assertEqual(ledger.list_funds("acct-1"), [])
        account = ledger.get_account("acct-1")
        self.assertEqual(account["purchases"][0]["refunded"], 0)


class AllocationAndGraceTest(unittest.TestCase):
    def test_partial_payment_grace_is_rule_based(self):
        ledger = approved_ledger()
        ledger.post_fund("acct-1", {
            "fund_id": "p-1", "source": SOURCE_MANUAL,
            "amount": "250.00", "value_date": "2026-04-10",
        })
        # 应还 500，减免 50，门槛 250；实付 250 >= 250，宽限期内算宽限。
        in_grace = ledger.project_payments("acct-1", as_of="2026-04-12")
        self.assertEqual(in_grace["periods"][0]["status"], "within_grace")
        self.assertEqual(in_grace["periods"][0]["outstanding"], "200.00")
        # 超过 7 天宽限仍未补足即逾期，结论只由日期与规则决定。
        past_due = ledger.project_payments("acct-1", as_of="2026-04-20")
        self.assertEqual(past_due["periods"][0]["status"], "past_due")

    def test_under_threshold_partial_is_due_even_inside_grace_window(self):
        ledger = approved_ledger()
        ledger.post_fund("acct-1", {
            "fund_id": "p-1", "source": SOURCE_MANUAL,
            "amount": "100.00", "value_date": "2026-04-10",
        })
        projection = ledger.project_payments("acct-1", as_of="2026-04-12")
        self.assertEqual(projection["periods"][0]["status"], "due")

    def test_allocation_is_independent_of_arrival_order(self):
        funds = [
            ("a", SOURCE_AUTODRAFT, "100.00", "2026-04-10"),
            ("b", SOURCE_MANUAL, "450.00", "2026-04-08"),
            ("c", SOURCE_AUTODRAFT, "300.00", "2026-04-11"),
        ]

        def build(order):
            ledger = approved_ledger()
            for index in order:
                fid, source, amount, value_date = funds[index]
                ledger.post_fund("acct-1", {
                    "fund_id": fid, "source": source, "amount": amount,
                    "value_date": value_date,
                })
            return ledger.project_payments("acct-1", as_of="2026-05-31")

        forward = build([0, 1, 2])
        reverse = build([2, 1, 0])
        self.assertEqual(forward, reverse)
        # 价值日 04-08 的 450 元恰好付清减免后首期，其余 400 元进入第二期。
        first = forward["periods"][0]
        self.assertEqual(first["status"], "satisfied")
        self.assertEqual(first["funds"][0]["fund_id"], "b")
        self.assertEqual(forward["periods"][1]["paid"], "400.00")

    def test_future_value_dated_fund_does_not_perform_early(self):
        ledger = approved_ledger()
        ledger.post_fund("acct-1", {
            "fund_id": "early", "source": SOURCE_MANUAL,
            "amount": "450.00", "value_date": "2026-04-20",
        })
        projection = ledger.project_payments("acct-1", as_of="2026-04-12")
        self.assertEqual(projection["periods"][0]["paid"], "0.00")
        self.assertEqual(projection["postdated"][0]["fund_id"], "early")

    def test_complete_calendar_unfreezes_account(self):
        ledger = approved_ledger()
        for period_no, day in enumerate(("2026-04-10", "2026-05-11", "2026-06-10")):
            amount = "450.00" if period_no == 0 else "500.00"
            ledger.post_fund("acct-1", {
                "fund_id": f"pay-{period_no}", "source": SOURCE_AUTODRAFT,
                "amount": amount, "value_date": day,
            })
        self.assertEqual(ledger.get_account("acct-1")["frozen"], False)
        self.assertEqual(
            ledger.list_plans("acct-1")[0]["status"], "completed"
        )


class VersioningTest(unittest.TestCase):
    def _v1_with_payment(self):
        ledger = approved_ledger()
        ledger.post_fund("acct-1", {
            "fund_id": "old-pay", "source": SOURCE_MANUAL,
            "amount": "200.00", "value_date": "2026-04-10",
        })
        return ledger

    def test_unemployment_update_creates_superseding_version(self):
        ledger = self._v1_with_payment()
        new_payload = standard_plan_payload(
            reason=REASON_UNEMPLOYMENT,
            effective_date="2026-05-01",
            payment_calendar=[
                {"period_no": 1, "due_date": "2026-06-10", "amount_due": "300.00"},
                {"period_no": 2, "due_date": "2026-07-10", "amount_due": "300.00"},
            ],
            waivers=[],
        )
        new_plan = ledger.approve_plan("acct-1", new_payload)
        self.assertEqual(new_plan["version_id"], "plan-acct-1-v2")
        self.assertEqual(new_plan["parent_version_id"], "plan-acct-1-v1")
        versions = ledger.list_plans("acct-1")
        self.assertEqual(versions[0]["status"], "superseded")
        self.assertEqual(versions[0]["superseded_by"], "plan-acct-1-v2")
        self.assertEqual(versions[1]["status"], "active")
        # 旧承诺与其披露、资金明细仍可查。
        self.assertEqual(versions[0]["disclosure"]["type"], "hardship_terms")
        old_funds = [f for f in ledger.list_funds("acct-1")
                     if f["plan_version_id"] == "plan-acct-1-v1"]
        self.assertEqual(old_funds[0]["fund_id"], "old-pay")

    def test_renegotiation_requires_current_parent(self):
        ledger = self._v1_with_payment()
        payload = standard_plan_payload(
            reason=REASON_RENEGOTIATION, parent_version_id="plan-acct-1-v999",
        )
        with self.assertRaises(LedgerError) as error:
            ledger.approve_plan("acct-1", payload)
        self.assertEqual(error.exception.code, "version_conflict")

    def test_default_restores_original_contract(self):
        ledger = self._v1_with_payment()
        restored = ledger.record_default("acct-1", {"at": "2026-05-02"})
        self.assertEqual(restored["reason"], "default")
        account = ledger.get_account("acct-1")
        self.assertFalse(account["frozen"])
        policy = ledger.effective_contact_policy("acct-1", at="2026-05-02T10:00:00")
        self.assertEqual(policy["source"], "standard_contract")
        self.assertIn("phone", policy["allowed_channels"])
        versions = ledger.list_plans("acct-1")
        self.assertEqual(versions[0]["status"], "breached")
        # 违约后承诺账本不再接受该账户的资金。
        with self.assertRaises(LedgerError) as error:
            ledger.post_fund("acct-1", {
                "fund_id": "late", "source": SOURCE_MANUAL,
                "amount": "10.00", "value_date": "2026-05-03",
            })
        self.assertEqual(error.exception.code, "plan_breached")


class ContactRestrictionTest(unittest.TestCase):
    def test_collector_sees_only_current_channels_and_hours(self):
        ledger = approved_ledger()
        policy = ledger.effective_contact_policy(
            "acct-1", at="2026-04-05T10:00:00"
        )
        self.assertEqual(policy["allowed_channels"], ["sms", "email"])
        self.assertEqual(policy["source"], "hardship_plan")

        allowed = ledger.record_contact_attempt("acct-1", {
            "attempt_id": "c-1", "channel": "sms", "at": "2026-04-05T10:00:00",
        })
        self.assertEqual(allowed["decision"], "allowed")

        blocked_phone = ledger.record_contact_attempt("acct-1", {
            "attempt_id": "c-2", "channel": "phone", "at": "2026-04-05T10:00:00",
        })
        self.assertEqual(blocked_phone["decision"], "blocked")
        self.assertIn("channel_not_allowed", blocked_phone["blocked_reasons"])

        blocked_night = ledger.record_contact_attempt("acct-1", {
            "attempt_id": "c-3", "channel": "sms", "at": "2026-04-05T20:00:00",
        })
        self.assertEqual(blocked_night["decision"], "blocked")
        self.assertIn("outside_allowed_hours", blocked_night["blocked_reasons"])

        # 被阻止的联系在客户/合规解释中可查。
        explanation = ledger.explain("acct-1", as_of="2026-04-05")
        blocked = explanation["blocked_contacts"]
        self.assertEqual({b["attempt_id"] for b in blocked}, {"c-2", "c-3"})

    def test_contact_attempt_is_idempotent(self):
        ledger = approved_ledger()
        payload = {"attempt_id": "c-9", "channel": "sms", "at": "2026-04-05T10:00:00"}
        first = ledger.record_contact_attempt("acct-1", payload)
        second = ledger.record_contact_attempt("acct-1", payload)
        self.assertEqual(first, second)
        self.assertEqual(len(ledger.list_contact_attempts("acct-1")), 1)


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_postings_commit_in_one_balance_boundary(self):
        ledger = approved_ledger()
        errors = []

        def post(fund_id, amount):
            try:
                ledger.post_fund("acct-1", {
                    "fund_id": fund_id, "source": SOURCE_MANUAL,
                    "amount": amount, "value_date": "2026-04-10",
                })
            except LedgerError as exc:  # pragma: no cover - 测试失败时暴露
                errors.append(exc)

        threads = [
            threading.Thread(target=post, args=(f"cc-{i}", "1.00"))
            for i in range(40)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        projection = ledger.project_payments("acct-1", as_of="2026-04-10")
        # 40 笔各 1 元全部进入同一余额边界，无丢失、无重复、无半次入账。
        self.assertEqual(len(ledger.list_funds("acct-1")), 40)
        total_paid = sum(
            int(float(p["paid"])) for p in projection["periods"]
        ) + sum(int(float(x["amount"])) for x in projection["unapplied"])
        self.assertEqual(total_paid, 40)
        self.assertEqual(projection["periods"][0]["paid"], "40.00")

    def test_concurrent_duplicate_submissions_post_once(self):
        ledger = approved_ledger()
        outcomes = []
        lock = threading.Lock()

        def post():
            result = ledger.post_fund("acct-1", {
                "fund_id": "same", "source": SOURCE_AUTODRAFT,
                "amount": "500.00", "value_date": "2026-04-10",
            })
            with lock:
                outcomes.append(result["outcome"])

        threads = [threading.Thread(target=post) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("posted"), 1)
        self.assertEqual(outcomes.count("duplicate"), 19)
        self.assertEqual(len(ledger.list_funds("acct-1")), 1)


class ExplainAndRecoveryTest(unittest.TestCase):
    def test_explain_reports_what_is_owed_and_when_repayment_resumes(self):
        ledger = approved_ledger()
        ledger.post_fund("acct-1", {
            "fund_id": "p-1", "source": SOURCE_AUTODRAFT,
            "amount": "450.00", "value_date": "2026-04-10",
        })
        explanation = ledger.explain("acct-1", as_of="2026-04-10")
        self.assertEqual(explanation["totals"]["due"], "1500.00")
        self.assertEqual(explanation["totals"]["paid"], "450.00")
        self.assertEqual(explanation["totals"]["waived"], "50.00")
        self.assertEqual(explanation["totals"]["outstanding"], "1000.00")
        self.assertEqual(
            explanation["totals"]["deferred_principal_at_resumption"], "3000.00"
        )
        self.assertEqual(explanation["resumption"]["resume_date"], "2026-08-01")
        self.assertEqual(explanation["periods"][0]["status"], "satisfied")
        todo = ledger.next_todo("acct-1", as_of="2026-04-10")
        self.assertEqual(todo["next_period"]["period_no"], 2)

    def test_snapshot_recovery_rebuilds_todo_and_suppression(self):
        tmp_dir = tempfile.mkdtemp()
        snapshot = os.path.join(tmp_dir, "ledger.json")
        ledger = Ledger(snapshot)
        open_ledger_with_account(ledger)
        ledger.approve_plan("acct-1", standard_plan_payload())
        ledger.post_fund("acct-1", {
            "fund_id": "p-1", "source": SOURCE_AUTODRAFT,
            "amount": "450.00", "value_date": "2026-04-10",
        })
        ledger.record_contact_attempt("acct-1", {
            "attempt_id": "c-1", "channel": "phone", "at": "2026-04-05T10:00:00",
        })
        ledger.shutdown()

        recovered = Ledger(snapshot)
        summary = recovered.recover()
        account_summary = next(
            item for item in summary["accounts"] if item["account_id"] == "acct-1"
        )
        self.assertTrue(account_summary["frozen"])
        self.assertEqual(account_summary["current_version_id"], "plan-acct-1-v1")
        self.assertEqual(account_summary["next_todo"]["period_no"], 2)
        restriction = account_summary["contact_restriction"]
        self.assertEqual(restriction["source"], "hardship_plan")
        self.assertEqual(restriction["allowed_channels"], ["sms", "email"])
        # 重建后资金幂等性仍然成立。
        duplicate = recovered.post_fund("acct-1", {
            "fund_id": "p-1", "source": SOURCE_AUTODRAFT,
            "amount": "450.00", "value_date": "2026-04-10",
        })
        self.assertEqual(duplicate["outcome"], "duplicate")
        attempts = recovered.list_contact_attempts("acct-1")
        self.assertEqual(attempts[0]["decision"], "blocked")
        recovered.shutdown()


class HttpContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.snapshot = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        service.reset_ledger(cls.snapshot)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        service.get_ledger().shutdown()
        os.unlink(cls.snapshot)

    def _request(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        return urlopen(request, timeout=3)

    def test_ledger_flow_over_http(self):
        with self._request("POST", "/v1/accounts", {
            "account_id": "http-acct",
            "standard_contact_policy": STANDARD_POLICY,
            "purchases": [{"purchase_id": "pur-9", "amount": "100.00"}],
        }) as response:
            self.assertEqual(response.status, 201)

        with self._request("POST", "/v1/accounts/http-acct/plans",
                           standard_plan_payload()) as response:
            self.assertEqual(response.status, 201)
            plan = json.load(response)
            self.assertTrue(plan["version_id"].endswith("v1"))

        with self._request("POST", "/v1/accounts/http-acct/funds", {
            "fund_id": "h-1", "source": SOURCE_AUTODRAFT,
            "amount": "450.00", "value_date": "2026-04-10",
        }) as response:
            self.assertEqual(response.status, 201)

        # 重传返回 200 + duplicate。
        with self._request("POST", "/v1/accounts/http-acct/funds", {
            "fund_id": "h-1", "source": SOURCE_AUTODRAFT,
            "amount": "450.00", "value_date": "2026-04-10",
        }) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)["outcome"], "duplicate")

        with self._request(
            "GET", "/v1/accounts/http-acct/explain?as_of=2026-04-10"
        ) as response:
            explanation = json.load(response)
        self.assertEqual(explanation["periods"][0]["status"], "satisfied")

        with self._request("POST", "/v1/accounts/http-acct/contact-attempts", {
            "attempt_id": "hc-1", "channel": "phone",
            "at": "2026-04-05T10:00:00",
        }) as response:
            attempt = json.load(response)
        self.assertEqual(attempt["decision"], "blocked")

        with self._request("GET", "/v1/accounts/http-acct/contact-policy"
                                 "?at=2026-04-05T10:00:00") as response:
            policy = json.load(response)
        self.assertEqual(policy["allowed_channels"], ["sms", "email"])

        with self._request("POST", "/v1/recover", {}) as response:
            summary = json.load(response)
        self.assertTrue(
            any(a["account_id"] == "http-acct" for a in summary["accounts"])
        )

    def test_domain_error_has_stable_code(self):
        try:
            self._request("POST", "/v1/accounts/missing/funds", {
                "fund_id": "x", "source": SOURCE_MANUAL,
                "amount": "1.00", "value_date": "2026-04-10",
            })
            self.fail("应当返回 404")
        except HTTPError as error:
            self.assertEqual(error.code, 404)
            body = json.load(error)
            self.assertEqual(body["error"]["code"], "not_found")
            error.close()


if __name__ == "__main__":
    unittest.main()
