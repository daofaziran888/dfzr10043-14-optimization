"""交接包服务：批次打包、清单校验与下游交接。

把已确认批次连同字段映射、校验摘要和清单打包成交接包，
供下游数仓直接消费并回溯来源。退回后批次不得再次交接。

签收侧走「资格核验」：接收方必须同时提交与批次对应的抽检导出证据，
本服务核对证据是最新一版、未撤销、且该批次的抽检样本无驳回，
再把证据编号、清单摘要与复核结论写入交接确认，供事后回查。
"""
from __future__ import annotations

from typing import Any

from app.services import sample_evidence as ev
from app.services.base import BaseModuleService
from app.services.sample_evidence import evidence_registry

ELIGIBILITY_VERSION = "handoff-eligibility/v1"


class HandoffPackService(BaseModuleService):
    MODULE = "handoff_pack"
    ENTITY = "交接包"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "交接编号", "关联批次", "目标系统", "清单文件数", "记录行数",
        "校验摘要", "交接版本", "交接时间",
    ]
    REQUIRED_FIELDS = ["交接编号", "关联批次", "目标系统"]
    STATUS_ORDER = ["待打包", "打包中", "已交接", "交接退回"]
    ACTION_RULES = {"启动打包": "打包中", "确认交接": "已交接", "记录退回": "交接退回"}
    NEGATIVE_ACTIONS = []

    def reject_handoff(self, pack_id: int, reason: str) -> dict[str, Any]:
        """驳回交接包，退回后批次不得进入交接。"""
        entry = self.get_entry(pack_id)
        if entry is None:
            return {"ok": False, "reason": "交接包不存在"}
        self.run_action(pack_id, "记录退回")
        return {"ok": True, "pack_id": pack_id, "reason": reason, "action": "交接退回"}

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """不允许绕过资格核验直接确认交接；签收必须走 signoff。"""
        if action == "确认交接":
            entry = self.get_entry(entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            return None, "确认交接必须通过签收接口提交导出证据完成"
        return super().run_action(entry_id, action)

    def mark_delivered(self, pack_id: int) -> tuple[dict[str, Any] | None, str]:
        """内部打包链路的交付标记，不对外暴露为可签收动作。"""
        return super().run_action(pack_id, "确认交接")

    def get_delivered_packs(self, target_system: str | None = None) -> list[dict[str, Any]]:
        """获取已交接的包列表，可按目标系统筛选。"""
        items, _ = self.list_entries(size=10000)
        delivered = [it for it in items if it.get("status") == "已交接"]
        if target_system:
            delivered = [it for it in delivered if it.get("目标系统") == target_system]
        return delivered

    # ── 签收资格核验 ─────────────────────────────────────────
    def verify_delivery(
        self,
        *,
        evidence_id: str,
        pack_id: int | None = None,
        batch: str | None = None,
        submitted_content: str | bytes | None = None,
        submitted_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """按当前资格核验版本判定交付能否签收。"""
        base = {"eligibility_version": ELIGIBILITY_VERSION, "evidence_id": evidence_id}
        evidence = evidence_registry.get(evidence_id)
        if evidence is None:
            return {**base, "ok": False, "eligible": False,
                    "reason": f"导出证据 {evidence_id} 不存在"}

        pack: dict[str, Any] | None = None
        if pack_id is not None:
            pack = self.get_entry(pack_id)
            if pack is None:
                return {**base, "ok": False, "eligible": False,
                        "reason": f"交接包 {pack_id} 不存在或已归档"}
            if pack.get("status") in ("已交接", "交接退回"):
                return {**base, "ok": False, "eligible": False,
                        "reason": f"交接包 {pack_id} 状态为{pack.get('status')}，不能签收"}
            if pack.get("status") not in ("待打包", "打包中"):
                return {**base, "ok": False, "eligible": False,
                        "reason": f"交接包 {pack_id} 当前状态为{pack.get('status')}，尚不可签收"}
            batch = str(pack.get("关联批次", ""))
        if not batch:
            return {**base, "ok": False, "eligible": False, "reason": "缺少关联批次，无法核验"}

        # 证据必须覆盖该批次，且是该批次当前登记的版本
        if batch not in (evidence.get("批次") or []):
            return {**base, "ok": False, "eligible": False,
                    "reason": f"导出证据 {evidence_id} 不覆盖批次 {batch}"}
        if evidence.get("状态") == "已撤销":
            return {**base, "ok": False, "eligible": False,
                    "reason": f"导出证据 {evidence_id} 已撤销：{evidence.get('撤销原因', '')}"}
        current = evidence_registry.current_for_batch(batch)
        if current != evidence_id:
            return {**base, "ok": False, "eligible": False,
                    "reason": (f"导出证据 {evidence_id} 不是批次 {batch} 的当前版本"
                               f"（当前为 {current or '无'}），请使用最新导出件签收")}

        # 该批次的抽检结论必须为通过；任一驳回即失去资格
        batch_rows = [
            row for row in evidence.get("样本行", [])
            if str(row.get("关联批次", "")) == batch
        ]
        rejected = [
            str(row.get("样本编号", row.get("id", "")))
            for row in batch_rows
            if ev.effective_conclusion(row) == "驳回"
        ]
        if rejected:
            return {**base, "ok": False, "eligible": False,
                    "reason": f"批次 {batch} 的抽检结论为驳回，样本：{'、'.join(rejected)}"}
        if ev.aggregate_conclusion(batch_rows) != "通过":
            return {**base, "ok": False, "eligible": False,
                    "reason": f"批次 {batch} 的抽检结论尚未全部通过，不能签收"}

        # 提交件逐行验真（防止样本行被替换后通过数相同）
        if submitted_content is not None or submitted_rows is not None:
            sample_svc = _sample_service()
            verdict = sample_svc.verify_handoff_submission(
                evidence_id=evidence_id,
                submitted_content=submitted_content,
                submitted_rows=submitted_rows,
                batch=batch,
            )
            if not verdict.get("ok"):
                return {**base, "ok": False, "eligible": False,
                        "reason": verdict.get("reason", "提交件核验未通过"),
                        "batch": batch}

        return {
            **base,
            "ok": True,
            "eligible": True,
            "pack_id": pack_id,
            "batch": batch,
            "reason": f"按 {ELIGIBILITY_VERSION} 核验通过，可办理签收",
            "review_conclusion": evidence["复核结论"],
            "scope": evidence["原样本范围"],
            "summary": evidence["汇总"],
            "manifest_digest": evidence["清单摘要"],
        }

    def signoff(
        self,
        *,
        pack_id: int,
        evidence_id: str,
        submitted_content: str | bytes | None = None,
        submitted_rows: list[dict[str, Any]] | None = None,
        operator: str = "receiver",
    ) -> dict[str, Any]:
        """验件通过后办理签收，并把抽检证据写进交接确认。

        签收必须带上接收方手里的导出件原文或样本行，否则无法证明「表中样本行
        没有被换过」；只查证据编号的签收一律拒绝。
        """
        # 先做与提交件无关的资格核验：证据不存在/已撤销/非当前版本时直接拒绝，
        # 这样拒绝理由指向真正的资格问题，而不是「没交文件」。
        precheck = self.verify_delivery(evidence_id=evidence_id, pack_id=pack_id)
        if not precheck.get("ok"):
            self._write_audit(
                pack_id, evidence_id, "交接签收拒绝",
                f"签收被资格核验拒绝：{precheck.get('reason', '')}", operator,
            )
            return {**precheck, "signed": False}

        if submitted_content is None and submitted_rows is None:
            self._write_audit(
                pack_id, evidence_id, "交接签收拒绝",
                "签收未提交导出件原文或样本行，无法完成交接验件", operator,
            )
            return {
                "ok": False, "eligible": False, "signed": False,
                "eligibility_version": ELIGIBILITY_VERSION, "evidence_id": evidence_id,
                "reason": "签收必须提交导出件原文或样本行，供交接验件核对",
            }
        verdict = self.verify_delivery(
            evidence_id=evidence_id,
            pack_id=pack_id,
            submitted_content=submitted_content,
            submitted_rows=submitted_rows,
        )
        if not verdict.get("ok"):
            self._write_audit(
                pack_id, evidence_id, "交接签收拒绝",
                f"签收被资格核验拒绝：{verdict.get('reason', '')}", operator,
            )
            return {**verdict, "signed": False}

        pack = self.get_entry(pack_id)
        assert pack is not None
        evidence = evidence_registry.get(evidence_id)
        assert evidence is not None
        pack["签收证据"] = evidence_id
        pack["签收清单摘要"] = evidence["清单摘要"]
        pack["签收复核结论"] = evidence["复核结论"]
        pack["签收原样本范围"] = evidence["原样本范围"]
        pack["签收汇总"] = evidence["汇总"]
        pack["签收资格核验"] = ELIGIBILITY_VERSION
        pack["签收时间"] = ev.now_iso()
        self.mark_delivered(pack_id)
        self._write_audit(
            pack_id, evidence_id, "交接签收",
            (f"交接包{pack.get('交接编号')}依据导出证据{evidence_id}"
             f"（版本{evidence['版本']}，摘要{evidence['清单摘要']}，"
             f"复核结论{evidence['复核结论']}）签收批次{pack.get('关联批次')}"),
            operator,
        )
        return {
            **verdict,
            "signed": True,
            "status": pack.get("status"),
            "confirmation": self.get_confirmation(pack_id),
        }

    def get_confirmation(self, pack_id: int) -> dict[str, Any] | None:
        """回查一份交接确认及其关联的抽检导出证据。"""
        pack = self.get_entry(pack_id)
        if pack is None or not pack.get("签收证据"):
            return None
        evidence = evidence_registry.get(str(pack["签收证据"]))
        return {
            "交接编号": pack.get("交接编号"),
            "关联批次": pack.get("关联批次"),
            "状态": pack.get("status"),
            "签收时间": pack.get("签收时间"),
            "签收资格核验": pack.get("签收资格核验"),
            "证据编号": pack.get("签收证据"),
            "清单摘要": pack.get("签收清单摘要"),
            "复核结论": pack.get("签收复核结论"),
            "原样本范围": pack.get("签收原样本范围"),
            "汇总": pack.get("签收汇总"),
            "证据当前状态": evidence.get("状态") if evidence else "已不存在",
            "样本改判": _sample_service().sample_revisions(
                batch=str(pack.get("关联批次", ""))),
        }

    def _write_audit(self, pack_id: int, evidence_id: str, operation: str,
                     summary: str, operator: str) -> None:
        audit_svc = _audit_service()
        rows = audit_svc.list_entries(size=10000)[0]
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        audit_svc.create_entry({
            "条目编号": f"AUDI-H{pack_id}-{next_id}",
            "操作对象": f"交接包.{pack_id}",
            "操作类型": operation,
            "操作者标识": operator,
            "操作时间": ev.now_iso(),
            "变更摘要": f"{summary}（证据{evidence_id}）",
            "关联执行": "",
            "追溯标记": f"handoff-signoff-{pack_id}",
        })


def _sample_service():
    from app.services import service_for
    service = service_for("sample_check")
    assert service is not None
    return service


def _audit_service():
    from app.services import service_for
    service = service_for("audit_trail")
    assert service is not None
    return service
