"""抽检样本服务：抽样检查、字段通过率和缺陷记录。

对已确认批次做抽样检查，记录字段通过率和缺陷描述，
驳回后批次不得进入交接。支持按比例和行数两种抽样方式。

除了通用的登记/流转动作，本服务还负责抽检样本的**导出证据链**：

* 导出登记为一份带清单摘要与签名的证据，抬头写明本次复核结论和原样本范围；
* 同一批次的后续导出会取代旧证据的签收资格，旧证据仍可回查当时的来源；
* 样本改判会留下改判记录并撤销引用旧结论的证据；
* 交接签收前用证据逐行核对提交的样本行，能发现样本行被替换（即便通过数相同）。
"""
from __future__ import annotations

from typing import Any

from app.services import sample_evidence as ev
from app.services.base import BaseModuleService
from app.services.sample_evidence import evidence_registry


class SampleCheckService(BaseModuleService):
    MODULE = "sample_check"
    ENTITY = "抽检样本"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "样本编号", "关联批次", "抽样比例", "抽样行数", "字段通过率",
        "缺陷描述", "抽检结论", "抽检时间",
    ]
    REQUIRED_FIELDS = ["样本编号", "关联批次", "抽样比例"]
    STATUS_ORDER = ["待抽检", "抽检中", "已通过", "已驳回"]
    ACTION_RULES = {"启动抽检": "抽检中", "通过样本": "已通过", "驳回样本": "已驳回"}
    NEGATIVE_ACTIONS = ["驳回样本"]

    def accept_sample(self, sample_id: int) -> dict[str, Any]:
        """通过抽检样本。"""
        entry = self.get_entry(sample_id)
        if entry is None:
            return {"ok": False, "reason": "样本不存在"}
        self._apply_verdict(entry, "通过", trigger="通过样本")
        return {"ok": True, "sample_id": sample_id, "conclusion": "通过"}

    def reject_sample(self, sample_id: int, defect: str) -> dict[str, Any]:
        """驳回抽检样本，记录缺陷描述。"""
        entry = self.get_entry(sample_id)
        if entry is None:
            return {"ok": False, "reason": "样本不存在"}
        entry["缺陷描述"] = defect
        self._apply_verdict(entry, "驳回", trigger="驳回样本")
        return {"ok": True, "sample_id": sample_id, "defect": defect, "conclusion": "驳回"}

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """复核动作统一走结论写入，确保通用动作接口也会留下改判记录。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action in ("通过样本", "驳回样本"):
            verdict = "通过" if action == "通过样本" else "驳回"
            self._apply_verdict(entry, verdict, trigger=action)
            return entry, f"{self.ENTITY}已{action}"
        return super().run_action(entry_id, action)

    def _apply_verdict(self, entry: dict[str, Any], verdict: str, *, trigger: str) -> None:
        """写入复核结论；与上一结论不同则留改判记录并撤销引用旧结论的证据。"""
        previous = self._effective_verdict(entry)
        entry["抽检结论"] = verdict
        entry["抽检时间"] = ev.now_iso()
        super().run_action(int(entry["id"]), trigger)
        if previous in ("通过", "驳回") and previous != verdict:
            self._record_revision(entry, previous, verdict, trigger)
            self._revoke_evidence_for_sample(int(entry["id"]))

    @staticmethod
    def _effective_verdict(entry: dict[str, Any]) -> str:
        conclusion = str(entry.get("抽检结论", ""))
        if conclusion in ("通过", "驳回"):
            return conclusion
        status = str(entry.get("status", ""))
        return {"已通过": "通过", "已驳回": "驳回"}.get(status, "待定")

    def _record_revision(self, entry: dict[str, Any], previous: str, current: str,
                         trigger: str) -> None:
        evidence_id = evidence_registry.current_for_batch(str(entry.get("关联批次", "")))
        revision = {
            "样本ID": int(entry["id"]),
            "样本编号": entry.get("样本编号", ""),
            "关联批次": entry.get("关联批次", ""),
            "原结论": previous,
            "新结论": current,
            "触发动作": trigger,
            "改判时间": ev.now_iso(),
            "撤销证据": evidence_id or "",
            "缺陷描述": entry.get("缺陷描述", ""),
        }
        evidence_registry.record_revision(revision)

    def _revoke_evidence_for_sample(self, sample_id: int) -> None:
        """撤销所有引用该样本的当前证据；旧导出件不再具备签收资格。"""
        for evidence in evidence_registry.items():
            if evidence.get("状态") == "已撤销":
                continue
            row_ids = {int(row.get("id", 0)) for row in evidence.get("样本行", [])}
            if sample_id in row_ids:
                evidence_registry.revoke(
                    evidence["证据编号"],
                    f"样本 {sample_id} 复核结论已改判，引用旧结论的导出件失效",
                )

    # ── 导出证据 ─────────────────────────────────────────────
    def build_scope(self, filters: dict[str, str] | None = None) -> dict[str, Any]:
        """原样本范围：导出时实际命中的筛选条件，供接收者核对交接范围。"""
        normalized = {key: value for key, value in (filters or {}).items() if value}
        return {"筛选条件": normalized, "说明": "导出件覆盖导出时命中的全部抽检样本行"}

    def create_export_evidence(
        self,
        *,
        filters: dict[str, str] | None = None,
        status: str | None = None,
        rows: list[dict[str, Any]] | None = None,
        channel: str = "download",
    ) -> dict[str, Any]:
        """把一次抽检导出登记为证据，返回证据与可下载的清单内容。"""
        if rows is None:
            rows, _ = self.list_entries(
                filters=filters, status=status, page=1, size=1_000_000)
        scope = self.build_scope(filters)
        if status:
            scope["筛选条件"] = {**scope["筛选条件"], "status": status}
        batches = sorted({str(row.get("关联批次", "")) for row in rows if row.get("关联批次")})
        scope_key = ";".join(batches) or "全部抽检样本"

        evidence_id, version = evidence_registry.allocate(scope_key)
        generated_at = ev.now_iso()
        document = ev.build_manifest_document(
            evidence_id=evidence_id,
            version=version,
            generated_at=generated_at,
            review_conclusion=ev.aggregate_conclusion(rows),
            scope=scope,
            summary=ev.recompute_summary(rows),
            rows=rows,
        )
        csv_text = ev.render_csv(document)
        json_text = ev.render_json(document)
        evidence = {
            "证据编号": evidence_id,
            "模块": self.MODULE,
            "版本": version,
            "生成时间": generated_at,
            "复核结论": document["meta"]["复核结论"],
            "原样本范围": scope,
            "汇总": document["meta"]["汇总"],
            "样本行": document["rows"],
            "批次": batches,
            "范围键": scope_key,
            "格式": ev.MANIFEST_FORMAT,
            "清单摘要": document["meta"]["清单摘要"],
            "签名": document["meta"]["签名"],
            "内容摘要": ev.content_digest(csv_text),
            "清单内容": csv_text,
            "json内容": json_text,
            "状态": "有效",
            "通道": channel,
        }
        evidence_registry.insert(evidence)
        return {"evidence": evidence, "document": document, "csv": csv_text, "json": json_text}

    def get_export_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        return evidence_registry.get(evidence_id)

    def list_export_evidence(self, batch: str | None = None) -> list[dict[str, Any]]:
        items = evidence_registry.items()
        if batch:
            items = [item for item in items if batch in (item.get("批次") or [])]
        return [ev.evidence_view(item) for item in items]

    def sample_revisions(self, sample_id: int | None = None,
                         batch: str | None = None) -> list[dict[str, Any]]:
        return evidence_registry.revisions(sample_id=sample_id, batch=batch)

    def verify_evidence(self, evidence_id: str) -> dict[str, Any]:
        """校验登记在册的导出证据是否仍然自洽。"""
        evidence = evidence_registry.get(evidence_id)
        if evidence is None:
            return {"ok": False, "reason": f"导出证据 {evidence_id} 不存在"}
        document = ev.parse_manifest(evidence["清单内容"])
        result = ev.verify_document(document)
        content_ok = ev.content_digest(evidence["清单内容"]) == evidence["内容摘要"]
        result["checks"].append({
            "name": "原文快照未变",
            "ok": content_ok,
            "detail": "" if content_ok else "登记原文与快照摘要不一致",
        })
        result["ok"] = result["ok"] and content_ok
        result["evidence_id"] = evidence_id
        result["status"] = evidence.get("状态", "有效")
        return result

    def verify_handoff_submission(
        self,
        *,
        evidence_id: str,
        submitted_content: str | bytes | None = None,
        submitted_rows: list[dict[str, Any]] | None = None,
        batch: str | None = None,
    ) -> dict[str, Any]:
        """交接验件：核对签收方手里的导出件与登记证据是否同一份、样本行是否被换过。

        校验顺序：登记状态 → 是否当前版本 → 文件自身完整性 → 与原证据的内容/逐行比对。
        即使替换后的通过数相同，逐行比对也会拦下副本。
        """
        evidence = evidence_registry.get(evidence_id)
        if evidence is None:
            return {"ok": False, "eligible": False, "reason": f"导出证据 {evidence_id} 不存在"}
        if evidence.get("状态") == "已撤销":
            return {
                "ok": False, "eligible": False,
                "reason": f"导出证据 {evidence_id} 已撤销：{evidence.get('撤销原因', '')}",
            }
        target_batch = batch or self._single_batch(evidence)
        if target_batch:
            current = evidence_registry.current_for_batch(target_batch)
            if current and current != evidence_id:
                return {
                    "ok": False, "eligible": False,
                    "reason": (f"导出证据 {evidence_id} 已被 {current} 取代，"
                               f"批次 {target_batch} 的签收应使用最新导出件"),
                }
            if current is None:
                return {
                    "ok": False, "eligible": False,
                    "reason": f"批次 {target_batch} 当前没有有效的导出证据，请重新导出",
                }

        if submitted_content is not None:
            submitted_text = (
                submitted_content.decode("utf-8-sig")
                if isinstance(submitted_content, bytes) else submitted_content
            )
            try:
                submitted = ev.parse_manifest(submitted_text)
            except ValueError as exc:
                return {"ok": False, "eligible": False, "reason": str(exc)}
            if submitted["meta"].get("证据编号") != evidence_id:
                return {
                    "ok": False, "eligible": False,
                    "reason": (f"提交件声明的证据编号 {submitted['meta'].get('证据编号')} "
                               f"与登记证据 {evidence_id} 不一致"),
                }
            # 先逐行比对，确保「样本行被换过」能给出具体定位，而不是笼统的摘要不符。
            if ev.rows_digest(submitted["rows"]) != ev.rows_digest(evidence["样本行"]):
                return {
                    "ok": False,
                    "eligible": False,
                    "reason": "提交件样本行与原证据不一致：" + ev.diagnose_row_replacement(
                        submitted["rows"], evidence["样本行"]),
                    "batch": target_batch,
                }
            own_check = ev.verify_document(submitted)
            if not own_check["ok"]:
                return {
                    "ok": False, "eligible": False,
                    "reason": f"提交件自身校验未通过：{own_check['reason']}",
                    "checks": own_check["checks"],
                }
            if str(submitted["meta"].get("清单摘要")) != evidence["清单摘要"]:
                return {
                    "ok": False, "eligible": False,
                    "reason": "提交件的清单摘要与登记证据不一致（抬头被改动或不是同一份导出件）",
                }
            if ev.content_digest(submitted_text) != evidence["内容摘要"]:
                return {
                    "ok": False, "eligible": False,
                    "reason": "提交件与登记的导出原文不一致（抬头或表头被改动）",
                    "batch": target_batch,
                }
        elif submitted_rows is not None:
            if ev.rows_digest(submitted_rows) != ev.rows_digest(evidence["样本行"]):
                return {
                    "ok": False, "eligible": False,
                    "reason": "提交件样本行与原证据不一致：" + ev.diagnose_row_replacement(
                        submitted_rows, evidence["样本行"]),
                    "batch": target_batch,
                }
        else:
            return {"ok": False, "eligible": False, "reason": "缺少提交件或样本行，无法验件"}

        return {
            "ok": True,
            "eligible": True,
            "reason": "导出件与抽检证据一致，可办理签收",
            "evidence_id": evidence_id,
            "batch": target_batch,
            "review_conclusion": evidence["复核结论"],
            "scope": evidence["原样本范围"],
            "summary": evidence["汇总"],
        }

    @staticmethod
    def _single_batch(evidence: dict[str, Any]) -> str | None:
        batches = evidence.get("批次") or []
        return batches[0] if len(batches) == 1 else None
