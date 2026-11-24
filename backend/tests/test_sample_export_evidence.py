"""抽检样本导出证据链的确定性测试。

覆盖题目要求的整条场景：
1. 导出件抬头写明复核结论与原样本范围，汇总可由自己的样本行复算；
2. 中断后按字节区间续传，始终落在同一份快照上，不混写两次复核结论；
3. 复制导出件并替换样本行（通过数不变），交接验件拦下副本，原件仍可签收；
4. 改判后再次下载，两版清单各自可复算；旧导出件失去签收资格但可回查改判；
5. 其他抽检批次照常交接。
"""
from __future__ import annotations

import pytest

from app.main import app
from app.services import sample_evidence as ev
from app.services import service_for
from tests.asgi_client import request

BATCH_A = "CAPT-A"
BATCH_B = "CAPT-B"
BATCH_C = "CAPT-C"


def _new_batch(code: str, rows: str = "120") -> dict:
    svc = service_for("capture_batch")
    entry, _ = svc.create_entry({
        "批次编号": code, "关联执行": "FLOW-0003", "记录行数": rows,
        "采集窗口": "[2026-11-24]", "完整性校验": f"sha256_{code}",
        "批次标签": "file-collect", "上游来源": f"FLOW-0003:{code}",
        "生成时间": ev.now_iso(),
    })
    svc.run_action(entry["id"], "确认批次")
    return entry


def _new_sample(number: str, batch: str, verdict: str, defect: str = "") -> dict:
    svc = service_for("sample_check")
    entry, missing = svc.create_entry({
        "样本编号": number, "关联批次": batch, "抽样比例": "0.2",
        "抽样行数": "10", "字段通过率": "0.9", "缺陷描述": defect,
        "抽检结论": "", "抽检时间": "",
    })
    assert not missing, missing
    svc.run_action(entry["id"], "启动抽检")
    svc.run_action(entry["id"], "通过样本" if verdict == "通过" else "驳回样本")
    return entry


def _new_pack(code: str, batch: str) -> dict:
    svc = service_for("handoff_pack")
    entry, _ = svc.create_entry({
        "交接编号": code, "关联批次": batch, "目标系统": "datalake_downstream",
        "清单文件数": "12", "记录行数": "120", "校验摘要": f"sha256_{code}",
        "交接版本": "v1", "交接时间": ev.now_iso(),
    })
    svc.run_action(entry["id"], "启动打包")
    return entry


def _forge_copy(evidence: dict, replace: dict[str, dict]) -> str:
    """按接收方视角伪造：替换样本行、重算行指纹/汇总/摘要并重新签名。

    这是「副本」能做到的最强篡改——通过数不变，且文件自身校验通过。
    """
    rows = [dict(row) for row in evidence["样本行"]]
    for index, row in enumerate(rows):
        if row.get("样本编号") in replace:
            rows[index] = ev.row_core(replace[row["样本编号"]])
            rows[index][ev.ROW_FINGERPRINT_FIELD] = ev.row_digest(rows[index])
    meta = dict(evidence)
    meta["汇总"] = ev.recompute_summary(rows)
    meta["复核结论"] = ev.aggregate_conclusion(rows)
    digest = ev.manifest_digest(meta, rows)
    meta["清单摘要"] = digest
    meta["签名"] = ev.sign_digest(digest)
    return ev.render_csv({"meta": meta, "rows": rows})


@pytest.fixture()
def batch_with_three_samples():
    _new_batch(BATCH_A)
    _new_batch(BATCH_B)
    _new_batch(BATCH_C)
    jia = _new_sample("SAMP-JIA", BATCH_A, "通过")
    yi = _new_sample("SAMP-YI", BATCH_A, "通过")
    bing = _new_sample("SAMP-BING", BATCH_B, "驳回", "字段错位")
    other = _new_sample("SAMP-C1", BATCH_C, "通过")
    return {"jia": jia, "yi": yi, "bing": bing, "other": other}


class TestManifestSelfDescription:
    def test_manifest_states_conclusion_and_scope(self, batch_with_three_samples):
        svc = service_for("sample_check")
        evidence = svc.create_export_evidence(filters={"关联批次": BATCH_A})["evidence"]
        document = ev.parse_manifest(evidence["清单内容"])
        meta = document["meta"]

        assert meta["复核结论"] == "通过"
        assert meta["原样本范围"]["筛选条件"] == {"关联批次": BATCH_A}
        assert {row["样本编号"] for row in document["rows"]} == {"SAMP-JIA", "SAMP-YI"}

    def test_summary_recomputes_from_own_rows(self, batch_with_three_samples):
        svc = service_for("sample_check")
        evidence = svc.create_export_evidence(filters={"关联批次": BATCH_A})["evidence"]
        document = ev.parse_manifest(evidence["清单内容"])
        assert ev.recompute_summary(document["rows"]) == document["meta"]["汇总"]
        assert document["meta"]["汇总"]["通过"] == 2

    def test_manifest_verifies_end_to_end(self, batch_with_three_samples):
        svc = service_for("sample_check")
        evidence = svc.create_export_evidence()["evidence"]
        result = svc.verify_evidence(evidence["证据编号"])
        assert result["ok"] is True
        assert all(check["ok"] for check in result["checks"])

    def test_export_endpoint_registers_evidence(self, batch_with_three_samples):
        response = request(app, "/api/sample_check/export", query="关联批次=CAPT-A")
        assert response.status == 200
        evidence_id = response.headers["x-evidence-id"]
        listed = request(app, "/api/sample_check/evidence").json()
        assert evidence_id in {item["证据编号"] for item in listed["items"]}
        assert response.headers["x-manifest-digest"]


class TestInterruptedDownload:
    def test_resume_stitches_exact_snapshot(self, batch_with_three_samples):
        svc = service_for("sample_check")
        evidence = svc.create_export_evidence()["evidence"]
        eid = evidence["证据编号"]
        original = request(app, f"/api/sample_check/evidence/{eid}/download").body
        assert original == evidence["清单内容"].encode("utf-8")

        cut = len(original) // 3
        first = request(app, f"/api/sample_check/evidence/{eid}/download",
                        headers={"Range": f"bytes=0-{cut - 1}"})
        assert first.status == 206
        assert first.headers["content-range"] == f"bytes 0-{cut - 1}/{len(original)}"
        second = request(app, f"/api/sample_check/evidence/{eid}/download",
                         headers={"Range": f"bytes={cut}-"})
        assert second.status == 206
        assert first.body + second.body == original

    def test_suffix_range_and_unsatisfiable(self, batch_with_three_samples):
        svc = service_for("sample_check")
        evidence = svc.create_export_evidence()["evidence"]
        eid = evidence["证据编号"]
        total = len(evidence["清单内容"].encode("utf-8"))
        tail = request(app, f"/api/sample_check/evidence/{eid}/download",
                       headers={"Range": "bytes=-50"})
        assert tail.status == 206
        assert tail.body == evidence["清单内容"].encode("utf-8")[-50:]
        out_of_range = request(app, f"/api/sample_check/evidence/{eid}/download",
                               headers={"Range": f"bytes={total + 10}-"})
        assert out_of_range.status == 416

    def test_resume_never_mixes_review_rounds(self, batch_with_three_samples):
        svc = service_for("sample_check")
        first = svc.create_export_evidence(filters={"关联批次": BATCH_A})["evidence"]
        eid = first["证据编号"]
        original = request(app, f"/api/sample_check/evidence/{eid}/download").body

        # 行下载到一半，另一次复核把甲改判为驳回并重新导出
        svc.reject_sample(batch_with_three_samples["jia"]["id"], "复核发现字段错位")
        second = svc.create_export_evidence(filters={"关联批次": BATCH_A})["evidence"]
        assert second["证据编号"] != eid
        assert second["版本"] == first["版本"] + 1

        resumed = request(app, f"/api/sample_check/evidence/{eid}/download",
                          headers={"Range": f"bytes={len(original) // 2}-"})
        stitched = original[: len(original) // 2] + resumed.body
        legacy = ev.parse_manifest(stitched)
        assert stitched == original
        assert legacy["meta"]["复核结论"] == "通过"
        assert legacy["meta"]["证据编号"] == eid
        assert [row["抽检结论"] for row in legacy["rows"]] == ["通过", "通过"]

        fresh = request(app, f"/api/sample_check/evidence/{second['证据编号']}/download").body
        current = ev.parse_manifest(fresh)
        assert current["meta"]["复核结论"] == "驳回"
        assert ev.recompute_summary(current["rows"]) == current["meta"]["汇总"]


class TestHandoffVerification:
    def test_original_export_is_eligible(self, batch_with_three_samples):
        svc = service_for("sample_check")
        handoff = service_for("handoff_pack")
        pack = _new_pack("HAND-A", BATCH_A)
        evidence = svc.create_export_evidence()["evidence"]
        verdict = handoff.verify_delivery(
            evidence_id=evidence["证据编号"], pack_id=pack["id"],
            submitted_content=evidence["清单内容"])
        assert verdict["ok"] is True
        assert verdict["eligible"] is True

    def test_copy_with_swapped_row_but_same_pass_count_is_caught(self, batch_with_three_samples):
        svc = service_for("sample_check")
        handoff = service_for("handoff_pack")
        pack = _new_pack("HAND-A", BATCH_A)
        evidence = svc.create_export_evidence()["evidence"]
        replacement = next(
            row for row in evidence["样本行"] if row["样本编号"] == "SAMP-C1")
        forged = _forge_copy(evidence, {"SAMP-JIA": replacement})

        forged_doc = ev.parse_manifest(forged)
        assert forged_doc["meta"]["汇总"]["通过"] == evidence["汇总"]["通过"]  # 通过数不变
        assert ev.verify_document(forged_doc)["ok"] is True  # 文件自身甚至能自洽

        verdict = handoff.verify_delivery(
            evidence_id=evidence["证据编号"], pack_id=pack["id"],
            submitted_content=forged)
        assert verdict["ok"] is False
        assert "SAMP-JIA" in verdict["reason"]

        signed = handoff.signoff(pack_id=pack["id"], evidence_id=evidence["证据编号"],
                                 submitted_content=forged)
        assert signed["signed"] is False
        assert service_for("handoff_pack").get_entry(pack["id"])["status"] == "打包中"

        ok = handoff.signoff(pack_id=pack["id"], evidence_id=evidence["证据编号"],
                             submitted_content=evidence["清单内容"])
        assert ok["signed"] is True
        assert service_for("handoff_pack").get_entry(pack["id"])["status"] == "已交接"

    def test_swapped_row_detected_even_without_content(self, batch_with_three_samples):
        svc = service_for("sample_check")
        handoff = service_for("handoff_pack")
        pack = _new_pack("HAND-A", BATCH_A)
        evidence = svc.create_export_evidence(filters={"关联批次": BATCH_A})["evidence"]
        forged_rows = [dict(row) for row in evidence["样本行"]]
        other_export = svc.create_export_evidence(filters={"关联批次": BATCH_C})["evidence"]
        forged_rows[0] = ev.row_core(next(
            row for row in other_export["样本行"] if row["样本编号"] == "SAMP-C1"))
        verdict = handoff.verify_delivery(
            evidence_id=evidence["证据编号"], pack_id=pack["id"], submitted_rows=forged_rows)
        assert verdict["ok"] is False
        assert "SAMP-JIA" in verdict["reason"]

    def test_value_tampering_without_resigning_is_caught(self, batch_with_three_samples):
        svc = service_for("sample_check")
        handoff = service_for("handoff_pack")
        pack = _new_pack("HAND-A", BATCH_A)
        evidence = svc.create_export_evidence()["evidence"]
        tampered = evidence["清单内容"].replace("SAMP-YI", "SAMP-JIA")
        verdict = handoff.verify_delivery(
            evidence_id=evidence["证据编号"], pack_id=pack["id"],
            submitted_content=tampered)
        assert verdict["ok"] is False


class TestRejudgement:
    def test_rejudgement_revokes_old_export_and_new_export_fails_signoff(
            self, batch_with_three_samples):
        svc = service_for("sample_check")
        handoff = service_for("handoff_pack")
        pack = _new_pack("HAND-A", BATCH_A)
        first = svc.create_export_evidence()["evidence"]
        assert handoff.verify_delivery(
            evidence_id=first["证据编号"], pack_id=pack["id"])["ok"] is True

        svc.reject_sample(batch_with_three_samples["jia"]["id"], "复核发现字段错位")
        second = svc.create_export_evidence()["evidence"]

        old = handoff.verify_delivery(evidence_id=first["证据编号"], pack_id=pack["id"])
        assert old["ok"] is False
        assert "已撤销" in old["reason"]

        new = handoff.signoff(pack_id=pack["id"], evidence_id=second["证据编号"],
                              submitted_content=second["清单内容"])
        assert new["signed"] is False
        assert "驳回" in new["reason"]
        assert handoff.get_entry(pack["id"])["status"] == "打包中"

    def test_both_versions_recompute_their_own_pass_counts(self, batch_with_three_samples):
        svc = service_for("sample_check")
        first = svc.create_export_evidence(filters={"关联批次": BATCH_A})["evidence"]
        svc.reject_sample(batch_with_three_samples["jia"]["id"], "复核发现字段错位")
        second = svc.create_export_evidence(filters={"关联批次": BATCH_A})["evidence"]

        legacy = ev.parse_manifest(first["清单内容"])
        current = ev.parse_manifest(second["清单内容"])
        assert ev.recompute_summary(legacy["rows"]) == legacy["meta"]["汇总"]
        assert ev.recompute_summary(current["rows"]) == current["meta"]["汇总"]
        assert (legacy["meta"]["汇总"]["通过"], legacy["meta"]["汇总"]["驳回"]) == (2, 0)
        assert (current["meta"]["汇总"]["通过"], current["meta"]["汇总"]["驳回"]) == (1, 1)
        assert ev.verify_document(legacy)["ok"] is True
        assert ev.verify_document(current)["ok"] is True

    def test_legacy_export_still_shows_original_scope_and_rejudgement(
            self, batch_with_three_samples):
        svc = service_for("sample_check")
        first = svc.create_export_evidence(filters={"关联批次": BATCH_A})["evidence"]
        svc.reject_sample(batch_with_three_samples["jia"]["id"], "复核发现字段错位")

        legacy = ev.parse_manifest(first["清单内容"])
        assert legacy["meta"]["原样本范围"]["筛选条件"] == {"关联批次": BATCH_A}
        assert [row["抽检结论"] for row in legacy["rows"]] == ["通过", "通过"]

        record = request(app, f"/api/sample_check/evidence/{first['证据编号']}/revisions").json()
        assert record["revisions"][0]["样本编号"] == "SAMP-JIA"
        assert record["revisions"][0]["原结论"] == "通过"
        assert record["revisions"][0]["新结论"] == "驳回"

    def test_rejudgement_of_other_batch_does_not_block_handoff(self, batch_with_three_samples):
        svc = service_for("sample_check")
        handoff = service_for("handoff_pack")
        _new_batch("CAPT-D", "40")
        _new_sample("SAMP-D1", "CAPT-D", "通过")
        pack_d = _new_pack("HAND-D", "CAPT-D")

        svc.reject_sample(batch_with_three_samples["other"]["id"], "下游复核改判")

        # 另一批次的签收不受这次改判影响：用专门覆盖该批次的导出件签收
        latest = svc.create_export_evidence(filters={"关联批次": "CAPT-D"})["evidence"]
        signed = handoff.signoff(pack_id=pack_d["id"], evidence_id=latest["证据编号"],
                                 submitted_content=latest["清单内容"])
        assert signed["signed"] is True
        assert handoff.get_entry(pack_d["id"])["status"] == "已交接"

        # 被改判的批次 C 自己则失去签收资格
        pack_c = _new_pack("HAND-C", BATCH_C)
        batch_c_export = svc.create_export_evidence(filters={"关联批次": BATCH_C})["evidence"]
        verdict = handoff.verify_delivery(
            evidence_id=batch_c_export["证据编号"], pack_id=pack_c["id"])
        assert verdict["ok"] is False
        assert "驳回" in verdict["reason"]


class TestHandoffConfirmation:
    def test_confirmation_links_export_evidence(self, batch_with_three_samples):
        svc = service_for("sample_check")
        handoff = service_for("handoff_pack")
        pack = _new_pack("HAND-A", BATCH_A)
        evidence = svc.create_export_evidence(filters={"关联批次": BATCH_A})["evidence"]
        signed = handoff.signoff(pack_id=pack["id"], evidence_id=evidence["证据编号"],
                                 submitted_content=evidence["清单内容"])
        assert signed["signed"] is True

        confirmation = request(app, f"/api/handoff_pack/{pack['id']}/confirmation").json()
        assert confirmation["证据编号"] == evidence["证据编号"]
        assert confirmation["清单摘要"] == evidence["清单摘要"]
        assert confirmation["复核结论"] == "通过"
        assert confirmation["原样本范围"]["筛选条件"] == {"关联批次": BATCH_A}
        assert confirmation["签收资格核验"] == "handoff-eligibility/v1"

    def test_revoked_evidence_cannot_sign_new_batch(self, batch_with_three_samples):
        svc = service_for("sample_check")
        handoff = service_for("handoff_pack")
        pack = _new_pack("HAND-A", BATCH_A)
        evidence = svc.create_export_evidence(filters={"关联批次": BATCH_A})["evidence"]
        svc.reject_sample(batch_with_three_samples["jia"]["id"], "复核发现字段错位")

        response = request(
            app, f"/api/handoff_pack/{pack['id']}/signoff", method="POST",
            body={"evidence_id": evidence["证据编号"]})
        assert response.status == 200
        payload = response.json()
        assert payload["signed"] is False
        assert payload["eligibility_version"] == "handoff-eligibility/v1"
        assert "已撤销" in payload["reason"]

        audit = service_for("audit_trail").query_by_trace(f"handoff-signoff-{pack['id']}")
        assert audit and audit[-1]["操作类型"] == "交接签收拒绝"

    def test_superseded_evidence_cannot_sign(self, batch_with_three_samples):
        svc = service_for("sample_check")
        handoff = service_for("handoff_pack")
        pack = _new_pack("HAND-A", BATCH_A)
        first = svc.create_export_evidence(filters={"关联批次": BATCH_A})["evidence"]
        # 第二次导出（内容相同但版本推进）取代第一次
        second = svc.create_export_evidence(filters={"关联批次": BATCH_A})["evidence"]
        verdict = handoff.verify_delivery(
            evidence_id=first["证据编号"], pack_id=pack["id"])
        assert verdict["ok"] is False
        assert second["证据编号"] in verdict["reason"]
        assert handoff.verify_delivery(
            evidence_id=second["证据编号"], pack_id=pack["id"])["ok"] is True


class TestDirectActionBypass:
    def test_generic_action_cannot_mark_delivered(self, batch_with_three_samples):
        handoff = service_for("handoff_pack")
        pack = _new_pack("HAND-A", BATCH_A)
        response = request(
            app, f"/api/handoff_pack/{pack['id']}/actions", method="POST",
            body={"action": "确认交接"})
        payload = response.json()
        assert payload["ok"] is False
        assert "签收接口" in payload["message"]
        assert handoff.get_entry(pack["id"])["status"] == "打包中"
