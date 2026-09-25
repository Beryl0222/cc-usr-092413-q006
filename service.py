"""公共健身资产问效的运行入口。

除健康检查外，提供退役与替换决策链的 HTTP 接口：

    POST /api/receipts                 现场回执幂等录入
    GET  /api/facilities/<id>/timeline 证据时间汇总
    POST /api/decisions                创建决定（运营方案动作）
    POST /api/decisions/<id>/sign      四方门控签署
    POST /api/decisions/<id>/finalize  定稿（带生效日）
    POST /api/decisions/<id>/execute   执行（支持分期 steps）
    POST /api/decisions/<id>/withdraw  撤回
    POST /api/decisions/<id>/restore   撤回后恢复
    GET  /api/decisions                决定列表
    POST /api/budget/rankings          公共价值规则排序
    POST /api/budget/rankings/<id>/adjust 人工调整顺序（须说明理由）
    GET  /api/funding/<id>/trace       资金追踪
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from decision_chain import DecisionChainApp, DecisionError, ConflictError

SERVICE_ID = "public-fitness-accountability"
SERVICE_NAME = "公共健身资产问效"

# 单进程共享一份决策链状态（内存存储，用于本地联调）。
CHAIN = DecisionChainApp()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与决策链领域接口。"""

    chain = CHAIN  # 类属性，测试可用子类替换以隔离状态

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/health":
                self._write_json(200, health_payload())
                return
            if parsed.path == "/api/decisions":
                self._write_json(200, {"decisions": self.chain.gates.list_decisions()})
                return
            if self._match(parsed.path, "/api/facilities/", "/timeline", 5):
                facility_id = parsed.path.split("/")[3]
                query = parse_qs(parsed.query)
                self._write_json(200, self.chain.evidence.facility_timeline(
                    facility_id,
                    window_start=query.get("start", [None])[0],
                    window_end=query.get("end", [None])[0],
                ))
                return
            if self._match(parsed.path, "/api/funding/", "/trace", 5):
                funding_id = parsed.path.split("/")[3]
                query = parse_qs(parsed.query)
                self._write_json(200, self.chain.funding_trace.trace_funding(
                    funding_id, as_of=query.get("as_of", [None])[0]
                ))
                return
            self.send_error(404)
        except DecisionError as exc:
            self._write_error(exc)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/receipts":
                result = self.chain.intake.submit_receipt(payload)
                self._write_json(200 if result.get("duplicate") else 201, result)
                return
            if parsed.path == "/api/decisions":
                self._write_json(201, self.chain.gates.create_decision(payload))
                return
            if self._match(parsed.path, "/api/decisions/", "/sign", 5):
                decision_id = parsed.path.split("/")[3]
                self._write_json(200, self.chain.gates.sign(decision_id, payload))
                return
            if self._match(parsed.path, "/api/decisions/", "/finalize", 5):
                decision_id = parsed.path.split("/")[3]
                self._write_json(200, self.chain.gates.finalize(decision_id, payload))
                return
            if self._match(parsed.path, "/api/decisions/", "/execute", 5):
                decision_id = parsed.path.split("/")[3]
                self._write_json(200, self.chain.execution.execute(decision_id, payload))
                return
            if self._match(parsed.path, "/api/decisions/", "/withdraw", 5):
                decision_id = parsed.path.split("/")[3]
                self._write_json(200, self.chain.gates.withdraw(decision_id, payload))
                return
            if self._match(parsed.path, "/api/decisions/", "/restore", 5):
                decision_id = parsed.path.split("/")[3]
                self._write_json(200, self.chain.gates.restore(decision_id))
                return
            if parsed.path == "/api/budget/rankings":
                self._write_json(201, self.chain.budget.rank_candidates(payload))
                return
            if self._match(parsed.path, "/api/budget/rankings/", "/adjust", 6):
                ranking_id = parsed.path.split("/")[5]
                self._write_json(200, self.chain.budget.adjust_ranking(
                    ranking_id, payload
                ))
                return
            self.send_error(404)
        except DecisionError as exc:
            self._write_error(exc)

    # -- 工具 -------------------------------------------------------------

    @staticmethod
    def _match(path, prefix, suffix, parts):
        segments = path.split("/")
        return (
            path.startswith(prefix)
            and path.endswith(suffix)
            and len(segments) == parts
            and all(segments[i] for i in range(1, parts - 1))
        )

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DecisionError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(payload, dict):
            raise DecisionError("请求体必须是 JSON 对象")
        return payload

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_error(self, exc):
        status = 409 if isinstance(exc, ConflictError) else 400
        self._write_json(status, {"error": type(exc).__name__, "message": str(exc)})

    def log_message(self, *_args):
        return


def make_handler(chain):
    """构造绑定指定决策链实例的 handler 类（测试隔离用）。"""

    class BoundHandler(Handler):
        pass

    BoundHandler.chain = chain
    BoundHandler.__name__ = "Handler"
    return BoundHandler


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
