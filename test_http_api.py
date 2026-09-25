"""协商承诺执行账本的 HTTP 接口契约测试。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from service import Handler
from ledger import LedgerRegistry


def _request(url, method="GET", payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 内存注册表，不落盘
        import service as service_module
        service_module.REGISTRY = LedgerRegistry()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _open(self, account_id="http-acct"):
        status, body = _request(f"{self.base}/accounts", "POST",
                                {"account_id": account_id, "opened_date": "2026-01-01"})
        self.assertEqual(status, 201, body)

    def _plan(self):
        return {
            "effective_date": "2026-04-01",
            "reason": "JOB_LOSS",
            "principal_deferral_months": 3,
            "schedule": [
                {"due_date": "2026-05-01", "amount": "1000.00"},
                {"due_date": "2026-06-01", "amount": "1000.00"},
            ],
            "forgiveness": [],
            "contact_policy": {
                "allowed_channels": ["PHONE"],
                "allowed_hours": {"start": "09:00", "end": "18:00"},
                "tz_offset_minutes": 480,
            },
            "grace_days": 5,
            "min_partial_ratio": "0.5",
            "restore_conditions": ["连续两期按时足额"],
            "disclosure": {"text": "暂缓3个月", "at": "2026-03-28T10:00:00+08:00"},
        }

    def test_full_lifecycle_over_http(self):
        self._open()
        status, body = _request(f"{self.base}/accounts/http-acct/purchases", "POST",
                                {"purchase_id": "p1", "date": "2026-02-01", "amount": "3000"})
        self.assertEqual(status, 201, body)

        status, body = _request(f"{self.base}/accounts/http-acct/plans", "POST", self._plan())
        self.assertEqual(status, 201, body)
        self.assertEqual(body["version"]["status"], "active")

        # 履约入账 + 重传幂等
        funds = {"posting_id": "f1", "idempotency_key": "k1", "source": "AUTO_DEBIT",
                 "amount": "1000", "value_date": "2026-05-01"}
        status, first = _request(f"{self.base}/accounts/http-acct/funds", "POST", funds)
        self.assertEqual(status, 201, first)
        status, retry = _request(f"{self.base}/accounts/http-acct/funds", "POST", funds)
        self.assertEqual(status, 200, retry)
        self.assertTrue(retry["result"]["duplicate"])

        # 退款冲消费
        status, refund = _request(f"{self.base}/accounts/http-acct/funds", "POST", {
            "posting_id": "r1", "idempotency_key": "r1", "source": "MERCHANT_REFUND",
            "amount": "200", "value_date": "2026-05-02", "purchase_id": "p1",
        })
        self.assertEqual(status, 201, refund)

        # 金额变化 -> 争议 409
        status, conflict = _request(f"{self.base}/accounts/http-acct/funds", "POST", {
            **funds, "amount": "1001",
        })
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "dispute_opened")

        # 联系被阻止
        status, contact = _request(f"{self.base}/accounts/http-acct/contacts", "POST", {
            "channel": "SMS", "ts": "2026-05-02T02:00:00Z",
        })
        self.assertEqual(status, 201)
        self.assertTrue(contact["decision"]["blocked"])

        # 查询
        status, periods = _request(f"{self.base}/accounts/http-acct/periods?as_of=2026-05-03")
        self.assertEqual(status, 200)
        self.assertEqual(periods["periods"][0]["paid_yuan"], "1000.00")

        status, view = _request(f"{self.base}/accounts/http-acct/contact-view?ts=2026-05-02T02:00:00Z")
        self.assertEqual(status, 200)
        self.assertEqual(view["allowed_channels"], ["PHONE"])

        status, explanation = _request(f"{self.base}/accounts/http-acct/explain?as_of=2026-05-03")
        self.assertEqual(status, 200)
        self.assertEqual(explanation["totals"]["refund_offset_yuan"], "200.00")
        self.assertEqual(explanation["totals"]["performed_yuan"], "1000.00")

        # 违约恢复
        status, defaulted = _request(f"{self.base}/accounts/http-acct/default", "POST",
                                     {"date": "2026-06-10", "reason": "MISSED"})
        self.assertEqual(status, 201, defaulted)
        status, versions = _request(f"{self.base}/accounts/http-acct/versions")
        self.assertEqual(status, 200)
        self.assertEqual([v["status"] for v in versions["versions"]], ["defaulted", "active"])

    def test_unknown_account_404(self):
        status, body = _request(f"{self.base}/accounts/missing/periods?as_of=2026-05-01")
        self.assertEqual(status, 404, body)


if __name__ == "__main__":
    unittest.main()
