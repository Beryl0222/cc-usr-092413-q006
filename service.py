"""公共健身资产问效的运行入口。

除健康检查外，本服务暴露健身设施退役与替换决策链的 JSON 接口，
领域规则全部封装在 :mod:`domain` 中。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from domain import DecisionStore, DomainError

SERVICE_ID = "public-fitness-accountability"
SERVICE_NAME = "公共健身资产问效"

# 单进程内存态存储；线程安全由 DecisionStore 内部锁保证
STORE = DecisionStore()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# 动作型 POST 路由：路径 -> store 方法名
POST_ACTIONS = {
    "/proposals": "add_proposal",
    "/signoffs": "add_signoff",
    "/decisions": "decide",
    "/decisions/withdraw": "withdraw",
    "/phases/plan": "plan_phases",
    "/phases/complete": "complete_phase",
    "/retirements": "execute_retirement",
    "/handovers": "execute_handover",
    "/budget/order": "budget_order",
    "/budget/adjust": "adjust_order",
}


class Handler(BaseHTTPRequestHandler):
    """提供健康检查、决策链查询接口与动作接口。"""

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/health":
                self._write_json(200, health_payload())
                return
            if path.startswith("/cases/") and path.endswith("/timeline"):
                case_id = path[len("/cases/"):-len("/timeline")]
                self._write_json(200, STORE.timeline_view(case_id))
                return
            if path.startswith("/cases/"):
                self._write_json(200, STORE.case_view(path[len("/cases/"):]))
                return
            if path.startswith("/facilities/") and path.endswith("/funding-trace"):
                facility_id = path[len("/facilities/"):-len("/funding-trace")]
                self._write_json(200, STORE.funding_trace(facility_id))
                return
            if path.startswith("/assets/"):
                self._write_json(200, STORE.asset_view(path[len("/assets/"):]))
                return
            self.send_error(404)
        except DomainError as error:
            self._write_json(400, {"error": str(error)})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            payload = self._read_json()
            if path == "/cases":
                self._write_json(201, STORE.create_case(
                    payload.get("facility_id", "").strip()
                ))
                return
            if path.startswith("/cases/") and path.endswith("/evidence"):
                case_id = path[len("/cases/"):-len("/evidence")]
                self._write_json(201, STORE.ingest(case_id, payload))
                return
            if path.startswith("/assets/") and path.endswith("/records"):
                asset_id = path[len("/assets/"):-len("/records")]
                self._write_json(201, STORE.record_asset_incident(asset_id, payload))
                return
            method_name = POST_ACTIONS.get(path)
            if method_name:
                self._write_json(200, getattr(STORE, method_name)(payload))
                return
            self.send_error(404)
        except DomainError as error:
            self._write_json(400, {"error": str(error)})
        except (json.JSONDecodeError, TypeError) as error:
            self._write_json(400, {"error": f"请求体不是有效 JSON: {error}"})

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
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


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert DecisionStore is not None
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
