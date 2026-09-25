"""健身设施退役与替换决策链。

覆盖年度预算评审要求的完整链路：

* 证据时间汇总：资金来源、安装批次、使用证据、安全事件、维修成本、服务覆盖
  按时间窗口汇总；缺测（无数据）与"测得零使用"严格区分，缺测不得自动解释
  为无人使用。
* 四方门控：运营单位（方案）、财务（未摊销资金核对）、安全负责人（风险确认）、
  社区代表（仅签署服务缺口），全部满足且有效后才能定稿。
* 定稿决定带生效日；退役关闭未来工单但保留事故与支出历史；替换原子承接适用的
  场地义务、缓存通知与保修责任，旧故障不记入新资产。
* 预算不足时按已批准的公共价值规则形成候选顺序，人工调整必须说明理由并留痕。
* 重复现场回执幂等；决定可撤回、可分期执行且可恢复。
* 资金追踪回答每笔资金为何继续投入、何时停止、居民服务是否得到补位。

模块只依赖标准库，状态保存在内存存储中，便于测试与本地联调。
"""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from copy import deepcopy
from datetime import date


class DecisionError(ValueError):
    """请求本身不合法（结构错误、前置条件不满足等），对应 HTTP 4xx。"""


class ConflictError(DecisionError):
    """状态冲突（重复定稿、重复执行等），对应 HTTP 409。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _today(value=None):
    if value is None:
        return date.today().isoformat()
    _parse_date(value)
    return value


def _parse_date(value):
    """宽松但明确的 ISO 日期解析，拒绝日期以外的输入。"""
    if not isinstance(value, str):
        raise DecisionError(f"日期必须是 YYYY-MM-DD 字符串：{value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DecisionError(f"日期格式应为 YYYY-MM-DD：{value!r}") from exc


def _require(obj, key, kind=None):
    if not isinstance(obj, dict) or key not in obj or obj[key] in (None, ""):
        raise DecisionError(f"缺少必填字段：{key}")
    value = obj[key]
    if kind is not None and not isinstance(value, kind):
        label = getattr(kind, "__name__", str(kind))
        raise DecisionError(f"字段 {key} 类型应为 {label}")
    return value


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

class Store:
    """线程安全的内存状态。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.facilities = {}          # facility_id -> dict
        self.funding = {}             # funding_id -> dict
        self.work_orders = {}         # work_order_id -> dict
        self.usage_observations = {}  # observation_id -> dict
        self.safety_incidents = {}    # incident_id -> dict
        self.service_coverage = {}    # coverage_id -> dict
        self.qr_guides = {}           # qr_id -> dict
        self.warranties = {}          # warranty_id -> dict
        self.maintenance_promises = {}  # promise_id -> dict（未结维保承诺）
        self.receipts = {}            # receipt_id -> 回执记录
        self.decisions = {}           # decision_id -> dict

    def snapshot(self):
        with self.lock:
            return deepcopy({
                "facilities": self.facilities,
                "funding": self.funding,
                "work_orders": self.work_orders,
                "usage_observations": self.usage_observations,
                "safety_incidents": self.safety_incidents,
                "service_coverage": self.service_coverage,
                "qr_guides": self.qr_guides,
                "warranties": self.warranties,
                "maintenance_promises": self.maintenance_promises,
                "receipts": self.receipts,
                "decisions": self.decisions,
            })


# ---------------------------------------------------------------------------
# 录入服务
# ---------------------------------------------------------------------------

class IntakeService:
    """登记设施、资金、工单、使用观测等基础记录。

    现场回执（receipt）是所有录入的幂等包装：同一现场回执编号重复提交时返回
    首次处理结果，绝不重复处置。
    """

    def __init__(self, store: Store):
        self.store = store

    # -- 幂等回执 ---------------------------------------------------------

    def submit_receipt(self, payload, today=None):
        receipt_no = _require(payload, "receipt_no", str)
        with self.store.lock:
            existing = self._find_receipt(receipt_no)
            if existing is not None:
                return {
                    "receipt_no": receipt_no,
                    "duplicate": True,
                }
            records = payload.get("records", [])
            if not isinstance(records, list):
                raise DecisionError("records 必须是数组")
            created = []
            for raw in records:
                created.append(self._ingest_one(raw, today=today))
            receipt = {
                "receipt_no": receipt_no,
                "submitted_on": _today(today),
                "records": created,
            }
            self.store.receipts[receipt_no] = receipt
            return {"receipt_no": receipt_no, "duplicate": False, "records": created}

    def _find_receipt(self, receipt_no):
        for receipt in self.store.receipts.values():
            if receipt["receipt_no"] == receipt_no:
                return receipt
        return None

    def _ingest_one(self, raw, today):
        kind = _require(raw, "type", str)
        handler = {
            "facility": self._facility,
            "funding": self._funding,
            "work_order": self._work_order,
            "usage_observation": self._usage_observation,
            "safety_incident": self._safety_incident,
            "service_coverage": self._service_coverage,
            "qr_guide": self._qr_guide,
            "warranty": self._warranty,
            "maintenance_promise": self._maintenance_promise,
        }.get(kind)
        if handler is None:
            raise DecisionError(f"未知记录类型：{kind}")
        return handler(raw)

    def _facility(self, raw):
        fid = _require(raw, "facility_id", str)
        if fid in self.store.facilities:
            raise ConflictError(f"设施已存在：{fid}")
        record = {
            "facility_id": fid,
            "name": raw.get("name", fid),
            "site_id": _require(raw, "site_id", str),
            "funding_ids": list(_require(raw, "funding_ids", list)),
            "install_batch": _require(raw, "install_batch", str),
            "installed_on": _require(raw, "installed_on", str),
            "status": "active",
            "superseded_by": None,
        }
        self.store.facilities[fid] = record
        return {"type": "facility", "id": fid}

    def _funding(self, raw):
        fid = _require(raw, "funding_id", str)
        if fid in self.store.funding:
            raise ConflictError(f"资金已存在：{fid}")
        amount = _require(raw, "amount")
        if not isinstance(amount, (int, float)) or amount < 0:
            raise DecisionError("amount 必须是非负数")
        record = {
            "funding_id": fid,
            "source": _require(raw, "source", str),
            "amount": amount,
            "awarded_on": _require(raw, "awarded_on", str),
            "amortization_years": _require(raw, "amortization_years", int),
            "facility_id": _require(raw, "facility_id", str),
        }
        self.store.funding[fid] = record
        return {"type": "funding", "id": fid}

    def _work_order(self, raw):
        wid = _new_id("wo")
        record = {
            "work_order_id": wid,
            "facility_id": _require(raw, "facility_id", str),
            "opened_on": _require(raw, "opened_on", str),
            "scheduled_for": raw.get("scheduled_for"),
            "closed_on": raw.get("closed_on"),
            "cost": raw.get("cost", 0),
            "reason": _require(raw, "reason", str),
            "status": raw.get("status", "open"),
        }
        if record["scheduled_for"] is not None:
            _parse_date(record["scheduled_for"])
        if record["closed_on"] is not None:
            _parse_date(record["closed_on"])
        if not isinstance(record["cost"], (int, float)) or record["cost"] < 0:
            raise DecisionError("cost 必须是非负数")
        self.store.work_orders[wid] = record
        return {"type": "work_order", "id": wid}

    def _usage_observation(self, raw):
        oid = _new_id("obs")
        measured = _require(raw, "measured", bool)
        record = {
            "observation_id": oid,
            "facility_id": _require(raw, "facility_id", str),
            "window_start": _require(raw, "window_start", str),
            "window_end": _require(raw, "window_end", str),
            "measured": measured,
            "count": _require(raw, "count", int) if measured else None,
            "note": raw.get("note", ""),
        }
        if measured and record["count"] < 0:
            raise DecisionError("count 不能为负")
        _parse_date(record["window_start"])
        _parse_date(record["window_end"])
        self.store.usage_observations[oid] = record
        return {"type": "usage_observation", "id": oid}

    def _safety_incident(self, raw):
        iid = _new_id("inc")
        record = {
            "incident_id": iid,
            "facility_id": _require(raw, "facility_id", str),
            "occurred_on": _require(raw, "occurred_on", str),
            "severity": _require(raw, "severity", str),
            "description": raw.get("description", ""),
            "preserved": True,
        }
        self.store.safety_incidents[iid] = record
        return {"type": "safety_incident", "id": iid}

    def _service_coverage(self, raw):
        cid = _new_id("cov")
        record = {
            "coverage_id": cid,
            "facility_id": _require(raw, "facility_id", str),
            "site_id": _require(raw, "site_id", str),
            "on": _require(raw, "on", str),
            "residents_served": _require(raw, "residents_served", int),
            "service_type": raw.get("service_type", "fitness"),
        }
        self.store.service_coverage[cid] = record
        return {"type": "service_coverage", "id": cid}

    def _qr_guide(self, raw):
        qid = _require(raw, "qr_id", str)
        if qid in self.store.qr_guides:
            raise ConflictError(f"二维码指导已存在：{qid}")
        record = {
            "qr_id": qid,
            "facility_id": _require(raw, "facility_id", str),
            "url": _require(raw, "url", str),
            "status": "active",
        }
        self.store.qr_guides[qid] = record
        return {"type": "qr_guide", "id": qid}

    def _warranty(self, raw):
        wid = _new_id("war")
        record = {
            "warranty_id": wid,
            "facility_id": _require(raw, "facility_id", str),
            "provider": _require(raw, "provider", str),
            "covers_until": _require(raw, "covers_until", str),
            "transferable": bool(raw.get("transferable", False)),
            "status": "active",
        }
        _parse_date(record["covers_until"])
        self.store.warranties[wid] = record
        return {"type": "warranty", "id": wid}

    def _maintenance_promise(self, raw):
        pid = _new_id("prom")
        record = {
            "promise_id": pid,
            "facility_id": _require(raw, "facility_id", str),
            "description": _require(raw, "description", str),
            "due_on": _require(raw, "due_on", str),
            "status": "open",
        }
        self.store.maintenance_promises[pid] = record
        return {"type": "maintenance_promise", "id": pid}


# ---------------------------------------------------------------------------
# 证据时间汇总
# ---------------------------------------------------------------------------

class EvidenceService:
    """按时间窗口汇总设施证据。

    关键规则：观测分三类——有数据（measured=true）、测得零使用（count=0）、
    缺测（measured=false）。缺测窗口单独计数，绝不折算成零使用。
    """

    def __init__(self, store: Store):
        self.store = store

    def facility_timeline(self, facility_id, window_start=None, window_end=None):
        with self.store.lock:
            facility = self.store.facilities.get(facility_id)
            if facility is None:
                raise DecisionError(f"设施不存在：{facility_id}")

            def in_window(on):
                d = _parse_date(on)
                if window_start is not None and d < _parse_date(window_start):
                    return False
                if window_end is not None and d > _parse_date(window_end):
                    return False
                return True

            observations = [
                o for o in self.store.usage_observations.values()
                if o["facility_id"] == facility_id
                and in_window(o["window_start"])
            ]
            measured = [o for o in observations if o["measured"]]
            unmeasured = [o for o in observations if not o["measured"]]
            zero_windows = [o for o in measured if o["count"] == 0]

            total_uses = sum(o["count"] for o in measured)
            avg_uses = round(total_uses / len(measured), 2) if measured else None

            work_orders = [
                w for w in self.store.work_orders.values()
                if w["facility_id"] == facility_id and in_window(w["opened_on"])
            ]
            incidents = [
                i for i in self.store.safety_incidents.values()
                if i["facility_id"] == facility_id and in_window(i["occurred_on"])
            ]
            coverage = [
                c for c in self.store.service_coverage.values()
                if c["facility_id"] == facility_id and in_window(c["on"])
            ]

            funding = [
                self.store.funding[fid]
                for fid in facility["funding_ids"]
                if fid in self.store.funding
            ]

            return {
                "facility_id": facility_id,
                "window": {"start": window_start, "end": window_end},
                "funding_sources": [
                    {
                        "funding_id": f["funding_id"],
                        "source": f["source"],
                        "amount": f["amount"],
                        "awarded_on": f["awarded_on"],
                    }
                    for f in funding
                ],
                "install_batch": facility["install_batch"],
                "installed_on": facility["installed_on"],
                "usage": {
                    "measured_windows": len(measured),
                    "missing_windows": len(unmeasured),
                    "zero_use_windows": len(zero_windows),
                    "total_uses": total_uses,
                    "avg_uses_per_measured_window": avg_uses,
                    # 显式声明：缺测不构成无人使用的证据
                    "missing_treated_as_zero": False,
                    "evidence_status": self._evidence_status(measured, unmeasured),
                    "windows": [
                        {
                            "window_start": o["window_start"],
                            "window_end": o["window_end"],
                            "measured": o["measured"],
                            "count": o["count"],
                        }
                        for o in observations
                    ],
                },
                "safety_incidents": [
                    {
                        "incident_id": i["incident_id"],
                        "occurred_on": i["occurred_on"],
                        "severity": i["severity"],
                        "description": i["description"],
                    }
                    for i in incidents
                ],
                "maintenance": {
                    "work_order_count": len(work_orders),
                    "total_cost": round(sum(w["cost"] for w in work_orders), 2),
                    "open_count": sum(1 for w in work_orders if w["status"] == "open"),
                    "work_orders": [
                        {
                            "work_order_id": w["work_order_id"],
                            "opened_on": w["opened_on"],
                            "cost": w["cost"],
                            "reason": w["reason"],
                            "status": w["status"],
                        }
                        for w in work_orders
                    ],
                },
                "service_coverage": {
                    "latest_residents_served": coverage[-1]["residents_served"]
                    if coverage
                    else None,
                    "records": [
                        {
                            "on": c["on"],
                            "residents_served": c["residents_served"],
                            "service_type": c["service_type"],
                        }
                        for c in coverage
                    ],
                },
            }

    @staticmethod
    def _evidence_status(measured, unmeasured):
        if measured and not unmeasured:
            return "complete"
        if measured and unmeasured:
            return "partial"
        return "missing"  # 全部缺测：结论只能是"证据不足"，不是"无人使用"


# ---------------------------------------------------------------------------
# 门控
# ---------------------------------------------------------------------------

ROLE_OPERATOR = "operator"      # 运营单位：提方案
ROLE_FINANCE = "finance"        # 财务：核对未摊销资金
ROLE_SAFETY = "safety_officer"  # 安全负责人：确认风险
ROLE_COMMUNITY = "community"    # 社区代表：只签服务缺口

ACTION_RETREAT = "retire"
ACTION_REPLACE = "replace"
ACTION_MIGRATE = "migrate"
ACTION_RENOVATE = "renovate"
ACTION_KEEP = "keep"
VALID_ACTIONS = {
    ACTION_RETREAT,
    ACTION_REPLACE,
    ACTION_MIGRATE,
    ACTION_RENOVATE,
    ACTION_KEEP,
}


class GateService:
    """四方门控：各方只对自己职责范围内的事项签署。"""

    def __init__(self, store: Store):
        self.store = store

    def create_decision(self, payload):
        facility_id = _require(payload, "facility_id", str)
        action = _require(payload, "action", str)
        if action not in VALID_ACTIONS:
            raise DecisionError(f"action 必须是 {sorted(VALID_ACTIONS)} 之一")
        with self.store.lock:
            if facility_id not in self.store.facilities:
                raise DecisionError(f"设施不存在：{facility_id}")
            if any(
                d["facility_id"] == facility_id and d["status"] in ("draft", "finalized")
                for d in self.store.decisions.values()
            ):
                raise ConflictError(f"设施已有进行中的决定：{facility_id}")
            did = _new_id("dec")
            decision = {
                "decision_id": did,
                "facility_id": facility_id,
                "action": action,
                "replacement_facility_id": payload.get("replacement_facility_id"),
                "effective_on": None,
                "status": "draft",
                "proposal": None,
                "signoffs": {},
                "history": [
                    {"event": "created", "action": action}
                ],
                "phases": [],
                "execution": {"state": "pending", "steps": []},
            }
            if action == ACTION_REPLACE:
                rid = _require(payload, "replacement_facility_id", str)
                if rid not in self.store.facilities:
                    raise DecisionError(f"替换设施不存在：{rid}")
                if rid == facility_id:
                    raise DecisionError("替换设施不能与旧设施相同")
            self.store.decisions[did] = decision
            return self._public_view(decision)

    def sign(self, decision_id, payload):
        role = _require(payload, "role", str)
        with self.store.lock:
            decision = self._get(decision_id)
            self._assert_editable(decision)
            signer = _require(payload, "signer", str)
            on = _today(payload.get("on"))

            if role == ROLE_OPERATOR:
                action = _require(payload, "action", str)
                if action not in VALID_ACTIONS:
                    raise DecisionError(f"action 必须是 {sorted(VALID_ACTIONS)} 之一")
                if action != decision["action"]:
                    raise DecisionError(
                        f"运营方案 {action} 与决定动作 {decision['action']} 不一致"
                    )
                decision["proposal"] = {
                    "action": action,
                    "rationale": _require(payload, "rationale", str),
                    "options_reviewed": _require(payload, "options_reviewed", list),
                    "estimated_cost": _require(payload, "estimated_cost"),
                    "phased": bool(payload.get("phased", False)),
                }
                decision["signoffs"][ROLE_OPERATOR] = {
                    "signer": signer, "on": on,
                    "scope": "proposal",
                }

            elif role == ROLE_FINANCE:
                unamortized = _require(payload, "unamortized_amount")
                if not isinstance(unamortized, (int, float)) or unamortized < 0:
                    raise DecisionError("unamortized_amount 必须是非负数")
                decision["signoffs"][ROLE_FINANCE] = {
                    "signer": signer,
                    "on": on,
                    "scope": "unamortized_funds",
                    "unamortized_amount": unamortized,
                    "verified": bool(payload.get("verified", True)),
                }

            elif role == ROLE_SAFETY:
                decision["signoffs"][ROLE_SAFETY] = {
                    "signer": signer,
                    "on": on,
                    "scope": "risk",
                    "risk_level": _require(payload, "risk_level", str),
                    "risk_accepted": bool(_require(payload, "risk_accepted")),
                    "residual_measures": payload.get("residual_measures", []),
                }

            elif role == ROLE_COMMUNITY:
                # 社区代表只能对服务缺口发表意见，不能替其他方背书
                decision["signoffs"][ROLE_COMMUNITY] = {
                    "signer": signer,
                    "on": on,
                    "scope": "service_gap_only",
                    "service_gap": _require(payload, "service_gap", str),
                    "gap_acceptable": bool(_require(payload, "gap_acceptable")),
                    "mitigation": payload.get("mitigation", ""),
                }
            else:
                raise DecisionError(f"未知签署角色：{role}")

            decision["history"].append(
                {"event": "signoff", "role": role, "signer": signer, "on": on}
            )
            return self._public_view(decision)

    def finalize(self, decision_id, payload):
        effective_on = _today(_require(payload, "effective_on", str))
        with self.store.lock:
            decision = self._get(decision_id)
            self._assert_editable(decision)
            missing = [
                role for role in
                (ROLE_OPERATOR, ROLE_FINANCE, ROLE_SAFETY, ROLE_COMMUNITY)
                if role not in decision["signoffs"]
            ]
            if missing:
                raise ConflictError(f"门控未齐备，缺少：{', '.join(missing)}")
            # 安全风险不接受则不能定稿
            if not decision["signoffs"][ROLE_SAFETY]["risk_accepted"]:
                raise ConflictError("安全负责人未接受风险，不能定稿")
            # 社区认为服务缺口不可接受且无缓解措施，不能定稿
            community = decision["signoffs"][ROLE_COMMUNITY]
            if not community["gap_acceptable"] and not community.get("mitigation"):
                raise ConflictError("服务缺口不可接受且缺少缓解安排，不能定稿")

            decision["status"] = "finalized"
            decision["effective_on"] = effective_on
            decision["history"].append(
                {"event": "finalized", "effective_on": effective_on}
            )
            return self._public_view(decision)

    def withdraw(self, decision_id, payload, today=None):
        reason = _require(payload, "reason", str)
        with self.store.lock:
            decision = self._get(decision_id)
            if decision["status"] == "withdrawn":
                raise ConflictError("决定已撤回")
            if decision["status"] == "executed":
                raise ConflictError("已全部执行完毕的决定不可撤回")
            decision["status"] = "withdrawn"
            decision["history"].append({
                "event": "withdrawn",
                "reason": reason,
                "on": _today(today),
            })
            return self._public_view(decision)

    def restore(self, decision_id, today=None):
        """撤回后恢复：已执行的分期步骤保持完成状态，流程可继续。"""
        with self.store.lock:
            decision = self._get(decision_id)
            if decision["status"] != "withdrawn":
                raise ConflictError("只有已撤回的决定可以恢复")
            decision["status"] = "finalized"
            decision["history"].append(
                {"event": "restored", "on": _today(today)}
            )
            return self._public_view(decision)

    def get(self, decision_id):
        with self.store.lock:
            return self._public_view(self._get(decision_id))

    def list_decisions(self):
        with self.store.lock:
            return [self._public_view(d) for d in self.store.decisions.values()]

    def _get(self, decision_id):
        decision = self.store.decisions.get(decision_id)
        if decision is None:
            raise DecisionError(f"决定不存在：{decision_id}")
        return decision

    @staticmethod
    def _assert_editable(decision):
        if decision["status"] == "finalized":
            raise ConflictError("决定已定稿，不能再修改或重复定稿")
        if decision["status"] == "withdrawn":
            raise ConflictError("决定已撤回，需先恢复")

    @staticmethod
    def _public_view(decision):
        return deepcopy(decision)


# ---------------------------------------------------------------------------
# 执行：退役 / 替换 / 迁移 / 改造
# ---------------------------------------------------------------------------

def _is_future_work_order(work_order, effective_on):
    """生效日之后仍待处置的工单视为未来工单：有排期按排期，无排期按开单日。"""
    reference = work_order["scheduled_for"] or work_order["opened_on"]
    return reference >= effective_on


class ExecutionService:
    """定稿决定的执行。

    * 退役：关闭生效日之后的未来工单；事故与支出历史原样保留。
    * 替换：单事务原子承接适用的场地义务、二维码通知缓存与保修责任；
      旧设施的故障/工单/事故绝不转移给新资产。
    * 分期：可分批执行，撤回后恢复可继续。
    """

    STEP_RETREAT_CANCEL_FUTURE_WO = "cancel_future_work_orders"
    STEP_RETREAT_PRESERVE_HISTORY = "preserve_incident_and_spending_history"
    STEP_RETREAT_DECOMMISSION_QR = "decommission_qr_guide"
    STEP_RETREAT_SETTLE_PROMISES = "settle_maintenance_promises"
    STEP_REPLACE_TRANSFER_SITE_OBLIGATIONS = "transfer_site_obligations"
    STEP_REPLACE_TRANSFER_WARRANTY = "transfer_applicable_warranties"
    STEP_REPLACE_CACHE_NOTICE = "cache_replacement_notice"
    STEP_REPLACE_CLOSE_OLD = "retire_old_facility"
    STEP_REPLACE_VERIFY_FAULT_ISOLATION = "verify_fault_isolation"

    def __init__(self, store: Store):
        self.store = store
        # 通知缓存：qr_id -> 已缓存通知（替换后扫码可见承接说明）
        self.notice_cache = {}

    def execute(self, decision_id, payload=None, today=None):
        payload = payload or {}
        on = _today(payload.get("on", today))
        only_steps = payload.get("steps")
        if only_steps is not None and not isinstance(only_steps, list):
            raise DecisionError("steps 必须是数组")
        with self.store.lock:
            decision = self.store.decisions.get(decision_id)
            if decision is None:
                raise DecisionError(f"决定不存在：{decision_id}")
            if decision["status"] == "withdrawn":
                raise ConflictError("决定已撤回，需先恢复才能执行")
            if decision["status"] not in ("finalized", "executed"):
                raise ConflictError("只有已定稿决定可以执行")
            if on < decision["effective_on"]:
                raise ConflictError(
                    f"执行日 {on} 早于生效日 {decision['effective_on']}"
                )

            plan = self._plan_for(decision)
            done = {
                s["step"] for s in decision["execution"]["steps"] if s["state"] == "done"
            }
            # 已全部执行完毕：重复回执/重复调用直接回当前状态（幂等）
            if len(done) == len(plan):
                return self._result(decision)

            pending = [s for s in plan if s not in done]
            if only_steps:
                unknown = [s for s in only_steps if s not in plan]
                if unknown:
                    raise DecisionError(f"不属于该决定的执行步骤：{unknown}")
                pending = [s for s in pending if s in only_steps]
                if not pending:
                    return self._result(decision)

            # 原子性：先在暂存区计算全部效果，任一前置不满足则整体不落库
            staged_changes = []
            for step in pending:
                staged_changes.append(
                    self._apply_step(decision, step, on, dry_run=True)
                )
            for change in staged_changes:
                change()  # 提交暂存的变更

            for step in pending:
                decision["execution"]["steps"].append(
                    {"step": step, "state": "done", "on": on}
                )
            decision["history"].append(
                {"event": "executed_steps", "steps": list(pending), "on": on}
            )

            if len({s["step"] for s in decision["execution"]["steps"]
                    if s["state"] == "done"}) == len(plan):
                decision["execution"]["state"] = "done"
                decision["status"] = "executed"
                decision["history"].append({"event": "fully_executed", "on": on})
            else:
                decision["execution"]["state"] = "partial"
            return self._result(decision)

    def _plan_for(self, decision):
        action = decision["action"]
        if action == ACTION_RETREAT:
            return [
                self.STEP_RETREAT_CANCEL_FUTURE_WO,
                self.STEP_RETREAT_SETTLE_PROMISES,
                self.STEP_RETREAT_DECOMMISSION_QR,
                self.STEP_RETREAT_PRESERVE_HISTORY,
            ]
        if action == ACTION_REPLACE:
            return [
                # 先承接，再关闭旧资产，最后核对故障隔离
                self.STEP_REPLACE_TRANSFER_SITE_OBLIGATIONS,
                self.STEP_REPLACE_TRANSFER_WARRANTY,
                self.STEP_REPLACE_CACHE_NOTICE,
                self.STEP_REPLACE_CLOSE_OLD,
                self.STEP_REPLACE_VERIFY_FAULT_ISOLATION,
            ]
        # 迁移 / 改造 / 保留：只关闭未来工单并核对承诺，不动历史
        return [
            self.STEP_RETREAT_CANCEL_FUTURE_WO,
            self.STEP_RETREAT_SETTLE_PROMISES,
        ]

    # -- 各步骤：dry_run 返回一个零参数闭包，调用时才真正落库 -------------

    def _apply_step(self, decision, step, on, dry_run):
        handler = {
            self.STEP_RETREAT_CANCEL_FUTURE_WO: self._step_cancel_future_wo,
            self.STEP_RETREAT_PRESERVE_HISTORY: self._step_preserve_history,
            self.STEP_RETREAT_DECOMMISSION_QR: self._step_decommission_qr,
            self.STEP_RETREAT_SETTLE_PROMISES: self._step_settle_promises,
            self.STEP_REPLACE_TRANSFER_SITE_OBLIGATIONS:
                self._step_transfer_site_obligations,
            self.STEP_REPLACE_TRANSFER_WARRANTY: self._step_transfer_warranty,
            self.STEP_REPLACE_CACHE_NOTICE: self._step_cache_notice,
            self.STEP_REPLACE_CLOSE_OLD: self._step_close_old,
            self.STEP_REPLACE_VERIFY_FAULT_ISOLATION:
                self._step_verify_fault_isolation,
        }[step]
        return handler(decision, on, dry_run)

    def _step_cancel_future_wo(self, decision, on, dry_run):
        fid = decision["facility_id"]
        effective = decision["effective_on"]
        targets = [
            w for w in self.store.work_orders.values()
            if w["facility_id"] == fid
            and w["status"] == "open"
            and _is_future_work_order(w, effective)
        ]

        def commit():
            for w in targets:
                w["status"] = "cancelled"
                w["cancelled_by_decision"] = decision["decision_id"]
                w["cancelled_on"] = on

        return commit

    def _step_preserve_history(self, decision, on, dry_run):
        # 事故与支出历史不可删除；此步骤固化保留标记并校验仍在
        fid = decision["facility_id"]
        incidents = [i for i in self.store.safety_incidents.values()
                     if i["facility_id"] == fid]
        spent = round(sum(
            w["cost"] for w in self.store.work_orders.values()
            if w["facility_id"] == fid
        ), 2)

        def commit():
            for i in incidents:
                i["preserved"] = True
            self.store.facilities[fid]["status"] = "retired"
            self.store.facilities[fid]["retired_on"] = on
            self.store.facilities[fid]["retired_by_decision"] = (
                decision["decision_id"]
            )
            decision["execution"].setdefault("preserved", {})
            decision["execution"]["preserved"] = {
                "incident_ids": [i["incident_id"] for i in incidents],
                "total_historical_spending": spent,
            }

        return commit

    def _step_decommission_qr(self, decision, on, dry_run):
        fid = decision["facility_id"]
        targets = [q for q in self.store.qr_guides.values()
                   if q["facility_id"] == fid and q["status"] == "active"]

        def commit():
            for q in targets:
                q["status"] = "decommissioned"
                q["decommissioned_on"] = on
                q["decommissioned_by_decision"] = decision["decision_id"]

        return commit

    def _step_settle_promises(self, decision, on, dry_run):
        fid = decision["facility_id"]
        open_promises = [p for p in self.store.maintenance_promises.values()
                         if p["facility_id"] == fid and p["status"] == "open"]
        # 未结维保承诺必须显式处置，不能悬空
        if not open_promises:
            return lambda: None
        plan = decision.get("proposal") or {}
        settlement = plan.get("promise_settlement", "honor_before_close")
        if settlement == "transfer" and decision["action"] != ACTION_REPLACE:
            raise DecisionError("维保承诺仅替换决定可转移给承接资产")
        if settlement not in (
            "transfer", "honor_before_close", "cancel_with_refund"
        ):
            raise DecisionError(f"未知维保承诺处置方式：{settlement}")

        def commit():
            for p in open_promises:
                if settlement == "transfer":
                    p["facility_id"] = decision["replacement_facility_id"]
                    p["transferred_from"] = fid
                    p["transferred_on"] = on
                    p["status"] = "transferred"
                elif settlement == "honor_before_close":
                    p["status"] = "honored"
                    p["honored_on"] = on
                else:
                    p["status"] = "cancelled_refunded"
                    p["cancelled_on"] = on

        return commit

    def _step_transfer_site_obligations(self, decision, on, dry_run):
        old = self.store.facilities[decision["facility_id"]]
        new = self.store.facilities[decision["replacement_facility_id"]]
        if old["site_id"] != new["site_id"]:
            raise DecisionError("替换设备不在同一场地，不能原子承接场地义务")
        transferred_funding = list(old["funding_ids"])

        def commit():
            # 场地服务义务（以服务覆盖记录表达）由新设施承接
            for cov in self.store.service_coverage.values():
                if cov["facility_id"] == old["facility_id"]:
                    cov["facility_id"] = new["facility_id"]
                    cov["assumed_from"] = old["facility_id"]
                    cov["assumed_on"] = on
            # 未结维保承诺随场地义务转移（仅适用的义务）
            for p in self.store.maintenance_promises.values():
                if p["facility_id"] == old["facility_id"] and p["status"] == "open":
                    p["facility_id"] = new["facility_id"]
                    p["transferred_from"] = old["facility_id"]
                    p["transferred_on"] = on
                    p["status"] = "transferred"
            new["assumed_funding_ids"] = sorted(
                set(new.get("assumed_funding_ids", [])) | set(transferred_funding)
            )
            new["assumed_obligations_from"] = old["facility_id"]

        return commit

    def _step_transfer_warranty(self, decision, on, dry_run):
        old_id = decision["facility_id"]
        new_id = decision["replacement_facility_id"]
        active = [
            w for w in self.store.warranties.values()
            if w["facility_id"] == old_id
            and w["status"] == "active"
            and w["transferable"]
            and _parse_date(w["covers_until"]) >= _parse_date(on)
        ]

        def commit():
            for w in active:
                w["facility_id"] = new_id
                w["transferred_from"] = old_id
                w["transferred_on"] = on

        return commit

    def _step_cache_notice(self, decision, on, dry_run):
        old_id = decision["facility_id"]
        new_id = decision["replacement_facility_id"]
        qr_ids = [q["qr_id"] for q in self.store.qr_guides.values()
                  if q["facility_id"] == old_id]
        notices = {
            qid: {
                "type": "replacement",
                "old_facility_id": old_id,
                "replacement_facility_id": new_id,
                "decision_id": decision["decision_id"],
                "effective_on": decision["effective_on"],
                "cached_on": on,
            }
            for qid in qr_ids
        }

        def commit():
            self.notice_cache.update(notices)

        return commit

    def _step_close_old(self, decision, on, dry_run):
        old_id = decision["facility_id"]
        new_id = decision["replacement_facility_id"]
        effective = decision["effective_on"]
        future_wo = [
            w for w in self.store.work_orders.values()
            if w["facility_id"] == old_id and w["status"] == "open"
            and _is_future_work_order(w, effective)
        ]
        old_qr = [q for q in self.store.qr_guides.values()
                  if q["facility_id"] == old_id and q["status"] == "active"]

        def commit():
            old = self.store.facilities[old_id]
            old["status"] = "retired"
            old["retired_on"] = on
            old["superseded_by"] = new_id
            new = self.store.facilities[new_id]
            new["supersedes"] = old_id
            for w in future_wo:
                w["status"] = "cancelled"
                w["cancelled_by_decision"] = decision["decision_id"]
                w["cancelled_on"] = on
            for q in old_qr:
                q["status"] = "redirected"
                q["redirected_to_facility"] = new_id
                q["redirected_on"] = on

        return commit

    def _step_verify_fault_isolation(self, decision, on, dry_run):
        """核对不变量：旧故障不随承接进入新资产。

        在暂存阶段（任何提交前）即校验，保证整笔替换事务原子性：承接步骤不会
        移动旧设施的工单与事故，故当前状态即事务后状态；一旦发现旧资产故障挂到
        新资产名下，整批步骤全部不落库。
        """
        old_id = decision["facility_id"]
        new_id = decision["replacement_facility_id"]
        leaked_wo = [
            w for w in self.store.work_orders.values()
            if w["facility_id"] == new_id and w.get("origin_facility_id") == old_id
        ]
        inherited_incidents = [
            i for i in self.store.safety_incidents.values()
            if i["facility_id"] == new_id and i.get("origin_facility_id") == old_id
        ]
        if leaked_wo or inherited_incidents:
            raise ConflictError("发现旧资产故障被记入新资产，已中止整笔替换")

        def commit():
            decision["execution"]["fault_isolation_verified"] = {
                "old_facility_id": old_id,
                "new_facility_id": new_id,
                "new_work_orders_originating_from_old": 0,
                "new_incidents_originating_from_old": 0,
                "on": on,
            }

        return commit

    def _result(self, decision):
        return {
            "decision_id": decision["decision_id"],
            "status": decision["status"],
            "execution_state": decision["execution"]["state"],
            "steps": deepcopy(decision["execution"]["steps"]),
            "notice_cache": deepcopy(self.notice_cache),
        }


# ---------------------------------------------------------------------------
# 预算排序：公共价值规则
# ---------------------------------------------------------------------------

class BudgetService:
    """预算不足时按已批准的公共价值规则生成候选顺序。

    规则（已批准版本 v1，权重固定可审计）：
      公共价值分 = 0.4 * 使用强度 + 0.3 * 服务覆盖 + 0.2 * 安全收益
                   + 0.1 * 资金止损
    使用证据全部缺测的设施不得排到可服务设施之前（缺测不视为零使用，
    同样也不能冒充高使用）。
    """

    RULE_VERSION = "public-value-v1"
    WEIGHTS = {"usage": 0.4, "coverage": 0.3, "safety": 0.2, "cost_avoidance": 0.1}

    def __init__(self, store: Store):
        self.store = store
        self.overrides = {}  # ranking_id -> [人工调整记录]

    def rank_candidates(self, payload):
        facility_ids = _require(payload, "facility_ids", list)
        budget = _require(payload, "budget")
        if not isinstance(budget, (int, float)) or budget < 0:
            raise DecisionError("budget 必须是非负数")
        proposals = payload.get("proposals", {})
        with self.store.lock:
            candidates = []
            for fid in facility_ids:
                if fid not in self.store.facilities:
                    raise DecisionError(f"设施不存在：{fid}")
                candidates.append(self._score(fid, proposals.get(fid, {})))

            # 已有人工调整（在本次重算时仍需重新给出理由，故排名先按规则）
            ranked = sorted(
                candidates,
                key=lambda c: (
                    # 证据完整者优先于纯缺测者
                    0 if c["evidence_status"] != "missing" else 1,
                    -c["public_value_score"],
                ),
            )

            selected = []
            remaining = budget
            for c in ranked:
                if c["estimated_cost"] <= remaining:
                    c["funded"] = True
                    selected.append(c["facility_id"])
                    remaining -= c["estimated_cost"]
                else:
                    c["funded"] = False

            ranking_id = _new_id("rank")
            record = {
                "ranking_id": ranking_id,
                "rule_version": self.RULE_VERSION,
                "weights": dict(self.WEIGHTS),
                "budget": budget,
                "on": _today(payload.get("on")),
                "candidates": ranked,
                "funded": selected,
                "adjustments": [],
            }
            self.overrides[ranking_id] = record
            return deepcopy(record)

    def adjust_ranking(self, ranking_id, payload):
        """人工调整候选顺序：必须说明理由并留痕。"""
        facility_id = _require(payload, "facility_id", str)
        new_position = _require(payload, "new_position", int)
        reason = _require(payload, "reason", str)
        if len(reason.strip()) < 5:
            raise DecisionError("人工调整必须说明具体理由（不少于 5 字）")
        with self.store.lock:
            record = self.overrides.get(ranking_id)
            if record is None:
                raise DecisionError(f"排序不存在：{ranking_id}")
            ids = [c["facility_id"] for c in record["candidates"]]
            if facility_id not in ids:
                raise DecisionError("调整对象不在候选名单中")
            if not 0 <= new_position < len(ids):
                raise DecisionError("new_position 超出候选范围")
            old_position = ids.index(facility_id)
            candidate = record["candidates"].pop(old_position)
            record["candidates"].insert(new_position, candidate)
            record["adjustments"].append({
                "facility_id": facility_id,
                "from_position": old_position,
                "to_position": new_position,
                "reason": reason,
                "by": _require(payload, "by", str),
                "on": _today(payload.get("on")),
            })
            # 重新计算资金分配（人工顺序优先）
            remaining = record["budget"]
            record["funded"] = []
            for c in record["candidates"]:
                if c["estimated_cost"] <= remaining:
                    c["funded"] = True
                    record["funded"].append(c["facility_id"])
                    remaining -= c["estimated_cost"]
                else:
                    c["funded"] = False
            return deepcopy(record)

    def get_ranking(self, ranking_id):
        with self.store.lock:
            record = self.overrides.get(ranking_id)
            if record is None:
                raise DecisionError(f"排序不存在：{ranking_id}")
            return deepcopy(record)

    def _score(self, facility_id, proposal):
        observations = [
            o for o in self.store.usage_observations.values()
            if o["facility_id"] == facility_id
        ]
        measured = [o for o in observations if o["measured"]]
        missing = [o for o in observations if not o["measured"]]
        evidence_status = EvidenceService._evidence_status(measured, missing)

        # 使用强度：取有数据窗口均值；缺测窗口不折算零，用中性分 0.5 占位
        avg_use = (sum(o["count"] for o in measured) / len(measured)) if measured else 0
        usage_score = self._scale(min(avg_use / 200.0, 1.0)) if measured else 0.5

        coverage_records = [
            c for c in self.store.service_coverage.values()
            if c["facility_id"] == facility_id
        ]
        latest_served = max(
            (c["residents_served"] for c in coverage_records), default=0
        )
        coverage_score = self._scale(min(latest_served / 500.0, 1.0))

        incident_records = [
            i for i in self.store.safety_incidents.values()
            if i["facility_id"] == facility_id
        ]
        high = sum(1 for i in incident_records if i["severity"] == "high")
        safety_score = self._scale(min(high / 3.0, 1.0))

        annual_repair = sum(
            w["cost"] for w in self.store.work_orders.values()
            if w["facility_id"] == facility_id
        )
        cost_avoidance_score = self._scale(min(annual_repair / 20000.0, 1.0))

        score = round(
            self.WEIGHTS["usage"] * usage_score
            + self.WEIGHTS["coverage"] * coverage_score
            + self.WEIGHTS["safety"] * safety_score
            + self.WEIGHTS["cost_avoidance"] * cost_avoidance_score,
            4,
        )
        return {
            "facility_id": facility_id,
            "estimated_cost": self._cost(facility_id, proposal.get("estimated_cost")),
            "proposed_action": proposal.get("proposed_action", ACTION_RETREAT),
            "public_value_score": score,
            "evidence_status": evidence_status,
            "score_breakdown": {
                "usage": round(usage_score, 4),
                "coverage": round(coverage_score, 4),
                "safety": round(safety_score, 4),
                "cost_avoidance": round(cost_avoidance_score, 4),
            },
        }

    def _cost(self, facility_id, value):
        if value is None:
            # 未提供估算时，以历年维修支出的 1.2 倍作为替换/退役的保守估算
            annual = sum(
                w["cost"] for w in self.store.work_orders.values()
                if w["facility_id"] == facility_id
            )
            return round(annual * 1.2, 2)
        if not isinstance(value, (int, float)) or value < 0:
            raise DecisionError("estimated_cost 必须是非负数")
        return value

    @staticmethod
    def _scale(value):
        return round(max(0.0, min(1.0, value)), 4)


# ---------------------------------------------------------------------------
# 资金追踪
# ---------------------------------------------------------------------------

class FundingTraceService:
    """回答：每笔资金为何继续投入、何时停止、居民服务是否得到补位。"""

    def __init__(self, store: Store, execution: ExecutionService):
        self.store = store
        self.execution = execution

    def trace_funding(self, funding_id, as_of=None):
        with self.store.lock:
            fund = self.store.funding.get(funding_id)
            if fund is None:
                raise DecisionError(f"资金不存在：{funding_id}")
            fid = fund["facility_id"]
            facility = self.store.facilities.get(fid)
            as_of = as_of or date.today().isoformat()
            _parse_date(as_of)
            years_active = max(
                (
                    _parse_date(as_of) - _parse_date(fund["awarded_on"])
                ).days / 365.25,
                0.0,
            )
            annual_amount = fund["amount"] / max(fund["amortization_years"], 1)
            amortized = round(
                min(years_active, fund["amortization_years"]) * annual_amount, 2
            )
            unamortized = round(fund["amount"] - amortized, 2)

            spent_on_repairs = round(sum(
                w["cost"] for w in self.store.work_orders.values()
                if w["facility_id"] == fid
            ), 2)
            decisions = [
                d for d in self.store.decisions.values()
                if d["facility_id"] == fid
            ]

            # 资金去向：退役即止于旧资产；替换则随场地义务由新资产承接
            carried_to = None
            stop_on = None
            for d in decisions:
                if d["action"] == ACTION_REPLACE and d["status"] in (
                    "finalized", "executed"
                ) and facility and facility.get("superseded_by"):
                    carried_to = facility["superseded_by"]
                if d["action"] == ACTION_RETREAT and d["effective_on"]:
                    stop_on = d["effective_on"]
                if d["action"] == ACTION_REPLACE and d["effective_on"]:
                    stop_on = d["effective_on"]

            continue_reasons = self._continue_reasons(
                fid, spent_on_repairs, decisions
            )
            service_backfilled = self._service_backfill(fid, carried_to)

            return {
                "funding_id": funding_id,
                "source": fund["source"],
                "amount": fund["amount"],
                "facility_id": fid,
                "amortization": {
                    "amortized": amortized,
                    "unamortized": unamortized,
                    "as_of": as_of,
                },
                "repair_spending_to_date": spent_on_repairs,
                "continuation": {
                    "still_investing": stop_on is None,
                    "why_continue": continue_reasons,
                    "stop_on": stop_on,
                    "carried_to_facility_id": carried_to,
                },
                "service_backfill": service_backfilled,
                "decisions": [
                    {"decision_id": d["decision_id"], "action": d["action"],
                     "status": d["status"], "effective_on": d["effective_on"]}
                    for d in decisions
                ],
            }

    def _continue_reasons(self, fid, spent, decisions):
        active_open_wo = [
            w for w in self.store.work_orders.values()
            if w["facility_id"] == fid and w["status"] == "open"
        ]
        reasons = []
        if active_open_wo:
            reasons.append(
                f"仍有 {len(active_open_wo)} 张未关闭工单，资金用于在保维修"
            )
        if not any(d["status"] in ("finalized", "executed") for d in decisions):
            reasons.append("尚无生效的退役/替换决定，按原资金用途继续投入")
        if spent > 0 and not reasons:
            reasons.append("决定已生效，历史支出保留可查，不再新增投入")
        return reasons

    def _service_backfill(self, fid, carried_to):
        old_coverage = [
            c for c in self.store.service_coverage.values()
            if c.get("assumed_from") == fid or c["facility_id"] == fid
        ]
        if carried_to is None:
            # 非替换：检查同场地是否仍有活跃设施承担覆盖
            facility = self.store.facilities.get(fid)
            site = facility["site_id"] if facility else None
            alternatives = [
                f2 for f2 in self.store.facilities.values()
                if site and f2["site_id"] == site
                and f2["facility_id"] != fid and f2["status"] == "active"
            ]
            return {
                "backfilled": bool(alternatives),
                "means": "same_site_active_facility" if alternatives else "none",
                "by_facility_ids": [a["facility_id"] for a in alternatives],
            }
        new_facility = self.store.facilities.get(carried_to)
        assumed = [
            c for c in self.store.service_coverage.values()
            if c["facility_id"] == carried_to and c.get("assumed_from") == fid
        ]
        return {
            "backfilled": new_facility is not None
            and new_facility["status"] == "active"
            and bool(assumed),
            "means": "replacement_assumed_site_obligation",
            "by_facility_ids": [carried_to] if assumed else [],
        }


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

class DecisionChainApp:
    """把各领域服务绑定到同一份存储上，供接口层调用。"""

    def __init__(self, store=None):
        self.store = store or Store()
        self.intake = IntakeService(self.store)
        self.evidence = EvidenceService(self.store)
        self.gates = GateService(self.store)
        self.execution = ExecutionService(self.store)
        self.budget = BudgetService(self.store)
        self.funding_trace = FundingTraceService(self.store, self.execution)
