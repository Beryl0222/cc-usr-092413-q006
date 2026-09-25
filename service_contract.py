"""验证基础服务与决策链 HTTP 接口的合同。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, SERVICE_NAME, health_payload


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = Request(f"{self.base_url}{path}", data=data, method=method,
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=2) as response:
            return response.status, json.load(response)

    def request_error(self, method, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = Request(f"{self.base_url}{path}", data=data, method=method,
                      headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(req, timeout=2)
        body = json.load(error.exception)
        return error.exception.code, body

    def test_health_payload_has_stable_identity(self):
        self.assertEqual(
            health_payload(),
            {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME},
        )

    def test_health_endpoint_returns_json(self):
        with urlopen(f"{self.base_url}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            self.assertEqual(json.load(response), health_payload())

    def test_unknown_route_is_not_exposed(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_domain_violation_returns_400_with_message(self):
        status, body = self.request_error("POST", "/decisions", {
            "case_id": "nope", "by": "x", "effective_date": "2026-03-01",
        })
        self.assertEqual(status, 400)
        self.assertIn("案件不存在", body["error"])

    def test_invalid_json_returns_400(self):
        req = Request(f"{self.base_url}/cases", data=b"{bad", method="POST",
                      headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(req, timeout=2)
        self.assertEqual(error.exception.code, 400)

    def test_full_retirement_decision_chain_over_http(self):
        # 建案
        _, case = self.request("POST", "/cases", {"facility_id": "HTTP-F1"})
        cid = case["case_id"]

        # 资金、安装、维修、安全、覆盖证据；故意不提交使用证据
        for record in [
            {"type": "funding", "date": "2024-01-10", "source": "体彩公益金",
             "amount": 50000},
            {"type": "installation", "date": "2024-02-01", "batch": "2024-A"},
            {"type": "maintenance", "date": "2025-03-01", "work_order_id": "WO-9",
             "cost": 3000, "status": "closed"},
            {"type": "maintenance", "date": "2025-07-01", "work_order_id": "WO-OPEN",
             "cost": 0, "status": "open"},
            {"type": "safety", "date": "2025-04-01", "incident_id": "INC-9",
             "severity": 2},
            {"type": "coverage", "date": "2025-06-01", "gap_level": 0.8},
        ]:
            status, _ = self.request("POST", f"/cases/{cid}/evidence", record)
            self.assertEqual(status, 201)

        # 缺测不得解释为无人使用
        _, timeline = self.request("GET", f"/cases/{cid}/timeline")
        self.assertEqual(timeline["usage_summary"]["evidence_status"], "no_evidence")

        # 运营提案（退役 + 替换）
        _, proposal = self.request("POST", "/proposals", {
            "case_id": cid, "option": "retire", "by": "运营单位",
            "at": "2026-01-10", "rationale": "零件停产长期停用",
            "service_plan": "邻近点位补位",
            "qrcode_disposition": {"action": "retarget", "target_asset_id": "AST-HTTP"},
            "old_asset_disposition": "拆除回收",
            "replacements": [{
                "new_asset_id": "AST-HTTP", "requested_amount": 60000,
                "site_obligations": ["场地地面管护"],
                "cached_notices": [{"notice_id": "N-1"}],
                "warranties": [{"warranty": "两年保修", "until": "2028-01-01"}],
            }],
        })
        self.assertTrue(proposal["proposal_id"])

        # 社区越权签署资金事项被拒
        status, body = self.request_error("POST", "/signoffs", {
            "case_id": cid, "kind": "community", "by": "丁", "at": "2026-01-14",
            "service_gap_opinion": "有缺口", "unamortized_amount": 0,
        })
        self.assertEqual(status, 400)
        self.assertIn("服务缺口", body["error"])

        # 三方会签齐备
        for signoff in [
            {"kind": "finance", "by": "财务", "at": "2026-01-12",
             "unamortized_amount": 9000, "disposition": "核减"},
            {"kind": "safety", "by": "安全负责人", "at": "2026-01-13",
             "risk_confirmed": True},
            {"kind": "community", "by": "社区代表", "at": "2026-01-14",
             "service_gap_opinion": {"gap_level": 0.8}},
        ]:
            self.request("POST", "/signoffs", {"case_id": cid, **signoff})

        # 形成带生效日的决定
        _, decision = self.request("POST", "/decisions", {
            "case_id": cid, "by": "主管部门", "at": "2026-02-01",
            "effective_date": "2026-03-01",
        })
        self.assertEqual(decision["effective_date"], "2026-03-01")

        # 退役：关未来工单、保历史
        _, retired = self.request("POST", "/retirements", {
            "case_id": cid, "at": "2026-03-02", "receipt_id": "RC-HTTP-1",
        })
        self.assertIn("WO-OPEN", retired["closed_future_work_orders"])
        self.assertEqual(retired["history_retained"]["maintenance_spending"], 3000)

        # 重复回执
        _, duplicate = self.request("POST", "/retirements", {
            "case_id": cid, "at": "2026-03-03", "receipt_id": "RC-HTTP-1",
        })
        self.assertTrue(duplicate["duplicate_receipt"])

        # 替换设备原子交接
        _, handover = self.request("POST", "/handovers", {
            "case_id": cid, "at": "2026-03-02", "receipt_id": "RC-HTTP-2",
        })
        _, asset = self.request("GET", "/assets/AST-HTTP")
        self.assertEqual(asset["cached_notices"][0]["retarget_to"], "AST-HTTP")
        self.assertEqual(asset["maintenance_records"], [])
        self.assertIn("WO-9", handover["handovers"][0]["excluded_old_faults"])

        # 资金追踪：为何停止 + 服务补位
        _, trace = self.request("GET", "/facilities/HTTP-F1/funding-trace")
        self.assertFalse(trace["funds"][0]["continues"])
        self.assertEqual(trace["funds"][0]["stops_on"], "2026-03-01")
        self.assertTrue(trace["service_backfill"]["covered"])


if __name__ == "__main__":
    unittest.main()
