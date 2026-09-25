"""退役与替换决策链的领域合同测试。"""

import unittest

from domain import DecisionStore, DomainError


def seed_evidence(store, case_id, *, with_usage=True, open_orders=("WO-OPEN",)):
    """灌入六类按时间排列的基础证据。"""
    records = [
        {"type": "funding", "date": "2024-01-10", "source": "体彩公益金",
         "amount": 50000, "batch": "2024-A"},
        {"type": "installation", "date": "2024-02-01", "batch": "2024-A"},
        {"type": "maintenance", "date": "2025-01-05", "work_order_id": "WO-1",
         "cost": 800, "status": "closed"},
        {"type": "maintenance", "date": "2025-03-05", "work_order_id": "WO-2",
         "cost": 1500, "status": "closed"},
        {"type": "safety", "date": "2025-04-01", "incident_id": "INC-1",
         "severity": 2},
        {"type": "coverage", "date": "2025-06-01", "gap_level": 0.7},
    ]
    if with_usage:
        records.append({"type": "usage", "date": "2025-05-01", "count": 120,
                        "ceiling": 500})
    for wid in open_orders:
        records.append({"type": "maintenance", "date": "2025-07-01",
                        "work_order_id": wid, "cost": 0, "status": "open"})
    for record in records:
        store.ingest(case_id, record)


def replacement(new_asset_id="AST-NEW", amount=60000):
    return {
        "new_asset_id": new_asset_id,
        "requested_amount": amount,
        "site_obligations": [{"obligation": "场地地面管护", "cadence": "每季度"}],
        "cached_notices": [{"notice_id": "N-1", "channel": "qrcode"}],
        "warranties": [{"warranty": "主轴两年保修", "until": "2028-01-01"}],
    }


def propose(store, case_id, option="retire", *, replacements=None,
            qrcode=None, rationale="反复维修且零件停产"):
    payload = {
        "case_id": case_id,
        "option": option,
        "by": "运营单位甲",
        "at": "2026-01-10",
        "rationale": rationale,
        "service_plan": "由邻近点位 P-2 补位",
    }
    if option in ("retire", "migrate", "retrofit"):
        payload["qrcode_disposition"] = qrcode or {
            "action": "retarget", "target_asset_id": "AST-NEW"
        }
        payload["old_asset_disposition"] = "拆除回收"
    if replacements is not None:
        payload["replacements"] = replacements
    return store.add_proposal(payload)


def all_signoffs(store, case_id, *, risk_confirmed=True, gap=None):
    store.add_signoff({
        "case_id": case_id, "kind": "finance", "by": "财务员乙",
        "at": "2026-01-12", "unamortized_amount": 12000,
        "disposition": "按公益金结余规定核减",
    })
    store.add_signoff({
        "case_id": case_id, "kind": "safety", "by": "安全负责人丙",
        "at": "2026-01-13", "risk_confirmed": risk_confirmed,
    })
    store.add_signoff({
        "case_id": case_id, "kind": "community", "by": "社区代表丁",
        "at": "2026-01-14",
        "service_gap_opinion": gap or {"gap_level": 0.7, "note": "晚间高峰缺器械"},
    })


def decided_case(store, facility_id="F-1", *, option="retire", replacements=None,
                 with_usage=True, effective_date="2026-03-01"):
    case = store.create_case(facility_id)
    seed_evidence(store, case["case_id"], with_usage=with_usage)
    propose(store, case["case_id"], option, replacements=replacements)
    all_signoffs(store, case["case_id"])
    decision = store.decide({
        "case_id": case["case_id"], "by": "体育主管部门",
        "at": "2026-02-01", "effective_date": effective_date,
    })
    return case["case_id"], decision


class TimelineTest(unittest.TestCase):
    def setUp(self):
        self.store = DecisionStore()
        self.cid = self.store.create_case("F-1")["case_id"]

    def test_evidence_grouped_by_type_in_time_order(self):
        seed_evidence(self.store, self.cid)
        view = self.store.timeline_view(self.cid)
        dates = [r["date"] for r in view["by_type"]["maintenance"]]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(view["maintenance_spending"], 2300)
        self.assertEqual(view["safety_incidents"], 1)
        self.assertIn("WO-OPEN", view["open_work_orders"])

    def test_missing_usage_is_flagged_not_counted_as_zero(self):
        # 不提交任何使用记录：缺测必须显式标注，不得解释为无人使用
        seed_evidence(self.store, self.cid, with_usage=False)
        view = self.store.timeline_view(self.cid)
        self.assertEqual(view["usage_summary"]["evidence_status"], "no_evidence")
        self.assertIsNone(view["usage_summary"]["latest_count"])
        self.assertEqual(view["usage_summary"]["observations"], 0)

    def test_observed_zero_usage_is_distinct_from_missing(self):
        seed_evidence(self.store, self.cid, with_usage=False)
        self.store.ingest(self.cid, {"type": "usage", "date": "2025-08-01",
                                     "count": 0, "ceiling": 500})
        view = self.store.timeline_view(self.cid)
        self.assertEqual(view["usage_summary"]["evidence_status"], "observed")
        self.assertEqual(view["usage_summary"]["latest_count"], 0)

    def test_negative_usage_rejected(self):
        with self.assertRaises(DomainError):
            self.store.ingest(self.cid, {"type": "usage", "date": "2025-08-01",
                                         "count": -3})

    def test_evidence_frozen_after_decision(self):
        cid, _ = decided_case(self.store, "F-2")
        with self.assertRaises(DomainError):
            self.store.ingest(cid, {"type": "usage", "date": "2026-03-02",
                                    "count": 1})

    def test_duplicate_facility_case_rejected(self):
        with self.assertRaises(DomainError):
            self.store.create_case("F-1")


class SignoffGateTest(unittest.TestCase):
    def setUp(self):
        self.store = DecisionStore()
        self.cid = self.store.create_case("F-1")["case_id"]
        seed_evidence(self.store, self.cid)

    def test_decision_requires_proposal_and_all_three_signoffs(self):
        with self.assertRaises(DomainError):
            self.store.decide({"case_id": self.cid, "by": "x",
                               "effective_date": "2026-03-01"})
        propose(self.store, self.cid)
        self.store.add_signoff({"case_id": self.cid, "kind": "finance",
                                "by": "财务", "at": "2026-01-12",
                                "unamortized_amount": 1, "disposition": "核减"})
        with self.assertRaises(DomainError) as error:
            self.store.decide({"case_id": self.cid, "by": "x",
                               "effective_date": "2026-03-01"})
        self.assertIn("安全负责人风险确认", str(error.exception))
        self.assertIn("社区服务缺口意见", str(error.exception))

    def test_community_can_only_sign_service_gap(self):
        propose(self.store, self.cid)
        with self.assertRaises(DomainError) as error:
            self.store.add_signoff({
                "case_id": self.cid, "kind": "community", "by": "丁",
                "at": "2026-01-14", "service_gap_opinion": "有缺口",
                "unamortized_amount": 0,
            })
        self.assertIn("服务缺口", str(error.exception))

    def test_finance_must_reconcile_unamortized_funds(self):
        propose(self.store, self.cid)
        with self.assertRaises(DomainError):
            self.store.add_signoff({"case_id": self.cid, "kind": "finance",
                                    "by": "财务", "at": "2026-01-12"})

    def test_unconfirmed_safety_risk_blocks_decision(self):
        propose(self.store, self.cid)
        all_signoffs(self.store, self.cid, risk_confirmed=False)
        with self.assertRaises(DomainError):
            self.store.decide({"case_id": self.cid, "by": "部门",
                               "effective_date": "2026-03-01"})

    def test_decision_carries_effective_date(self):
        propose(self.store, self.cid)
        all_signoffs(self.store, self.cid)
        decision = self.store.decide({
            "case_id": self.cid, "by": "部门", "at": "2026-02-01",
            "effective_date": "2026-03-01",
        })
        self.assertEqual(decision["effective_date"], "2026-03-01")
        self.assertEqual(self.store.case_view(self.cid)["status"], "decided")

    def test_retire_proposal_requires_qrcode_and_asset_disposition(self):
        with self.assertRaises(DomainError):
            self.store.add_proposal({
                "case_id": self.cid, "option": "retire", "by": "甲",
                "at": "2026-01-10", "rationale": "r",
            })
        with self.assertRaises(DomainError):
            self.store.add_proposal({
                "case_id": self.cid, "option": "retire", "by": "甲",
                "at": "2026-01-10", "rationale": "r",
                "qrcode_disposition": {"action": "retarget"},
                "old_asset_disposition": "拆除",
            })


class RetirementTest(unittest.TestCase):
    def setUp(self):
        self.store = DecisionStore()
        self.cid, _ = decided_case(self.store, "F-1")

    def test_retirement_closes_open_orders_but_keeps_history(self):
        result = self.store.execute_retirement({
            "case_id": self.cid, "at": "2026-03-02", "receipt_id": "R-1",
        })
        self.assertTrue(result["retired"])
        self.assertIn("WO-OPEN", result["closed_future_work_orders"])
        # 事故与支出历史保留
        self.assertEqual(result["history_retained"]["safety_incidents"], 1)
        self.assertEqual(result["history_retained"]["maintenance_spending"], 2300)
        view = self.store.timeline_view(self.cid)
        self.assertEqual(view["open_work_orders"], [])
        self.assertEqual(view["safety_incidents"], 1)
        self.assertEqual(view["maintenance_spending"], 2300)
        # 旧资产与二维码指导处置随退役决定留痕
        self.assertEqual(result["qrcode_disposition"]["action"], "retarget")
        self.assertEqual(result["old_asset_disposition"], "拆除回收")

    def test_duplicate_retirement_receipt_does_not_double_process(self):
        first = self.store.execute_retirement({
            "case_id": self.cid, "at": "2026-03-02", "receipt_id": "R-1",
        })
        second = self.store.execute_retirement({
            "case_id": self.cid, "at": "2026-03-03", "receipt_id": "R-1",
        })
        self.assertTrue(second["duplicate_receipt"])
        self.assertEqual(
            second["closed_future_work_orders"],
            first["closed_future_work_orders"],
        )
        view = self.store.case_view(self.cid)
        self.assertEqual(view["closed_work_orders"], ["WO-OPEN"])


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.store = DecisionStore()
        self.repl = replacement()
        self.cid, _ = decided_case(
            self.store, "F-1", replacements=[self.repl]
        )

    def test_handover_atomically_transfers_obligations_notices_warranties(self):
        result = self.store.execute_handover({
            "case_id": self.cid, "at": "2026-03-02", "receipt_id": "H-1",
        })
        self.assertTrue(result["atomic"])
        ledger = self.store.asset_view("AST-NEW")
        self.assertEqual(ledger["site_obligations"][0]["obligation"], "场地地面管护")
        self.assertEqual(ledger["cached_notices"][0]["retarget_to"], "AST-NEW")
        self.assertEqual(ledger["warranties"][0]["until"], "2028-01-01")
        # 旧故障与旧事故不得算到新资产
        self.assertEqual(ledger["maintenance_spending"], 0)
        self.assertEqual(ledger["maintenance_records"], [])
        self.assertEqual(ledger["safety_records"], [])
        excluded = result["handovers"][0]["excluded_old_faults"]
        self.assertEqual(set(excluded), {"WO-1", "WO-2", "WO-OPEN"})
        self.assertEqual(result["handovers"][0]["excluded_old_incidents"], ["INC-1"])

    def test_new_asset_faults_stay_on_new_ledger(self):
        self.store.execute_handover({"case_id": self.cid, "at": "2026-03-02"})
        self.store.record_asset_incident("AST-NEW", {
            "type": "maintenance", "date": "2026-04-01",
            "work_order_id": "WO-NEW", "cost": 200,
        })
        ledger = self.store.asset_view("AST-NEW")
        self.assertEqual(ledger["maintenance_spending"], 200)
        new_order_ids = {r["work_order_id"] for r in ledger["maintenance_records"]}
        self.assertEqual(new_order_ids, {"WO-NEW"})

    def test_handover_is_atomic_on_validation_failure(self):
        # 第二个替换设备引用了已存在台账的资产：整笔交接必须回滚
        other = self.store.create_case("F-OTHER")["case_id"]
        seed_evidence(self.store, other)
        propose(self.store, other, replacements=[replacement("AST-DUP")])
        all_signoffs(self.store, other)
        self.store.decide({"case_id": other, "by": "部门",
                           "effective_date": "2026-03-01"})
        self.store.execute_handover({"case_id": other, "at": "2026-03-02"})

        cid2, _ = decided_case(
            self.store, "F-2",
            replacements=[replacement("AST-OK"), replacement("AST-DUP")],
        )
        with self.assertRaises(DomainError):
            self.store.execute_handover({"case_id": cid2, "at": "2026-03-02"})
        # 回滚：AST-OK 也不得落账
        with self.assertRaises(DomainError):
            self.store.asset_view("AST-OK")

    def test_duplicate_handover_receipt_returns_same_result(self):
        first = self.store.execute_handover({
            "case_id": self.cid, "at": "2026-03-02", "receipt_id": "H-1",
        })
        second = self.store.execute_handover({
            "case_id": self.cid, "at": "2026-03-03", "receipt_id": "H-1",
        })
        self.assertTrue(second["duplicate_receipt"])
        self.assertEqual(len(self.store.case_view(self.cid)["handovers"]),
                         len(first["handovers"]))


class BudgetOrderTest(unittest.TestCase):
    def _rules(self):
        return {
            "approved": True,
            "version": "v2026-1",
            "weights": {"service_gap": 0.3, "usage": 0.3,
                        "safety_risk": 0.25, "cost_effectiveness": 0.15},
        }

    def test_only_approved_rules_accepted(self):
        store = DecisionStore()
        with self.assertRaises(DomainError):
            store.budget_order({"rules": {"approved": False}, "budget": 1})
        with self.assertRaises(DomainError):
            store.budget_order({"rules": {"approved": True}, "budget": 1})

    def test_missing_usage_neutral_not_zero_and_flags_candidate(self):
        store = DecisionStore()
        # 无使用证据的案件：使用指标从中性归一化中剔除，并被标注
        cid_missing, _ = decided_case(store, "F-MISS", with_usage=False,
                                      replacements=[replacement("AST-M", 40000)])
        order = store.budget_order({"rules": self._rules(), "budget": 100000})
        row = next(r for r in order["order"] if r["case_id"] == cid_missing)
        self.assertTrue(row["usage_evidence_missing"])
        # 其余指标（缺口0.7、安全0.667、性价比）仍计分，不会因缺测变 0 分候选
        self.assertGreater(row["score"], 0.5)

    def test_budget_funds_in_rank_order(self):
        store = DecisionStore()
        cid1, _ = decided_case(store, "F-1", replacements=[replacement("AST-1", 40000)])
        cid2, _ = decided_case(store, "F-2", replacements=[replacement("AST-2", 40000)])
        order = store.budget_order({"rules": self._rules(), "budget": 45000})
        self.assertTrue(order["order"][0]["funded"])
        self.assertFalse(order["order"][1]["funded"])
        self.assertEqual(order["allocated"], 40000)

    def test_manual_adjust_requires_reason_and_is_audited(self):
        store = DecisionStore()
        cid1, _ = decided_case(store, "F-1", replacements=[replacement("AST-1", 1)])
        cid2, _ = decided_case(store, "F-2", replacements=[replacement("AST-2", 1)])
        store.budget_order({"rules": self._rules(), "budget": 1000})
        with self.assertRaises(DomainError):
            store.adjust_order({"case_id": cid2, "position": 1, "by": "处长"})
        store.adjust_order({"case_id": cid2, "position": 1, "by": "处长",
                            "reason": "F-2 点位安全事件升级，优先补位"})
        self.assertEqual(store.budget_round["order"][0]["case_id"], cid2)
        self.assertEqual(store.budget_round["order"][0]["adjust_reason"],
                         "F-2 点位安全事件升级，优先补位")


class WithdrawAndPhaseTest(unittest.TestCase):
    def test_phased_completion_then_completed_status(self):
        store = DecisionStore()
        cid, _ = decided_case(store, "F-1", option="retrofit")
        store.plan_phases({"case_id": cid, "phases": [
            {"id": "P1", "label": "基础加固"}, {"id": "P2", "label": "器械更新"},
        ]})
        r1 = store.complete_phase({"case_id": cid, "phase_id": "P1",
                                   "at": "2026-03-05", "receipt_id": "PH-1"})
        self.assertFalse(r1["all_done"])
        # 重复回执不重复处置
        r1b = store.complete_phase({"case_id": cid, "phase_id": "P1",
                                    "at": "2026-03-05", "receipt_id": "PH-1"})
        self.assertTrue(r1b["duplicate_receipt"])
        r2 = store.complete_phase({"case_id": cid, "phase_id": "P2",
                                   "at": "2026-03-10", "receipt_id": "PH-2"})
        self.assertTrue(r2["all_done"])
        self.assertEqual(store.case_view(cid)["status"], "completed")

    def test_withdraw_preserves_executed_actions_and_allows_resume(self):
        store = DecisionStore()
        cid, _ = decided_case(store, "F-1")
        store.execute_retirement({"case_id": cid, "at": "2026-03-02",
                                  "receipt_id": "R-1"})
        result = store.withdraw({"case_id": cid, "at": "2026-03-10",
                                 "reason": "处置去向需补充说明"})
        self.assertTrue(result["resumable"])
        self.assertEqual(result["retained"]["closed_work_orders"], ["WO-OPEN"])
        self.assertEqual(store.case_view(cid)["status"], "open")
        # 按原方案重新提案：会签仍有效，可恢复形成决定
        propose(store, cid)
        decision = store.decide({"case_id": cid, "by": "部门",
                                 "effective_date": "2026-04-01"})
        self.assertEqual(decision["status"], "decided")
        # 现场退役不重复执行（回执历史与已执行标记保留）
        again = store.execute_retirement({"case_id": cid, "at": "2026-04-02"})
        self.assertTrue(again["already_executed"])

    def test_changed_proposal_after_withdraw_requires_new_signoffs(self):
        store = DecisionStore()
        cid, _ = decided_case(store, "F-1")
        store.withdraw({"case_id": cid, "at": "2026-03-10", "reason": "改方案"})
        propose(store, cid, "migrate", rationale="迁移至室内点位")
        with self.assertRaises(DomainError) as error:
            store.decide({"case_id": cid, "by": "部门",
                          "effective_date": "2026-04-01"})
        self.assertIn("会签未齐备", str(error.exception))

    def test_executed_retirement_cannot_switch_to_keep(self):
        store = DecisionStore()
        cid, _ = decided_case(store, "F-1")
        store.execute_retirement({"case_id": cid, "at": "2026-03-02"})
        store.withdraw({"case_id": cid, "at": "2026-03-10", "reason": "r"})
        propose(store, cid, "keep")
        all_signoffs(store, cid)
        with self.assertRaises(DomainError) as error:
            store.decide({"case_id": cid, "by": "部门",
                          "effective_date": "2026-04-01"})
        self.assertIn("旧资产已离场", str(error.exception))


class FundingTraceTest(unittest.TestCase):
    def test_funds_continue_until_retirement_effective_date(self):
        store = DecisionStore()
        cid, _ = decided_case(store, "F-1", effective_date="2026-03-01")
        trace = store.funding_trace("F-1")
        self.assertEqual(len(trace["funds"]), 1)
        self.assertTrue(trace["funds"][0]["continues"])
        self.assertIn("继续投入理由", trace["funds"][0]["why"])

        store.execute_retirement({"case_id": cid, "at": "2026-03-02"})
        trace = store.funding_trace("F-1")
        self.assertFalse(trace["funds"][0]["continues"])
        self.assertEqual(trace["funds"][0]["stops_on"], "2026-03-01")
        # 未摊销资金核对结果可追溯
        self.assertEqual(trace["unamortized"]["amount"], 12000)

    def test_service_backfill_visible_after_handover(self):
        store = DecisionStore()
        cid, _ = decided_case(store, "F-1", replacements=[replacement()])
        trace_before = store.funding_trace("F-1")
        self.assertFalse(trace_before["service_backfill"]["covered"])
        store.execute_handover({"case_id": cid, "at": "2026-03-02"})
        trace_after = store.funding_trace("F-1")
        self.assertTrue(trace_after["service_backfill"]["covered"])
        self.assertEqual(trace_after["service_backfill"]["replacement_assets"],
                         ["AST-NEW"])
        self.assertTrue(trace_after["service_backfill"]["handover_done"])

    def test_withdrawn_decision_marks_funds_as_pending_redecision(self):
        store = DecisionStore()
        cid, _ = decided_case(store, "F-1")
        store.withdraw({"case_id": cid, "at": "2026-03-10", "reason": "r"})
        trace = store.funding_trace("F-1")
        self.assertTrue(trace["funds"][0]["continues"])
        self.assertIn("撤回", trace["funds"][0]["why"])


if __name__ == "__main__":
    unittest.main()
