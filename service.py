"""消费贷审慎额度管理的服务入口，承载协商承诺执行账本接口。

所有写入都是命令式 JSON 请求，状态以事件追加方式落到 --data-dir；
进程重启后重放事件即可重建下一期待办与联系抑制状态。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ledger import EventStore, LedgerError, LedgerRegistry

SERVICE_ID = "responsible-consumer-credit"
SERVICE_NAME = "消费贷审慎额度管理"

# 领域错误码 -> HTTP 状态
_ERROR_STATUS = {
    "account_exists": 409,
    "purchase_exists": 409,
    "account_frozen": 409,
    "hardship_inactive": 409,
    "no_active_plan": 409,
    "dispute_opened": 409,
    "version_not_found": 404,
    "account_not_open": 404,
    "unknown_purchase": 404,
    "refund_exceeds_purchase": 422,
    "purchase_required": 422,
}

REGISTRY = LedgerRegistry()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _require(body: dict, key: str):
    if key not in body or body[key] in (None, ""):
        raise LedgerError("invalid_request", f"缺少必填字段: {key}")
    return body[key]


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与协商承诺执行账本的 JSON 接口。"""

    server_version = "RCCLedger/1.0"

    # -- 基础工具 ----------------------------------------------------------

    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise LedgerError("invalid_header", "Content-Length 非法") from exc
        if length <= 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise LedgerError("invalid_json", f"请求体不是合法 JSON: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise LedgerError("invalid_json", "请求体必须是 JSON 对象")
        return payload

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _account_id(self, parts: list[str]) -> str:
        # /accounts/<id>/...
        account_id = parts[2]
        if not account_id or "/" in account_id or ".." in account_id:
            raise LedgerError("invalid_account_id", "账户ID非法")
        return account_id

    def _ledger(self, account_id: str):
        ledger = REGISTRY.get(account_id)
        if not ledger.state.opened:
            raise LedgerError("account_not_open", f"账户不存在或未开户: {account_id}")
        return ledger

    # -- 路由 --------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/health":
                self._send_json(health_payload())
                return
            parts = path.split("/")
            # /accounts/<id>/<resource...>
            if len(parts) >= 4 and parts[1] == "accounts":
                account_id = self._account_id(parts)
                ledger = self._ledger(account_id)
                resource = parts[3] if len(parts) > 3 else ""
                query = self._query()
                if len(parts) == 4 and resource == "":
                    self._send_json(ledger.snapshot())
                elif resource == "versions":
                    if len(parts) == 4:
                        self._send_json({"versions": ledger.versions()})
                    elif len(parts) == 5:
                        try:
                            number = int(parts[4])
                        except ValueError as exc:
                            raise LedgerError("invalid_version", "版本号必须是整数") from exc
                        self._send_json(ledger.version(number))
                    else:
                        raise LedgerError("not_found", "未知路由")
                elif resource == "periods":
                    self._send_json(
                        {"as_of": _require(query, "as_of"),
                         "periods": ledger.periods(_require(query, "as_of"))}
                    )
                elif resource == "next-todo":
                    self._send_json(ledger.next_todo(_require(query, "as_of")))
                elif resource == "contact-view":
                    self._send_json(ledger.contact_view(_require(query, "ts")))
                elif resource == "explain":
                    self._send_json(ledger.explain(_require(query, "as_of")))
                elif resource == "disputes":
                    self._send_json({"disputes": ledger.disputes()})
                else:
                    raise LedgerError("not_found", f"未知资源: {resource}")
            else:
                raise LedgerError("not_found", "未知路由")
        except LedgerError as exc:
            self._send_error(exc)
        except Exception as exc:  # noqa: BLE001 - 接口层兜底，绝不泄漏堆栈
            self._send_json(
                {"error": {"code": "internal_error", "message": str(exc)}}, status=500
            )

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            body = self._read_json()
            parts = path.split("/")
            if len(parts) == 2 and parts[1] == "accounts":
                account_id = _require(body, "account_id")
                ledger = REGISTRY.open(account_id, _require(body, "opened_date"))
                self._send_json(ledger.snapshot(), status=201)
                return
            if len(parts) >= 4 and parts[1] == "accounts":
                account_id = self._account_id(parts)
                ledger = self._ledger(account_id)
                resource = parts[3]
                if resource == "purchases":
                    event = ledger.record_purchase(
                        _require(body, "purchase_id"),
                        _require(body, "date"),
                        _require(body, "amount"),
                    )
                    self._send_json({"event": event}, status=201)
                elif resource == "plans":
                    event = ledger.approve_plan(
                        effective_date=_require(body, "effective_date"),
                        reason=_require(body, "reason"),
                        principal_deferral_months=_require(body, "principal_deferral_months"),
                        schedule=_require(body, "schedule"),
                        forgiveness=body.get("forgiveness", []),
                        contact_policy=_require(body, "contact_policy"),
                        grace_days=_require(body, "grace_days"),
                        min_partial_ratio=_require(body, "min_partial_ratio"),
                        restore_conditions=_require(body, "restore_conditions"),
                        disclosure=_require(body, "disclosure"),
                    )
                    self._send_json({"event": event, "version": ledger.version(event["version"])}, 201)
                elif resource == "default":
                    events = ledger.default_plan(
                        _require(body, "date"), _require(body, "reason")
                    )
                    self._send_json({"events": events}, 201)
                elif resource == "funds":
                    result = ledger.receive_funds(
                        posting_id=_require(body, "posting_id"),
                        idempotency_key=_require(body, "idempotency_key"),
                        source=_require(body, "source"),
                        amount=_require(body, "amount"),
                        value_date=_require(body, "value_date"),
                        received_at=body.get("received_at"),
                        purchase_id=body.get("purchase_id"),
                    )
                    status = 200 if result.get("duplicate") else 201
                    self._send_json({"result": result}, status)
                elif resource == "contacts":
                    event = ledger.record_contact_attempt(
                        _require(body, "channel"),
                        _require(body, "ts"),
                        attempt_id=body.get("attempt_id"),
                    )
                    self._send_json({"event": event, "decision": {
                        "blocked": event["blocked"],
                        "reasons": event["reasons"],
                    }}, 201)
                else:
                    raise LedgerError("not_found", f"未知资源: {resource}")
            else:
                raise LedgerError("not_found", "未知路由")
        except LedgerError as exc:
            self._send_error(exc)
        except Exception as exc:  # noqa: BLE001 - 接口层兜底，绝不泄漏堆栈
            self._send_json(
                {"error": {"code": "internal_error", "message": str(exc)}}, status=500
            )

    def _send_error(self, exc: LedgerError):
        if exc.code == "not_found":
            self.send_error(404)
            return
        status = _ERROR_STATUS.get(exc.code, 400)
        payload = {"error": {"code": exc.code, "message": str(exc)}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def build_server(port: int, data_dir: str) -> ThreadingHTTPServer:
    global REGISTRY
    if data_dir:
        REGISTRY = LedgerRegistry(EventStore(data_dir))
    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data-dir", default="data", help="事件日志目录（JSONL）")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    build_server(args.port, args.data_dir).serve_forever()


if __name__ == "__main__":
    main()
