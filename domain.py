"""健身设施退役与替换决策链的领域服务。

决策链覆盖：证据按时间汇总（缺测不等于无人使用）、运营单位提案、
财务/安全/社区三方会签（社区仅对服务缺口表态）、带生效日的决定、
退役关闭未来工单但保留事故与支出历史、替换设备原子承接场地义务/
缓存通知/保修责任且旧故障不转移、按已批准公共价值规则排序候选、
现场回执幂等、撤回与分期可恢复，以及资金投入可追溯查询。

本模块只依赖标准库，所有状态保存在内存中，由 :class:`DecisionStore`
在单把锁内串行化写入，方便 HTTP 层直接复用。
"""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from datetime import date
from typing import Any, Optional

# 运营单位可提出的四种处置方案
OPTIONS = ("keep", "migrate", "retrofit", "retire")
OPTION_LABELS = {
    "keep": "保留",
    "migrate": "迁移",
    "retrofit": "改造",
    "retire": "退役",
}

# 六类按时间汇总的证据
EVIDENCE_TYPES = ("funding", "installation", "usage", "safety", "maintenance", "coverage")

# 三方会签角色；社区代表只允许就服务缺口发表意见
SIGNOFF_FINANCE = "finance"
SIGNOFF_SAFETY = "safety"
SIGNOFF_COMMUNITY = "community"

# 社区意见允许携带的字段白名单：服务缺口之外的内容一律不得签署
COMMUNITY_ALLOWED_FIELDS = {"service_gap_opinion"}

# 退役时二维码指导的处置方式
QRCODE_ACTIONS = ("remove", "retarget", "keep_generic")


class DomainError(ValueError):
    """输入违反决策链规则。"""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _require(data: dict, key: str) -> Any:
    if key not in data or data[key] in (None, ""):
        raise DomainError(f"缺少必填字段: {key}")
    return data[key]


class DecisionStore:
    """内存态决策链存储，方法对应业务动作并返回可序列化结果。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.cases: dict[str, dict] = {}
        self.facility_index: dict[str, str] = {}
        # 新资产台账：handover 后建立，只含新资产自身的故障与支出
        self.assets: dict[str, dict] = {}
        # 现场回执幂等表：receipt_id -> 首次处理结果
        self.receipts: dict[str, dict] = {}
        # 已批准的预算候选顺序（最近一次预算评审）
        self.budget_round: Optional[dict] = None

    # ------------------------------------------------------------------
    # 案件与证据
    # ------------------------------------------------------------------
    def create_case(self, facility_id: str) -> dict:
        with self._lock:
            if facility_id in self.facility_index:
                raise DomainError(f"设施 {facility_id} 已存在案件")
            case_id = _new_id("case")
            case = {
                "case_id": case_id,
                "facility_id": facility_id,
                "created_at": date.today().isoformat(),
                "timeline": [],
                "proposals": [],
                "signoffs": {},
                "proposal_signature": None,
                "decision": None,
                "decision_history": [],
                "phases": [],
                "completed_phases": [],
                "attempts": [],
                "retirement_executed": False,
                "handover_executed": False,
                "closed_work_orders": [],
                "handovers": [],
                "events": [],
                "status": "open",
            }
            self.cases[case_id] = case
            self.facility_index[facility_id] = case_id
            case["events"].append({"at": case["created_at"], "event": "case_created"})
            return self.case_view(case_id)

    def _require_case(self, case_id: str) -> dict:
        case = self.cases.get(case_id)
        if case is None:
            raise DomainError(f"案件不存在: {case_id}")
        return case

    def case_for_facility(self, facility_id: str) -> Optional[str]:
        return self.facility_index.get(facility_id)

    def ingest(self, case_id: str, record: dict) -> dict:
        """追加一条带日期的证据记录。

        使用证据缺测通过“没有记录”表达；汇总层会显式标注 no_evidence，
        任何路径都不会把缺测折算成使用量为零。
        """
        record = dict(record)
        kind = _require(record, "type")
        if kind not in EVIDENCE_TYPES:
            raise DomainError(f"未知证据类型: {kind}")
        on_date = _require(record, "date")
        with self._lock:
            case = self._require_case(case_id)
            if case["decision"] and case["status"] != "open":
                raise DomainError("决定生效后证据链冻结，不得补录证据")
            record["record_id"] = _new_id("ev")
            if kind == "usage":
                # count 只允许为非负整数；没有数据就不要提交记录
                count = record.get("count")
                if count is not None and (not isinstance(count, int) or count < 0):
                    raise DomainError("使用次数 count 必须为非负整数；缺测请不要提交零值记录")
            if kind == "maintenance":
                record.setdefault("status", "closed")
                if record["status"] not in ("open", "closed"):
                    raise DomainError("工单状态只能是 open 或 closed")
            case["timeline"].append(record)
            case["events"].append({"at": on_date, "event": f"evidence:{kind}"})
            return {"ingested": record["record_id"], "timeline": self.timeline_view(case_id)}

    # ------------------------------------------------------------------
    # 提案与会签
    # ------------------------------------------------------------------
    def add_proposal(self, payload: dict) -> dict:
        case_id = _require(payload, "case_id")
        option = _require(payload, "option")
        if option not in OPTIONS:
            raise DomainError(f"方案必须是 {OPTIONS} 之一")
        with self._lock:
            case = self._require_case(case_id)
            if case["decision"] and case["status"] == "decided":
                raise DomainError("已有生效决定，需先撤回才能提交新提案")
            proposal = {
                "proposal_id": _new_id("prop"),
                "option": option,
                "option_label": OPTION_LABELS[option],
                "by": _require(payload, "by"),
                "at": _require(payload, "at"),
                "rationale": _require(payload, "rationale"),
                "service_plan": payload.get("service_plan", ""),
                "qrcode_disposition": None,
                "old_asset_disposition": None,
                "replacements": [],
            }
            # 退役/迁移/改造涉及旧资产下场，必须说明旧资产与二维码指导去向
            if option in ("retire", "migrate", "retrofit"):
                qr = _require(payload, "qrcode_disposition")
                if qr.get("action") not in QRCODE_ACTIONS:
                    raise DomainError(f"二维码处置 action 必须是 {QRCODE_ACTIONS}")
                if qr["action"] == "retarget" and not qr.get("target_asset_id"):
                    raise DomainError("二维码改投必须指定 target_asset_id")
                proposal["qrcode_disposition"] = qr
                proposal["old_asset_disposition"] = _require(payload, "old_asset_disposition")
            for repl in payload.get("replacements", []):
                proposal["replacements"].append(self._validate_replacement(repl))
            case["proposals"].append(proposal)
            # 撤回后按原方案恢复决定的，会签继续有效；
            # 方案实质变更（选项/理由/替换设备/处置去向变化）必须重新会签。
            signature = self._proposal_signature(proposal)
            if case.get("proposal_signature") != signature:
                if case["signoffs"] or case["phases"]:
                    case["attempts"].append({
                        "signoffs": dict(case["signoffs"]),
                        "phases": list(case["phases"]),
                        "completed_phases": list(case["completed_phases"]),
                    })
                case["signoffs"] = {}
                case["phases"] = []
                case["completed_phases"] = []
                case["proposal_signature"] = signature
            case["events"].append({"at": proposal["at"], "event": f"proposal:{option}"})
            return proposal

    @staticmethod
    def _proposal_signature(proposal: dict) -> str:
        import json
        return json.dumps({
            "option": proposal["option"],
            "rationale": proposal["rationale"],
            "service_plan": proposal["service_plan"],
            "qrcode_disposition": proposal["qrcode_disposition"],
            "old_asset_disposition": proposal["old_asset_disposition"],
            "replacements": proposal["replacements"],
        }, sort_keys=True, ensure_ascii=False)

    @staticmethod
    def _validate_replacement(repl: dict) -> dict:
        return {
            "new_asset_id": _require(repl, "new_asset_id"),
            "requested_amount": int(_require(repl, "requested_amount")),
            # 场地义务（适用的场地管护要求）
            "site_obligations": list(_require(repl, "site_obligations")),
            # 已缓存的居民通知/二维码指导推送，需要重定向到新资产
            "cached_notices": list(_require(repl, "cached_notices")),
            # 保修责任（含未结维保承诺），按是否适用于新资产区分
            "warranties": list(_require(repl, "warranties")),
        }

    def add_signoff(self, payload: dict) -> dict:
        case_id = _require(payload, "case_id")
        kind = _require(payload, "kind")
        if kind not in (SIGNOFF_FINANCE, SIGNOFF_SAFETY, SIGNOFF_COMMUNITY):
            raise DomainError("会签角色只能是 finance/safety/community")
        with self._lock:
            case = self._require_case(case_id)
            if not case["proposals"]:
                raise DomainError("运营单位尚未提出方案，无法会签")
            at = _require(payload, "at")
            by = _require(payload, "by")
            if kind == SIGNOFF_FINANCE:
                signoff = {
                    "kind": kind,
                    "by": by,
                    "at": at,
                    # 未摊销资金必须核对出具体金额（可为 0）与处置去向
                    "unamortized_amount": _require(payload, "unamortized_amount"),
                    "disposition": _require(payload, "disposition"),
                }
            elif kind == SIGNOFF_SAFETY:
                extra = set(payload) - {
                    "case_id", "kind", "by", "at", "risk_confirmed", "residual_risk_note",
                }
                if extra:
                    raise DomainError(f"安全确认包含不被接受的字段: {sorted(extra)}")
                signoff = {
                    "kind": kind,
                    "by": by,
                    "at": at,
                    "risk_confirmed": bool(_require(payload, "risk_confirmed")),
                    "residual_risk_note": payload.get("residual_risk_note", ""),
                }
            else:
                opinion_keys = set(payload) - {"case_id", "kind", "by", "at"}
                forbidden = opinion_keys - COMMUNITY_ALLOWED_FIELDS
                if forbidden:
                    raise DomainError(
                        "社区代表仅可对服务缺口签署意见，不能就资金/风险等事项表态: "
                        f"{sorted(forbidden)}"
                    )
                signoff = {
                    "kind": kind,
                    "by": by,
                    "at": at,
                    "service_gap_opinion": _require(payload, "service_gap_opinion"),
                }
            case["signoffs"][kind] = signoff
            case["events"].append({"at": at, "event": f"signoff:{kind}"})
            return signoff

    # ------------------------------------------------------------------
    # 决定
    # ------------------------------------------------------------------
    def decide(self, payload: dict) -> dict:
        case_id = _require(payload, "case_id")
        effective_date = _require(payload, "effective_date")
        with self._lock:
            case = self._require_case(case_id)
            proposal = case["proposals"][-1] if case["proposals"] else None
            missing = []
            if proposal is None:
                missing.append("运营单位方案")
            for role, label in (
                (SIGNOFF_FINANCE, "财务未摊销资金核对"),
                (SIGNOFF_SAFETY, "安全负责人风险确认"),
                (SIGNOFF_COMMUNITY, "社区服务缺口意见"),
            ):
                if role not in case["signoffs"]:
                    missing.append(label)
            if missing:
                raise DomainError("会签未齐备，不能形成决定: " + "、".join(missing))
            if not case["signoffs"][SIGNOFF_SAFETY]["risk_confirmed"]:
                raise DomainError("安全风险未确认，不能形成决定")
            if case["retirement_executed"] and proposal["option"] != "retire":
                raise DomainError(
                    "现场退役已执行，旧资产已离场，不能改为保留/迁移方案；"
                    "如需补位请另立替换案件"
                )
            # 已原子交接给新资产的，撤回后不得换成另一批替换设备
            if case["handover_executed"]:
                executed = {h["new_asset_id"] for h in case["handovers"]}
                proposed = {r["new_asset_id"] for r in proposal["replacements"]}
                if executed != proposed:
                    raise DomainError(
                        "替换设备已完成原子交接，不能更换为其他新资产；如需变更请另案处理"
                    )
            if case["decision"]:
                case["decision_history"].append(case["decision"])
            decision = {
                "decision_id": _new_id("dec"),
                "proposal_id": proposal["proposal_id"],
                "option": proposal["option"],
                "effective_date": effective_date,
                "by": _require(payload, "by"),
                "at": payload.get("at", effective_date),
                "status": "decided",
            }
            case["decision"] = decision
            case["status"] = "decided"
            case["events"].append({"at": decision["at"], "event": "decision:effective"})
            return decision

    def withdraw(self, payload: dict) -> dict:
        """撤回生效决定。已完成的现场动作（关单/交接）不回滚，案件可恢复。"""
        case_id = _require(payload, "case_id")
        reason = _require(payload, "reason")
        with self._lock:
            case = self._require_case(case_id)
            if not case["decision"] or case["status"] not in ("decided", "completed"):
                raise DomainError("当前没有可撤回的生效决定")
            decision = case["decision"]
            decision["status"] = "withdrawn"
            decision["withdrawn_at"] = _require(payload, "at")
            decision["withdraw_reason"] = reason
            # 已完成的分期与现场动作保留为历史；案件回到开放态，允许重新提案决定
            case["status"] = "open"
            case["events"].append(
                {"at": decision["withdrawn_at"], "event": "decision:withdrawn"}
            )
            return {
                "withdrawn": decision["decision_id"],
                "retained": {
                    "closed_work_orders": list(case["closed_work_orders"]),
                    "completed_phases": list(case["completed_phases"]),
                    "handovers": list(case["handovers"]),
                },
                "resumable": True,
            }

    # ------------------------------------------------------------------
    # 分期执行
    # ------------------------------------------------------------------
    def plan_phases(self, payload: dict) -> dict:
        case_id = _require(payload, "case_id")
        phases = _require(payload, "phases")
        with self._lock:
            case = self._require_case(case_id)
            if not case["decision"] or case["status"] != "decided":
                raise DomainError("需要先生效决定才能排分期")
            if case["phases"]:
                raise DomainError("分期计划已存在")
            seen = set()
            normalized = []
            for phase in phases:
                pid = _require(phase, "id")
                if pid in seen:
                    raise DomainError(f"分期编号重复: {pid}")
                seen.add(pid)
                normalized.append({"id": pid, "label": _require(phase, "label")})
            case["phases"] = normalized
            return {"phases": normalized}

    def complete_phase(self, payload: dict) -> dict:
        case_id = _require(payload, "case_id")
        phase_id = _require(payload, "phase_id")
        return self._with_receipt(payload, f"phase:{phase_id}", lambda: self._complete_phase(
            case_id, phase_id, _require(payload, "at")
        ))

    def _complete_phase(self, case_id: str, phase_id: str, at: str) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            if not case["decision"] or case["status"] != "decided":
                raise DomainError("决定未生效或已撤回，不能执行分期")
            if phase_id not in {p["id"] for p in case["phases"]}:
                raise DomainError(f"未知分期: {phase_id}")
            if phase_id in case["completed_phases"]:
                # 不带新回执的重复调用：幂等返回，不重复处置
                return {"phase_id": phase_id, "already_completed": True}
            case["completed_phases"].append(phase_id)
            case["events"].append({"at": at, "event": f"phase:completed:{phase_id}"})
            done = len(case["completed_phases"]) == len(case["phases"])
            if done:
                case["status"] = "completed"
                case["decision"]["status"] = "completed"
            return {"phase_id": phase_id, "completed": True, "all_done": done}

    # ------------------------------------------------------------------
    # 退役与替换交接
    # ------------------------------------------------------------------
    def execute_retirement(self, payload: dict) -> dict:
        case_id = _require(payload, "case_id")
        at = _require(payload, "at")
        return self._with_receipt(payload, "retirement", lambda: self._retire(case_id, at))

    def _retire(self, case_id: str, at: str) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            decision = case["decision"]
            if not decision or case["status"] not in ("decided", "completed"):
                raise DomainError("退役只能在决定生效后执行")
            if case["retirement_executed"]:
                return {"already_executed": True, "closed_work_orders": case["closed_work_orders"]}
            proposal = self._proposal(case, decision["proposal_id"])
            if proposal["option"] != "retire":
                raise DomainError(f"当前决定方案是 {proposal['option']}，不是退役")
            # 关闭全部未结工单（含已报修待修与计划未来执行的），杜绝退役后继续派单；
            # 事故与支出历史原样保留，关闭动作本身也留痕。
            closed = []
            for record in case["timeline"]:
                if record["type"] != "maintenance":
                    continue
                if record["status"] == "open":
                    record["status"] = "closed"
                    record["closed_at"] = at
                    record["close_reason"] = (
                        "设施退役，关闭未来工单" if record["date"] >= at
                        else "设施退役，关闭未结欠单"
                    )
                    closed.append(record["work_order_id"])
            case["closed_work_orders"].extend(
                wid for wid in closed if wid not in case["closed_work_orders"]
            )
            case["retirement_executed"] = True
            all_phases_done = (
                not case["phases"]
                or len(case["completed_phases"]) == len(case["phases"])
            )
            if all_phases_done:
                case["status"] = "completed"
                decision["status"] = "completed"
            case["events"].append({"at": at, "event": "retirement:executed"})
            return {
                "retired": True,
                "closed_future_work_orders": closed,
                "history_retained": {
                    "safety_incidents": self._count(case, "safety"),
                    "maintenance_spending": self._maintenance_spend(case),
                },
                "qrcode_disposition": proposal["qrcode_disposition"],
                "old_asset_disposition": proposal["old_asset_disposition"],
            }

    def execute_handover(self, payload: dict) -> dict:
        case_id = _require(payload, "case_id")
        at = _require(payload, "at")
        return self._with_receipt(payload, "handover", lambda: self._handover(case_id, at))

    def _handover(self, case_id: str, at: str) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            decision = case["decision"]
            if not decision or case["status"] not in ("decided", "completed"):
                raise DomainError("交接只能在决定生效后执行")
            proposal = self._proposal(case, decision["proposal_id"])
            if not proposal["replacements"]:
                raise DomainError("当前方案不包含替换设备，无需交接")
            if case["handover_executed"]:
                return {"already_executed": True, "handovers": case["handovers"]}

            # 原子交接：先在临时结构上校验全部条目，全部通过才落账；
            # 任一替换设备条目有问题则整体不发生。
            old_faults = sorted(
                record["work_order_id"]
                for record in case["timeline"]
                if record["type"] == "maintenance"
            )
            old_incidents = sorted(
                record.get("incident_id", record["record_id"])
                for record in case["timeline"]
                if record["type"] == "safety"
            )
            ledgers = []
            for repl in proposal["replacements"]:
                new_id = repl["new_asset_id"]
                if new_id in self.assets:
                    raise DomainError(f"新资产 {new_id} 已被承接，不能重复建立台账")
                obligations = [dict(o) if isinstance(o, dict) else {"obligation": o}
                               for o in repl["site_obligations"]]
                notices = []
                for notice in repl["cached_notices"]:
                    item = dict(notice) if isinstance(notice, dict) else {"notice_id": notice}
                    item["retarget_to"] = new_id
                    notices.append(item)
                warranties = [dict(w) if isinstance(w, dict) else {"warranty": w}
                              for w in repl["warranties"]]
                ledgers.append({
                    "new_asset_id": new_id,
                    "at": at,
                    "inherited_site_obligations": obligations,
                    "inherited_cached_notices": notices,
                    "inherited_warranties": warranties,
                    # 旧故障、旧事故、旧支出一律不随资产转移
                    "excluded_old_faults": old_faults,
                    "excluded_old_incidents": old_incidents,
                    "maintenance_spending_at_start": 0,
                    "incidents_at_start": 0,
                })

            for ledger in ledgers:
                self.assets[ledger["new_asset_id"]] = {
                    "asset_id": ledger["new_asset_id"],
                    "created_by_case": case_id,
                    "facility_id": case["facility_id"],
                    "since": at,
                    "site_obligations": ledger["inherited_site_obligations"],
                    "cached_notices": ledger["inherited_cached_notices"],
                    "warranties": ledger["inherited_warranties"],
                    "maintenance_records": [],
                    "safety_records": [],
                }
                case["handovers"].append(ledger)
            case["handover_executed"] = True
            case["events"].append({"at": at, "event": "handover:executed"})
            return {"handovers": ledgers, "atomic": True}

    def record_asset_incident(self, asset_id: str, record: dict) -> dict:
        """新资产运行后自身发生的维修/事故，用于验证旧故障不会混入。"""
        with self._lock:
            asset = self.assets.get(asset_id)
            if asset is None:
                raise DomainError(f"资产台账不存在: {asset_id}")
            kind = _require(record, "type")
            if kind not in ("maintenance", "safety"):
                raise DomainError("资产台账只登记 maintenance/safety")
            entry = {"date": _require(record, "date"), **record}
            if kind == "maintenance":
                entry.setdefault("cost", 0)
                asset["maintenance_records"].append(entry)
            else:
                asset["safety_records"].append(entry)
            return {"asset_id": asset_id, "entry": entry}

    # ------------------------------------------------------------------
    # 公共价值排序
    # ------------------------------------------------------------------
    def budget_order(self, payload: dict) -> dict:
        rules = _require(payload, "rules")
        budget = _require(payload, "budget")
        if not rules.get("approved"):
            raise DomainError("只能依据已批准(approved)的公共价值规则排序")
        if "version" not in rules:
            raise DomainError("规则必须带版本号")
        weights = rules.get("weights", {})
        required = {"service_gap", "usage", "safety_risk", "cost_effectiveness"}
        if set(weights) != required:
            raise DomainError(f"权重必须恰好包含 {sorted(required)}")
        if any(float(v) < 0 for v in weights.values()):
            raise DomainError("权重不能为负")
        with self._lock:
            ids = payload.get("case_ids") or list(self.cases)
            scored = [self._score_case(self._require_case(cid), rules) for cid in ids]
            # 并列时：安全风险高者优先，再按最早安装日（老设施优先补位）
            scored.sort(key=lambda r: (-r["score"], -r["safety_priority"], r["install_date"]))
            allocated = 0
            for rank, row in enumerate(scored, start=1):
                row["rank"] = rank
                row["funded"] = allocated + row["requested_amount"] <= budget
                if row["funded"]:
                    allocated += row["requested_amount"]
            round_ = {
                "rules_version": rules["version"],
                "budget": budget,
                "allocated": allocated,
                "order": [
                    {
                        "rank": row["rank"],
                        "case_id": row["case_id"],
                        "facility_id": row["facility_id"],
                        "score": row["score"],
                        "requested_amount": row["requested_amount"],
                        "funded": row["funded"],
                        "usage_evidence_missing": row["usage_evidence_missing"],
                        "adjustments": [],
                        "adjust_reason": None,
                    }
                    for row in scored
                ],
            }
            self.budget_round = round_
            return round_

    def _score_case(self, case: dict, rules: dict) -> dict:
        proposal = case["proposals"][-1] if case["proposals"] else None
        weights = rules["weights"]
        metrics = {
            "service_gap": self._gap_level(case),
            "usage": self._usage_level(case),
            "safety_risk": self._safety_level(case),
            "cost_effectiveness": self._cost_effectiveness(case, proposal),
        }
        # 缺测不按零计：从归一化分母中剔除该指标（中性处理），并显式标注
        usage_missing = metrics["usage"] is None
        applicable = {
            key: value for key, value in metrics.items() if value is not None
        }
        denom = sum(weights[key] for key in applicable) or 1.0
        score = sum(weights[key] * value for key, value in applicable.items()) / denom
        install_date = min(
            (r["date"] for r in case["timeline"] if r["type"] == "installation"),
            default="9999-12-31",
        )
        return {
            "case_id": case["case_id"],
            "facility_id": case["facility_id"],
            "score": round(score, 6),
            "metrics": metrics,
            "usage_evidence_missing": usage_missing,
            "safety_priority": metrics["safety_risk"] or 0.0,
            "install_date": install_date,
            "requested_amount": sum(r["requested_amount"] for r in proposal["replacements"])
            if proposal else 0,
        }

    @staticmethod
    def _gap_level(case: dict) -> float:
        """覆盖缺口越大分越高：取覆盖记录中最大的缺口比例。"""
        levels = [
            float(r["gap_level"])
            for r in case["timeline"]
            if r["type"] == "coverage" and r.get("gap_level") is not None
        ]
        community = case["signoffs"].get(SIGNOFF_COMMUNITY)
        if community and isinstance(community["service_gap_opinion"], dict):
            levels.append(float(community["service_gap_opinion"].get("gap_level", 0)))
        return max(levels, default=0.0)

    @staticmethod
    def _usage_level(case: dict) -> Optional[float]:
        """归一化使用强度；无任何使用记录时返回 None（缺测，不折算为零）。"""
        observations = [r for r in case["timeline"] if r["type"] == "usage"]
        if not observations:
            return None
        latest = max(observations, key=lambda r: r["date"])
        ceiling = max(r.get("ceiling") or 1 for r in observations)
        return min(1.0, float(latest["count"]) / float(ceiling))

    @staticmethod
    def _safety_level(case: dict) -> float:
        return min(
            1.0,
            sum(float(r.get("severity", 1)) for r in case["timeline"] if r["type"] == "safety")
            / 3.0,
        )

    @staticmethod
    def _cost_effectiveness(case: dict, proposal: Optional[dict]) -> Optional[float]:
        """替换请求金额相对累计维修支出越省，分越高（0~1）。"""
        if proposal is None or not proposal["replacements"]:
            return None
        requested = sum(r["requested_amount"] for r in proposal["replacements"])
        spend = DecisionStore._maintenance_spend(case)
        if requested <= 0:
            return 1.0
        return min(1.0, spend / requested)

    def adjust_order(self, payload: dict) -> dict:
        """人工调整候选顺序，必须书面说明理由并留痕。"""
        case_id = _require(payload, "case_id")
        position = int(_require(payload, "position"))
        reason = _require(payload, "reason")
        by = _require(payload, "by")
        with self._lock:
            if not self.budget_round:
                raise DomainError("尚无已形成的候选顺序可调整")
            order = self.budget_round["order"]
            idx = next((i for i, row in enumerate(order) if row["case_id"] == case_id), None)
            if idx is None:
                raise DomainError("案件不在本轮候选中")
            if not 1 <= position <= len(order):
                raise DomainError("目标顺位超出范围")
            row = order.pop(idx)
            order.insert(position - 1, row)
            row["adjustments"].append({"by": by, "reason": reason, "position": position})
            row["adjust_reason"] = reason
            for rank, item in enumerate(order, start=1):
                item["rank"] = rank
            return self.budget_round

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def case_view(self, case_id: str) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            proposal = None
            if case["decision"]:
                proposal = self._proposal(case, case["decision"]["proposal_id"])
            return {
                "case_id": case["case_id"],
                "facility_id": case["facility_id"],
                "status": case["status"],
                "proposal": proposal,
                "signoffs": dict(case["signoffs"]),
                "decision": case["decision"],
                "phases": case["phases"],
                "completed_phases": case["completed_phases"],
                "retirement_executed": case["retirement_executed"],
                "handover_executed": case["handover_executed"],
                "closed_work_orders": case["closed_work_orders"],
                "handovers": list(case["handovers"]),
                "events": list(case["events"]),
            }

    def timeline_view(self, case_id: str) -> dict:
        with self._lock:
            case = self._require_case(case_id)
            grouped: dict[str, list] = defaultdict(list)
            for record in sorted(case["timeline"], key=lambda r: (r["date"], r["record_id"])):
                grouped[record["type"]].append(record)
            usage_records = grouped.get("usage", [])
            return {
                "case_id": case_id,
                "by_type": {kind: grouped.get(kind, []) for kind in EVIDENCE_TYPES},
                "usage_summary": {
                    # 缺测显式标注 no_evidence，绝不写成 0 次使用
                    "evidence_status": "observed" if usage_records else "no_evidence",
                    "observations": len(usage_records),
                    "latest_count": (
                        max(usage_records, key=lambda r: r["date"])["count"]
                        if usage_records else None
                    ),
                },
                "maintenance_spending": self._maintenance_spend(case),
                "open_work_orders": [
                    r["work_order_id"]
                    for r in case["timeline"]
                    if r["type"] == "maintenance" and r["status"] == "open"
                ],
                "safety_incidents": self._count(case, "safety"),
            }

    def funding_trace(self, facility_id: str) -> dict:
        """回答每笔资金为何继续投入、何时停止、居民服务是否补位。"""
        with self._lock:
            case_id = self.facility_index.get(facility_id)
            if case_id is None:
                raise DomainError(f"设施无在办案件: {facility_id}")
            case = self._require_case(case_id)
            decision = case["decision"]
            proposal = self._proposal(case, decision["proposal_id"]) if decision else (
                case["proposals"][-1] if case["proposals"] else None
            )
            retired = case["retirement_executed"]
            funds = []
            active_decision = decision and decision["status"] in ("decided", "completed")
            for record in sorted(
                (r for r in case["timeline"] if r["type"] == "funding"),
                key=lambda r: r["date"],
            ):
                if active_decision and proposal and proposal["option"] == "retire":
                    if retired:
                        continues = False
                        stops_on = decision["effective_date"]
                        why = (
                            f"退役决定自{decision['effective_date']}生效，"
                            "现场退役已执行，未来工单已关闭，资金停止投入"
                        )
                    else:
                        # 决定已生效但现场尚未处置：仅保留收尾投入，生效日为停止界点
                        continues = True
                        stops_on = decision["effective_date"]
                        why = (
                            "退役决定已生效、现场处置待执行，"
                            f"继续投入理由: 生效日{decision['effective_date']}前的收尾支出，"
                            f"退役依据: {proposal['rationale']}"
                        )
                elif active_decision and proposal:
                    # 保留/迁移/改造：按批准方案继续投入，完成全部分期即停止新增
                    all_phases_done = case["phases"] and len(
                        case["completed_phases"]
                    ) == len(case["phases"])
                    continues = not all_phases_done
                    stops_on = None if continues else (
                        case["completed_phases"] and decision["effective_date"]
                    )
                    why = (
                        f"方案为{proposal['option_label']}，继续投入理由: {proposal['rationale']}"
                        if continues else "分期执行完毕，停止新增投入"
                    )
                else:
                    continues = True
                    stops_on = None
                    why = (
                        "原决定已撤回，资金安排随案件恢复待重新决定"
                        if decision and decision["status"] == "withdrawn"
                        else f"方案为{proposal['option_label'] if proposal else '未定'}，"
                             f"继续投入理由: {proposal['rationale'] if proposal else '方案形成中'}"
                    )
                funds.append({
                    "funding_id": record.get("record_id"),
                    "date": record["date"],
                    "source": record.get("source"),
                    "amount": record.get("amount"),
                    "continues": continues,
                    "why": why,
                    "stops_on": stops_on,
                })
            finance = case["signoffs"].get(SIGNOFF_FINANCE)
            community = case["signoffs"].get(SIGNOFF_COMMUNITY)
            backfill = {
                "service_gap_opinion": community["service_gap_opinion"] if community else None,
                "plan": proposal["service_plan"] if proposal else None,
                "replacement_assets": [r["new_asset_id"] for r in proposal["replacements"]]
                if proposal else [],
                "handover_done": case["handover_executed"],
                "covered": case["handover_executed"]
                or (proposal is not None and proposal["option"] == "keep"),
            }
            return {
                "facility_id": facility_id,
                "case_id": case_id,
                "funds": funds,
                "unamortized": {
                    "amount": finance["unamortized_amount"] if finance else None,
                    "disposition": finance["disposition"] if finance else None,
                },
                "service_backfill": backfill,
            }

    def asset_view(self, asset_id: str) -> dict:
        with self._lock:
            asset = self.assets.get(asset_id)
            if asset is None:
                raise DomainError(f"资产台账不存在: {asset_id}")
            return {
                "asset_id": asset["asset_id"],
                "facility_id": asset["facility_id"],
                "since": asset["since"],
                "site_obligations": asset["site_obligations"],
                "cached_notices": asset["cached_notices"],
                "warranties": asset["warranties"],
                "maintenance_records": list(asset["maintenance_records"]),
                "safety_records": list(asset["safety_records"]),
                "maintenance_spending": sum(
                    r.get("cost", 0) for r in asset["maintenance_records"]
                ),
            }

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _with_receipt(self, payload: dict, action: str, fn):
        receipt_id = payload.get("receipt_id")
        with self._lock:
            if receipt_id is not None:
                if receipt_id in self.receipts:
                    # 重复现场回执：原样返回首次结果，绝不重复处置
                    return {
                        **self.receipts[receipt_id]["result"],
                        "duplicate_receipt": True,
                    }
            result = fn()
            if receipt_id is not None:
                self.receipts[receipt_id] = {"action": action, "result": result}
            return result

    @staticmethod
    def _proposal(case: dict, proposal_id: str) -> dict:
        return next(p for p in case["proposals"] if p["proposal_id"] == proposal_id)

    @staticmethod
    def _count(case: dict, kind: str) -> int:
        return sum(1 for r in case["timeline"] if r["type"] == kind)

    @staticmethod
    def _maintenance_spend(case: dict) -> int:
        return sum(
            int(r.get("cost", 0))
            for r in case["timeline"]
            if r["type"] == "maintenance" and r["status"] == "closed"
        )
