"""消费贷审慎额度管理的运行入口。

除健康检查外，本模块暴露协商承诺执行账本的 JSON 接口：账户与承诺版本、
资金入账、争议、逐期履约解释、联系策略与服务恢复。账本默认保存在内存，
设置 LEDGER_SNAPSHOT_PATH 后使用原子快照持久化，多线程写入都在账本自身
的余额边界锁内串行提交。
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from ledger import Ledger, LedgerError

SERVICE_ID = "responsible-consumer-credit"
SERVICE_NAME = "消费贷审慎额度管理"

_LEDGER = None


def get_ledger():
    """惰性创建进程内唯一账本，供 HTTP 处理器与测试共用。"""
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = Ledger(os.environ.get("LEDGER_SNAPSHOT_PATH"))
    return _LEDGER


def reset_ledger(path=None):
    """测试辅助：重建账本（例如切换到临时快照路径）。"""
    global _LEDGER
    if _LEDGER is not None:
        _LEDGER.shutdown()
    _LEDGER = Ledger(path)
    return _LEDGER


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """健康检查与协商承诺执行账本接口。"""

    server_version = "ResponsibleConsumerCredit/1.0"

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                self._write_json(200, health_payload())
                return
            if path.startswith("/v1/"):
                self._route_v1(method, path, query)
                return
            self.send_error(404)
        except LedgerError as error:
            self._write_json(
                error.status,
                {"error": {"code": error.code, "message": str(error)}},
            )
        except json.JSONDecodeError:
            self._write_json(
                400, {"error": {"code": "invalid_json", "message": "请求体不是合法 JSON"}}
            )
        except (KeyError, TypeError, ValueError) as error:
            self._write_json(
                400, {"error": {"code": "bad_request", "message": str(error)}}
            )

    # -- v1 路由 ------------------------------------------------------------

    def _route_v1(self, method, path, query):
        if method == "POST" and path == "/v1/recover":
            self._write_json(200, get_ledger().recover())
            return

        if method == "POST" and path == "/v1/accounts":
            self._write_json(201, get_ledger().create_account(self._read_body()))
            return

        parts = [unquote(segment) for segment in path.split("/") if segment]
        # /v1/accounts/{account_id}/...
        if len(parts) >= 3 and parts[0] == "v1" and parts[1] == "accounts":
            account_id = parts[2]
            resource = parts[3] if len(parts) > 3 else None
            body = self._read_body() if method == "POST" else None

            if resource is None:
                if method == "POST":
                    self._write_json(201, get_ledger().create_account(body))
                elif method == "GET":
                    self._write_json(200, get_ledger().get_account(account_id))
                else:
                    self.send_error(405)
                return

            if resource == "plans":
                if method == "POST":
                    self._write_json(201, get_ledger().approve_plan(account_id, body))
                elif method == "GET":
                    self._write_json(200, {"versions": get_ledger().list_plans(account_id)})
                else:
                    self.send_error(405)
                return

            if resource == "default" and method == "POST":
                self._write_json(201, get_ledger().record_default(account_id, body))
                return

            if resource == "funds":
                if method == "POST":
                    result = get_ledger().post_fund(account_id, body)
                    # 重传与争议没有产生新入账，返回 200 与 outcome 说明。
                    status = 200 if result.get("outcome") in ("duplicate", "disputed") else 201
                    self._write_json(status, result)
                elif method == "GET":
                    self._write_json(200, {"funds": get_ledger().list_funds(account_id)})
                else:
                    self.send_error(405)
                return

            if resource == "disputes" and method == "GET":
                self._write_json(
                    200, {"disputes": get_ledger().list_disputes(account_id)}
                )
                return

            if resource == "payments" and method == "GET":
                self._write_json(
                    200,
                    get_ledger().project_payments(account_id, _one(query, "as_of")),
                )
                return

            if resource == "explain" and method == "GET":
                self._write_json(
                    200, get_ledger().explain(account_id, _one(query, "as_of"))
                )
                return

            if resource == "next-todo" and method == "GET":
                self._write_json(
                    200, get_ledger().next_todo(account_id, _one(query, "as_of"))
                )
                return

            if resource == "contact-policy" and method == "GET":
                self._write_json(
                    200,
                    get_ledger().effective_contact_policy(
                        account_id, _one(query, "at")
                    ),
                )
                return

            if resource == "contact-attempts":
                if method == "POST":
                    self._write_json(
                        201, get_ledger().record_contact_attempt(account_id, body)
                    )
                elif method == "GET":
                    self._write_json(
                        200,
                        {
                            "attempts": get_ledger().list_contact_attempts(
                                account_id, _one(query, "decision")
                            )
                        },
                    )
                else:
                    self.send_error(405)
                return

        self.send_error(404)

    # -- 传输辅助 ------------------------------------------------------------

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def _one(query, key):
    values = query.get(key)
    return values[0] if values else None


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 让 --check 同时覆盖账本关键路径，避免带病启动。
        ledger = get_ledger()
        ledger.recover()
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
