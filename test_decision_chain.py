"""退役与替换决策链的领域与接口测试。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from decision_chain import (
    ACTION_REPLACE,
    ACTION_RETREAT,
    DecisionChainApp,
    DecisionError,
    ConflictError,
)
from service import make_handler


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------

class ChainTest(unittest.TestCase):
    def setUp(self):
        self.app = DecisionChainApp()

    @property
    def store(self):
        return self.app.store

    def receipt(self, receipt_no, records):
        return self.app.intake.submit_receipt(
            {"receipt_no": receipt_no, "records": records},
            today="2026-09-01",
        )

    def add_facility(self, fid, site="S1", funding_ids=None, installed="2020-03-01",
                     batch="B2020-Q1"):
        funding_ids = funding_ids if funding_ids is not None else [f"fund_{fid}"]
        if funding_ids and not all(f in self.app.store.funding for f in funding_ids):
            # 先补资金
            for f in funding_ids:
                if f not in self.app.store.funding:
                    self.receipt(f"r-fund-{f}", [{
                        "type": "funding", "funding_id": f, "source": "体彩公益金",
                        "amount": 100000, "awarded_on": "2020-01-01",
                        "amortization_years": 10, "facility_id": fid,
                    }])
        self.receipt(f"r-fac-{fid}", [{
            "type": "facility", "facility_id": fid, "name": fid,
            "site_id": site, "funding_ids": funding_ids,
            "install_batch": batch, "installed_on": installed,
        }])

    def usage(self, fid, measured, count=None, start="2026-01-01", end="2026-03-31"):
        rec = {
            "type": "usage_observation", "facility_id": fid,
            "window_start": start, "window_end": end, "measured": measured,
        }
        if measured:
            rec["count"] = count
        self.receipt(f"r-obs-{fid}-{start}-{measured}", [rec])

    def work_order(self, fid, opened, cost, scheduled=None, status="open",
                   reason="维修"):
        result = self.receipt(f"r-wo-{fid}-{opened}-{scheduled}-{cost}", [{
            "type": "work_order", "facility_id": fid, "opened_on": opened,
            "scheduled_for": scheduled, "cost": cost, "reason": reason,
            "status": status,
        }])
        return result["records"][0]["id"]

    def incident(self, fid, on="2026-05-01", severity="low"):
        return self.receipt(f"r-inc-{fid}-{on}-{severity}", [{
            "type": "safety_incident", "facility_id": fid,
            "occurred_on": on, "severity": severity,
            "description": "测试事故",
        }])["records"][0]["id"]

    def coverage(self, fid, residents, on="2026-06-01", site="S1"):
        self.receipt(f"r-cov-{fid}-{on}", [{
            "type": "service_coverage", "facility_id": fid, "site_id": site,
            "on": on, "residents_served": residents,
        }])

    def qr(self, fid, qid=None):
        qid = qid or f"qr-{fid}"
        self.receipt(f"r-qr-{qid}", [{
            "type": "qr_guide", "qr_id": qid, "facility_id": fid,
            "url": f"https://guide.example/{qid}",
        }])

    def warranty(self, fid, transferable=True, until="2027-12-31",
                 provider="厂方"):
        return self.receipt(f"r-war-{fid}-{until}-{transferable}", [{
            "type": "warranty", "facility_id": fid, "provider": provider,
            "covers_until": until, "transferable": transferable,
        }])["records"][0]["id"]

    def promise(self, fid, due="2026-12-31", desc="年度保养承诺"):
        return self.receipt(f"r-prom-{fid}-{due}", [{
            "type": "maintenance_promise", "facility_id": fid,
            "description": desc, "due_on": due,
        }])["records"][0]["id"]

    # -- 门控快捷流程 -----------------------------------------------------

    def _four_signoffs(self, decision_id, action=ACTION_RETREAT,
                       risk_accepted=True, gap_acceptable=True, mitigation=""):
        self.app.gates.sign(decision_id, {
            "role": "operator", "signer": "运营张三", "on": "2026-09-10",
            "action": action, "rationale": "反复维修且零件停产，长期停用",
            "options_reviewed": ["keep", "migrate", "renovate", "retire"],
            "estimated_cost": 5000,
        })
        self.app.gates.sign(decision_id, {
            "role": "finance", "signer": "财务李四", "on": "2026-09-11",
            "unamortized_amount": 32000, "verified": True,
        })
        self.app.gates.sign(decision_id, {
            "role": "safety_officer", "signer": "安全王五", "on": "2026-09-12",
            "risk_level": "medium", "risk_accepted": risk_accepted,
            "residual_measures": ["停用期间围挡"],
        })
        community = {
            "role": "community", "signer": "代表赵六", "on": "2026-09-13",
            "service_gap": "本小区该类器材仅此一处",
            "gap_acceptable": gap_acceptable,
        }
        if mitigation:
            community["mitigation"] = mitigation
        self.app.gates.sign(decision_id, community)


# ---------------------------------------------------------------------------
# 录入与证据汇总
# ---------------------------------------------------------------------------

class IntakeAndTimelineTest(ChainTest):
    def test_duplicate_receipt_is_idempotent(self):
        self.add_facility("F1")
        payload = {
            "receipt_no": "RCP-1",
            "records": [{
                "type": "work_order", "facility_id": "F1",
                "opened_on": "2026-04-01", "cost": 800, "reason": "紧固",
            }],
        }
        first = self.app.intake.submit_receipt(payload, today="2026-09-01")
        second = self.app.intake.submit_receipt(payload, today="2026-09-02")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertNotIn("records", second)
        self.assertEqual(
            len([w for w in self.app.store.work_orders.values()
                 if w["facility_id"] == "F1"]),
            1,
        )

    def test_missing_measurements_not_counted_as_zero_use(self):
        self.add_facility("F1")
        self.usage("F1", measured=False, start="2026-01-01", end="2026-03-31")
        self.usage("F1", measured=True, count=120,
                   start="2026-04-01", end="2026-06-30")
        self.work_order("F1", "2026-02-01", 1500)
        self.incident("F1")
        self.coverage("F1", 300)

        tl = self.app.evidence.facility_timeline("F1")
        usage = tl["usage"]
        self.assertEqual(usage["measured_windows"], 1)
        self.assertEqual(usage["missing_windows"], 1)
        self.assertEqual(usage["zero_use_windows"], 0)
        self.assertEqual(usage["total_uses"], 120)
        self.assertFalse(usage["missing_treated_as_zero"])
        self.assertEqual(usage["evidence_status"], "partial")
        self.assertEqual(tl["maintenance"]["total_cost"], 1500)
        self.assertEqual(tl["maintenance"]["work_order_count"], 1)
        self.assertEqual(len(tl["safety_incidents"]), 1)
        self.assertEqual(tl["service_coverage"]["latest_residents_served"], 300)
        self.assertEqual(tl["funding_sources"][0]["source"], "体彩公益金")
        self.assertEqual(tl["install_batch"], "B2020-Q1")

    def test_all_missing_evidence_is_not_judged_unused(self):
        self.add_facility("F1")
        self.usage("F1", measured=False)
        tl = self.app.evidence.facility_timeline("F1")
        self.assertEqual(tl["usage"]["evidence_status"], "missing")
        self.assertEqual(tl["usage"]["avg_uses_per_measured_window"], None)
        self.assertEqual(tl["usage"]["total_uses"], 0)  # 无数据，而非测得 0
        self.assertEqual(tl["usage"]["zero_use_windows"], 0)

    def test_zero_use_window_is_distinct_from_missing(self):
        self.add_facility("F1")
        self.usage("F1", measured=True, count=0)
        tl = self.app.evidence.facility_timeline("F1")
        self.assertEqual(tl["usage"]["evidence_status"], "complete")
        self.assertEqual(tl["usage"]["zero_use_windows"], 1)
        self.assertEqual(tl["usage"]["missing_windows"], 0)

    def test_timeline_window_filter(self):
        self.add_facility("F1")
        self.work_order("F1", "2025-01-01", 100)
        self.work_order("F1", "2026-05-01", 900)
        tl = self.app.evidence.facility_timeline(
            "F1", window_start="2026-01-01", window_end="2026-12-31"
        )
        self.assertEqual(tl["maintenance"]["work_order_count"], 1)
        self.assertEqual(tl["maintenance"]["total_cost"], 900)


# ---------------------------------------------------------------------------
# 门控
# ---------------------------------------------------------------------------

class GateTest(ChainTest):
    def test_cannot_finalize_without_all_four_gates(self):
        self.add_facility("F1")
        d = self.app.gates.create_decision(
            {"facility_id": "F1", "action": ACTION_RETREAT}
        )
        self.app.gates.sign(d["decision_id"], {
            "role": "operator", "signer": "甲", "on": "2026-09-10",
            "action": ACTION_RETREAT, "rationale": "理由充分具体",
            "options_reviewed": ["keep"], "estimated_cost": 1000,
        })
        with self.assertRaisesRegex(ConflictError, "finance"):
            self.app.gates.finalize(
                d["decision_id"], {"effective_on": "2026-10-01"}
            )

    def test_community_scope_is_service_gap_only(self):
        self.add_facility("F1")
        d = self.app.gates.create_decision(
            {"facility_id": "F1", "action": ACTION_RETREAT}
        )
        self.app.gates.sign(d["decision_id"], {
            "role": "community", "signer": "代表", "on": "2026-09-13",
            "service_gap": "无替代器材", "gap_acceptable": False,
            "mitigation": "就近迁移一台",
        })
        view = self.app.gates.get(d["decision_id"])
        self.assertEqual(
            view["signoffs"]["community"]["scope"], "service_gap_only"
        )

    def test_unknown_role_rejected(self):
        self.add_facility("F1")
        d = self.app.gates.create_decision(
            {"facility_id": "F1", "action": ACTION_RETREAT}
        )
        with self.assertRaisesRegex(DecisionError, "未知签署角色"):
            self.app.gates.sign(d["decision_id"], {
                "role": "mayor", "signer": "x",
            })

    def test_operator_action_must_match_decision(self):
        self.add_facility("F1")
        d = self.app.gates.create_decision(
            {"facility_id": "F1", "action": ACTION_RETREAT}
        )
        with self.assertRaisesRegex(DecisionError, "不一致"):
            self.app.gates.sign(d["decision_id"], {
                "role": "operator", "signer": "甲", "on": "2026-09-10",
                "action": ACTION_REPLACE, "rationale": "x" * 6,
                "options_reviewed": [], "estimated_cost": 1,
            })

    def test_unaccepted_safety_risk_blocks_finalize(self):
        self.add_facility("F1")
        d = self.app.gates.create_decision(
            {"facility_id": "F1", "action": ACTION_RETREAT}
        )
        self._four_signoffs(d["decision_id"], risk_accepted=False)
        with self.assertRaisesRegex(ConflictError, "安全负责人未接受风险"):
            self.app.gates.finalize(
                d["decision_id"], {"effective_on": "2026-10-01"}
            )

    def test_unacceptable_gap_without_mitigation_blocks_finalize(self):
        self.add_facility("F1")
        d = self.app.gates.create_decision(
            {"facility_id": "F1", "action": ACTION_RETREAT}
        )
        self._four_signoffs(d["decision_id"], gap_acceptable=False, mitigation="")
        with self.assertRaisesRegex(ConflictError, "服务缺口"):
            self.app.gates.finalize(
                d["decision_id"], {"effective_on": "2026-10-01"}
            )

    def test_full_gates_finalize_with_effective_date_and_is_immutable(self):
        self.add_facility("F1")
        d = self.app.gates.create_decision(
            {"facility_id": "F1", "action": ACTION_RETREAT}
        )
        self._four_signoffs(d["decision_id"], mitigation="迁移补位")
        finalized = self.app.gates.finalize(
            d["decision_id"], {"effective_on": "2026-10-01"}
        )
        self.assertEqual(finalized["status"], "finalized")
        self.assertEqual(finalized["effective_on"], "2026-10-01")
        with self.assertRaises(ConflictError):
            self.app.gates.finalize(
                d["decision_id"], {"effective_on": "2026-10-02"}
            )

    def test_no_two_active_decisions_for_same_facility(self):
        self.add_facility("F1")
        self.app.gates.create_decision(
            {"facility_id": "F1", "action": ACTION_RETREAT}
        )
        with self.assertRaises(ConflictError):
            self.app.gates.create_decision(
                {"facility_id": "F1", "action": "keep"}
            )


# ---------------------------------------------------------------------------
# 退役执行
# ---------------------------------------------------------------------------

class RetirementExecutionTest(ChainTest):
    def _finalized_retirement(self, fid):
        d = self.app.gates.create_decision(
            {"facility_id": fid, "action": ACTION_RETREAT}
        )
        self._four_signoffs(d["decision_id"], mitigation="同场地保留同类器材")
        self.app.gates.finalize(d["decision_id"],
                                {"effective_on": "2026-10-01"})
        return d["decision_id"]

    def test_retire_cancels_future_work_orders_but_keeps_past_and_history(self):
        self.add_facility("F1")
        past_wo = self.work_order("F1", "2026-09-01", 300, status="closed")
        future_wo = self.work_order("F1", "2026-09-20", 900,
                                    scheduled="2026-10-05")
        inflight = self.work_order("F1", "2026-09-15", 100)
        inc_id = self.incident("F1", severity="high")
        self.qr("F1")
        pid = self.promise("F1")

        did = self._finalized_retirement("F1")
        result = self.app.execution.execute(did, {"on": "2026-10-01"})

        self.assertEqual(result["execution_state"], "done")
        self.assertEqual(self.store.work_orders[future_wo]["status"], "cancelled")
        # 已关闭的历史工单保留原样；在保抢修（早于生效日、无未来排期）继续完成
        self.assertEqual(self.store.work_orders[past_wo]["status"], "closed")
        self.assertEqual(self.store.work_orders[inflight]["status"], "open")

        # 事故历史不删除、可追溯
        incident = self.store.safety_incidents[inc_id]
        self.assertTrue(incident["preserved"])
        decision = self.app.gates.get(did)
        self.assertIn(inc_id,
                      decision["execution"]["preserved"]["incident_ids"])
        self.assertEqual(
            decision["execution"]["preserved"]["total_historical_spending"],
            1300,
        )
        # 二维码指导退役；未结维保承诺已兑现，不悬空
        self.assertEqual(self.store.qr_guides["qr-F1"]["status"], "decommissioned")
        self.assertEqual(self.store.maintenance_promises[pid]["status"], "honored")
        self.assertEqual(self.store.facilities["F1"]["status"], "retired")

    def _steps(self, did):
        return self.app.gates.get(did)["execution"]["steps"]

    def test_execute_before_effective_date_rejected(self):
        self.add_facility("F1")
        did = self._finalized_retirement("F1")
        with self.assertRaisesRegex(ConflictError, "早于生效日"):
            self.app.execution.execute(did, {"on": "2026-09-30"})

    def test_execute_is_idempotent(self):
        self.add_facility("F1")
        did = self._finalized_retirement("F1")
        first = self.app.execution.execute(did, {"on": "2026-10-01"})
        second = self.app.execution.execute(did, {"on": "2026-10-02"})
        self.assertEqual(len(first["steps"]), len(second["steps"]))
        self.assertEqual(second["execution_state"], "done")


# ---------------------------------------------------------------------------
# 替换执行：原子承接与故障隔离
# ---------------------------------------------------------------------------

class ReplacementExecutionTest(ChainTest):
    def _setup_pair(self):
        self.add_facility("OLD", site="S1")
        self.add_facility("NEW", site="S1", funding_ids=["fund_NEW"])
        self.coverage("OLD", 400)
        self.qr("OLD", qid="qr-OLD")
        self.warranty("OLD", transferable=True, until="2027-12-31")
        self.warranty("OLD", transferable=False, until="2027-12-31",
                      provider="专属延保")
        self.warranty("OLD", transferable=True, until="2025-12-31",
                      provider="已过期保")
        self.promise("OLD", desc="免费年检两次")
        self.work_order("OLD", "2026-09-20", 900, scheduled="2026-10-05")
        self.incident("OLD", severity="high")

    def _finalized_replacement(self):
        d = self.app.gates.create_decision({
            "facility_id": "OLD", "action": ACTION_REPLACE,
            "replacement_facility_id": "NEW",
        })
        self._four_signoffs(d["decision_id"], action=ACTION_REPLACE)
        self.app.gates.finalize(d["decision_id"],
                                {"effective_on": "2026-10-01"})
        return d["decision_id"]

    def test_replacement_atomically_assumes_obligations_and_isolates_faults(self):
        self._setup_pair()
        did = self._finalized_replacement()
        result = self.app.execution.execute(did, {"on": "2026-10-01"})
        self.assertEqual(result["execution_state"], "done")

        old = self.store.facilities["OLD"]
        new = self.store.facilities["NEW"]
        self.assertEqual(old["status"], "retired")
        self.assertEqual(old["superseded_by"], "NEW")
        self.assertEqual(new["supersedes"], "OLD")

        # 场地服务义务由新资产原子承接
        coverage = [c for c in self.store.service_coverage.values()
                    if c["facility_id"] == "NEW"]
        self.assertEqual(len(coverage), 1)
        self.assertEqual(coverage[0]["assumed_from"], "OLD")
        self.assertEqual(coverage[0]["residents_served"], 400)

        # 仅适用（可转移且未过期）的保修责任转移
        warranties_on_new = [
            w for w in self.store.warranties.values()
            if w["facility_id"] == "NEW"
        ]
        self.assertEqual(len(warranties_on_new), 1)
        self.assertEqual(warranties_on_new[0]["provider"], "厂方")

        # 未结维保承诺随场地义务转移
        promises_on_new = [
            p for p in self.store.maintenance_promises.values()
            if p["facility_id"] == "NEW"
        ]
        self.assertEqual(len(promises_on_new), 1)
        self.assertEqual(promises_on_new[0]["status"], "transferred")

        # 旧二维码扫码得到替换通知缓存，并指向新资产
        self.assertEqual(self.store.qr_guides["qr-OLD"]["status"], "redirected")
        self.assertEqual(
            self.store.qr_guides["qr-OLD"]["redirected_to_facility"], "NEW"
        )
        self.assertIn("qr-OLD", result["notice_cache"])
        notice = result["notice_cache"]["qr-OLD"]
        self.assertEqual(notice["replacement_facility_id"], "NEW")

        # 未来工单随旧资产关闭，不转移
        self.assertTrue(all(
            w["facility_id"] == "OLD"
            for w in self.store.work_orders.values()
        ))
        cancelled = [w for w in self.store.work_orders.values()
                     if w["status"] == "cancelled"]
        self.assertEqual(len(cancelled), 1)

        # 事故历史保留在旧资产，旧故障不算到新资产
        self.assertTrue(all(
            i["facility_id"] == "OLD"
            for i in self.store.safety_incidents.values()
        ))
        decision = self.app.gates.get(did)
        self.assertEqual(
            decision["execution"]["fault_isolation_verified"]
            ["new_incidents_originating_from_old"],
            0,
        )

    def test_cross_site_replacement_is_rejected_before_any_change(self):
        self._setup_pair()
        # 把 NEW 改到另一场地：整笔事务必须无任何副作用
        self.store.facilities["NEW"]["site_id"] = "S2"
        did = self._finalized_replacement()
        with self.assertRaises(DecisionError):
            self.app.execution.execute(did, {"on": "2026-10-01"})
        self.assertEqual(self.store.facilities["OLD"]["status"], "active")
        self.assertEqual(
            [c for c in self.store.service_coverage.values()
             if c["facility_id"] == "NEW"],
            [],
        )
        self.assertEqual(
            [p for p in self.store.maintenance_promises.values()
             if p["facility_id"] == "NEW"],
            [],
        )
        self.assertEqual(
            [w for w in self.store.work_orders.values()
             if w["status"] == "cancelled"],
            [],
        )

    def test_phased_execution_and_resume_after_withdraw(self):
        self._setup_pair()
        did = self._finalized_replacement()

        partial = self.app.execution.execute(did, {
            "on": "2026-10-01",
            "steps": ["transfer_site_obligations",
                      "transfer_applicable_warranties"],
        })
        self.assertEqual(partial["execution_state"], "partial")
        self.assertEqual(self.store.facilities["OLD"]["status"], "active")

        # 分期期间撤回：执行被阻止
        self.app.gates.withdraw(did, {"reason": "需要补充居民听证"},
                                today="2026-10-02")
        with self.assertRaises(ConflictError):
            self.app.execution.execute(did, {"on": "2026-10-03"})

        # 恢复后从中断处继续，已完成步骤不重复
        self.app.gates.restore(did, today="2026-10-04")
        done = self.app.execution.execute(did, {"on": "2026-10-04"})
        self.assertEqual(done["execution_state"], "done")
        self.assertEqual(self.store.facilities["OLD"]["status"], "retired")
        step_names = [s["step"] for s in done["steps"]]
        self.assertEqual(len(step_names), 5)

    def test_fully_executed_decision_cannot_be_withdrawn(self):
        self._setup_pair()
        did = self._finalized_replacement()
        self.app.execution.execute(did, {"on": "2026-10-01"})
        with self.assertRaisesRegex(ConflictError, "不可撤回"):
            self.app.gates.withdraw(did, {"reason": "反悔"})


# ---------------------------------------------------------------------------
# 预算排序
# ---------------------------------------------------------------------------

class BudgetRankingTest(ChainTest):
    def _high_value_facility(self, fid):
        self.add_facility(fid)
        self.usage(fid, measured=True, count=200)
        self.coverage(fid, 500)
        for i in range(3):
            self.incident(fid, on=f"2026-0{i+1}-01", severity="high")
            self.work_order(fid, f"2026-0{i+1}-10", 7000)

    def test_ranking_follows_public_value_rule(self):
        self._high_value_facility("HV")
        self.add_facility("LV")
        self.usage("LV", measured=True, count=5)
        self.coverage("LV", 10)
        self.work_order("LV", "2026-05-01", 200)

        ranking = self.app.budget.rank_candidates({
            "facility_ids": ["LV", "HV"],
            "budget": 100000,
            "proposals": {
                "HV": {"estimated_cost": 30000, "proposed_action": "replace"},
                "LV": {"estimated_cost": 8000, "proposed_action": "retire"},
            },
            "on": "2026-09-20",
        })
        self.assertEqual(ranking["rule_version"], "public-value-v1")
        order = [c["facility_id"] for c in ranking["candidates"]]
        self.assertEqual(order, ["HV", "LV"])
        self.assertGreater(
            ranking["candidates"][0]["public_value_score"],
            ranking["candidates"][1]["public_value_score"],
        )

    def test_missing_evidence_does_not_jump_queue(self):
        self._high_value_facility("HV")
        self.add_facility("UNK")  # 无任何使用观测
        ranking = self.app.budget.rank_candidates({
            "facility_ids": ["HV", "UNK"],
            "budget": 100000,
            "proposals": {
                "HV": {"estimated_cost": 30000},
                "UNK": {"estimated_cost": 1000},
            },
        })
        self.assertEqual(
            [c["facility_id"] for c in ranking["candidates"]], ["HV", "UNK"]
        )
        self.assertEqual(ranking["candidates"][1]["evidence_status"], "missing")

    def test_budget_shortfall_selects_in_rank_order(self):
        self._high_value_facility("HV")
        self.add_facility("LV")
        self.usage("LV", measured=True, count=10)
        ranking = self.app.budget.rank_candidates({
            "facility_ids": ["LV", "HV"],
            "budget": 20000,
            "proposals": {
                "HV": {"estimated_cost": 20000},
                "LV": {"estimated_cost": 5000},
            },
        })
        self.assertEqual(ranking["funded"], ["HV"])
        self.assertFalse(ranking["candidates"][1]["funded"])

    def test_manual_adjustment_requires_reason_and_is_audited(self):
        self._high_value_facility("HV")
        self.add_facility("LV")
        self.usage("LV", measured=True, count=10)
        ranking = self.app.budget.rank_candidates({
            "facility_ids": ["HV", "LV"], "budget": 100000,
            "proposals": {
                "HV": {"estimated_cost": 20000},
                "LV": {"estimated_cost": 5000},
            },
        })
        with self.assertRaisesRegex(DecisionError, "理由"):
            self.app.budget.adjust_ranking(ranking["ranking_id"], {
                "facility_id": "LV", "new_position": 0, "reason": "x",
                "by": "审批人",
            })
        adjusted = self.app.budget.adjust_ranking(ranking["ranking_id"], {
            "facility_id": "LV", "new_position": 0,
            "reason": "LV 点毗邻即将入住的安置房，优先补位",
            "by": "审批人", "on": "2026-09-21",
        })
        self.assertEqual(
            [c["facility_id"] for c in adjusted["candidates"]], ["LV", "HV"]
        )
        self.assertEqual(len(adjusted["adjustments"]), 1)
        self.assertEqual(adjusted["adjustments"][0]["from_position"], 1)
        self.assertEqual(adjusted["adjustments"][0]["to_position"], 0)

    def test_manual_adjustment_recomputes_funding(self):
        self._high_value_facility("HV")
        self.add_facility("LV")
        self.usage("LV", measured=True, count=10)
        ranking = self.app.budget.rank_candidates({
            "facility_ids": ["HV", "LV"], "budget": 22000,
            "proposals": {
                "HV": {"estimated_cost": 20000},
                "LV": {"estimated_cost": 5000},
            },
        })
        self.assertEqual(ranking["funded"], ["HV"])
        adjusted = self.app.budget.adjust_ranking(ranking["ranking_id"], {
            "facility_id": "LV", "new_position": 0,
            "reason": "安置房入住优先保障器材服务",
            "by": "审批人",
        })
        self.assertEqual(adjusted["funded"], ["LV"])


# ---------------------------------------------------------------------------
# 资金追踪
# ---------------------------------------------------------------------------

class FundingTraceTest(ChainTest):
    def test_explains_continuation_before_decision(self):
        self.add_facility("F1")
        self.work_order("F1", "2026-08-01", 1200)
        trace = self.app.funding_trace.trace_funding(
            "fund_F1", as_of="2026-09-01"
        )
        self.assertTrue(trace["continuation"]["still_investing"])
        self.assertIsNone(trace["continuation"]["stop_on"])
        self.assertTrue(any("工单" in r for r in trace["continuation"]["why_continue"]))
        self.assertEqual(trace["repair_spending_to_date"], 1200)
        self.assertGreater(trace["amortization"]["unamortized"], 0)

    def test_stop_and_service_backfill_on_replacement(self):
        self.add_facility("OLD", site="S1")
        self.add_facility("NEW", site="S1", funding_ids=["fund_NEW"])
        self.coverage("OLD", 400)
        d = self.app.gates.create_decision({
            "facility_id": "OLD", "action": ACTION_REPLACE,
            "replacement_facility_id": "NEW",
        })
        self._four_signoffs(d["decision_id"], action=ACTION_REPLACE)
        self.app.gates.finalize(d["decision_id"],
                                {"effective_on": "2026-10-01"})
        self.app.execution.execute(d["decision_id"], {"on": "2026-10-01"})

        trace = self.app.funding_trace.trace_funding(
            "fund_OLD", as_of="2026-11-01"
        )
        self.assertFalse(trace["continuation"]["still_investing"])
        self.assertEqual(trace["continuation"]["stop_on"], "2026-10-01")
        self.assertEqual(
            trace["continuation"]["carried_to_facility_id"], "NEW"
        )
        self.assertTrue(trace["service_backfill"]["backfilled"])
        self.assertEqual(
            trace["service_backfill"]["means"],
            "replacement_assumed_site_obligation",
        )

    def test_retire_without_alternative_shows_service_gap(self):
        self.add_facility("LONER", site="S9")
        d = self.app.gates.create_decision(
            {"facility_id": "LONER", "action": ACTION_RETREAT}
        )
        self._four_signoffs(d["decision_id"],
                            gap_acceptable=True, mitigation="")
        self.app.gates.finalize(d["decision_id"],
                                {"effective_on": "2026-10-01"})
        self.app.execution.execute(d["decision_id"], {"on": "2026-10-01"})
        trace = self.app.funding_trace.trace_funding(
            "fund_LONER", as_of="2026-11-01"
        )
        self.assertEqual(trace["continuation"]["stop_on"], "2026-10-01")
        self.assertFalse(trace["service_backfill"]["backfilled"])
        self.assertEqual(trace["service_backfill"]["means"], "none")


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------

class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = DecisionChainApp()
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(cls.app)
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _request(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = Request(self.base + path, data=data, method=method,
                      headers={"Content-Type": "application/json"})
        return urlopen(req, timeout=3)

    def _post(self, path, payload):
        with self._request("POST", path, payload) as resp:
            return resp.status, json.load(resp)

    def _get(self, path):
        with self._request("GET", path) as resp:
            return resp.status, json.load(resp)

    def test_full_decision_flow_over_http(self):
        # 录入设施与资金（同一回执）
        status, receipt = self._post("/api/receipts", {
            "receipt_no": "HTTP-1",
            "records": [
                {"type": "funding", "funding_id": "hf", "source": "公益金",
                 "amount": 80000, "awarded_on": "2021-01-01",
                 "amortization_years": 8, "facility_id": "HF"},
                {"type": "facility", "facility_id": "HF", "site_id": "S1",
                 "funding_ids": ["hf"], "install_batch": "B21",
                 "installed_on": "2021-03-01"},
                {"type": "usage_observation", "facility_id": "HF",
                 "window_start": "2026-01-01", "window_end": "2026-06-30",
                 "measured": False},
            ],
        })
        self.assertEqual(status, 201)
        # 重放回执不重复处置
        status2, receipt2 = self._post("/api/receipts", {
            "receipt_no": "HTTP-1", "records": [],
        })
        self.assertEqual(status2, 200)
        self.assertTrue(receipt2["duplicate"])

        _, timeline = self._get("/api/facilities/HF/timeline")
        self.assertEqual(timeline["usage"]["missing_windows"], 1)
        self.assertFalse(timeline["usage"]["missing_treated_as_zero"])

        _, decision = self._post("/api/decisions",
                                 {"facility_id": "HF", "action": "retire"})
        did = decision["decision_id"]
        signoffs = [
            {"role": "operator", "signer": "运营", "on": "2026-09-10",
             "action": "retire", "rationale": "反复维修利用率下降且零件停产",
             "options_reviewed": ["keep", "migrate"], "estimated_cost": 4000},
            {"role": "finance", "signer": "财务", "on": "2026-09-11",
             "unamortized_amount": 20000},
            {"role": "safety_officer", "signer": "安全", "on": "2026-09-12",
             "risk_level": "low", "risk_accepted": True},
            {"role": "community", "signer": "社区", "on": "2026-09-13",
             "service_gap": "有邻近点位", "gap_acceptable": True},
        ]
        for s in signoffs:
            self._post(f"/api/decisions/{did}/sign", s)
        _, finalized = self._post(f"/api/decisions/{did}/finalize",
                                  {"effective_on": "2026-10-01"})
        self.assertEqual(finalized["status"], "finalized")

        _, executed = self._post(f"/api/decisions/{did}/execute",
                                 {"on": "2026-10-01"})
        self.assertEqual(executed["execution_state"], "done")

        _, listing = self._get("/api/decisions")
        hf_decisions = [d for d in listing["decisions"]
                        if d["facility_id"] == "HF"]
        self.assertEqual(len(hf_decisions), 1)
        self.assertEqual(hf_decisions[0]["status"], "executed")

        _, trace = self._get("/api/funding/hf/trace?as_of=2026-11-01")
        self.assertEqual(trace["continuation"]["stop_on"], "2026-10-01")

    def test_conflict_returns_409(self):
        self._post("/api/receipts", {
            "receipt_no": "HTTP-2",
            "records": [
                {"type": "funding", "funding_id": "cf", "source": "公益金",
                 "amount": 1, "awarded_on": "2021-01-01",
                 "amortization_years": 8, "facility_id": "CF"},
                {"type": "facility", "facility_id": "CF", "site_id": "S1",
                 "funding_ids": ["cf"], "install_batch": "B21",
                 "installed_on": "2021-03-01"},
            ],
        })
        self._post("/api/decisions",
                   {"facility_id": "CF", "action": "retire"})
        try:
            self._post("/api/decisions",
                       {"facility_id": "CF", "action": "keep"})
            self.fail("应当返回 409")
        except HTTPError as exc:
            self.assertEqual(exc.code, 409)
            body = json.load(exc)
            self.assertEqual(body["error"], "ConflictError")

    def test_bad_request_returns_400(self):
        try:
            self._post("/api/decisions", {"facility_id": "NOPE",
                                          "action": "retire"})
            self.fail("应当返回 400")
        except HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
