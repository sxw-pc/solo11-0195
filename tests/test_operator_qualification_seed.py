"""OP-7 操作人员资格与逐介质授权台账验证。

五组硬验证：

1. 种子虚增 48 小时：朴素相加仍是 48，但同一真实分钟只计一次 -> 24；
2. 本地完整路线 A：重叠只计一次 + 16h 不重叠补课 + O2/N2 分别实操，
   全部达成前不得独立操作，达成后获得较宽的 O2/N2 授权且生效较晚；
3. 互认有限范围路线 B：EXT-7 验真后在有效期内只给 O2，N2 仍是 trainee，
   身份/签名/有效期/介质任一不匹配即拒绝，且不派生检验员角色；
4. 两角色分离：同一瓶的充装操作员与检验签字人必须是不同自然人；
5. 迟到撤销：签到撤销/考试复核/机构失效只撤销尚未执行的排班授权，
   已完成充装与当时依据原样保留，台账只追加、不覆盖旧证据。

运行：``python -B -m pytest -q -p no:cacheprovider``
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from cylindertrail.training_credit import (
    hours,
    raw_hours,
    valid_sessions,
    hours_available_at,
    hours_threshold_reached_at,
    backfill_conflicts,
)
from cylindertrail.operator_authority import (
    AUTHORIZED,
    TRAINEE,
    LIMITED,
    EXPIRED,
    REVOKED,
    SeparationViolation,
    decide,
    verify_reciprocal,
    sign_credential,
    check_separation,
)
from cylindertrail.qualification_capture import (
    QualificationLedger,
    build_ledger,
    ledger_from_fixture,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "operator-qualification.json"

EARLY = "2026-11-26T12:00:00"          # 重叠 24h 之后、补课时段之中
HOURS_DONE = "2026-11-27T16:05:00"      # 40h 已真实达成且已录入
O2_EXAM_DONE = "2026-11-27T17:00:00"    # O2 实操已过、N2 未考
ROUTE_A_DONE = "2026-11-27T18:00:00"    # N2 实操也已通过
DEC_O2_SHIFT = "2026-12-02T08:00:00"
DEC_N2_SHIFT = "2026-12-03T08:00:00"
RECONCILE_AT = "2026-11-30T12:00:00"


@pytest.fixture(scope="module")
def data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture()
def route_b(data):
    """只有重叠签到 + EXT-7 O2 互认的路线 B 台账。"""
    return build_ledger(data, include_route_a=False)


@pytest.fixture()
def route_a(data):
    """重叠签到 + 16h 补课 + 两介质实操的纯本地路线 A（不含互认/排班）。"""
    return build_ledger(data, route="A", include_credential=False,
                        include_fills=False, include_shifts=False,
                        include_inspections=False)


# --------------------------------------------------------------------------- #
# 1. 种子虚增 48 小时
# --------------------------------------------------------------------------- #
def test_seed_raw_sum_still_shows_inflated_48(data):
    op = data["operator"]
    # 两张签到覆盖完全相同的一天 —— 种子缺陷的现场必须保留可观察。
    assert op["sessions"][0]["start"] == op["sessions"][1]["start"]
    assert op["sessions"][0]["end"] == op["sessions"][1]["end"]
    assert raw_hours(op["sessions"]) == 48
    # 把第二张记账时数改成 10，朴素合计降到 34（原种子测试记录的现象）。
    less = [op["sessions"][0], {**op["sessions"][1], "credited_hours": 10}]
    assert raw_hours(less) == 34


def test_seed_same_minute_counts_once_and_proof_is_segmentwise(data):
    op = data["operator"]
    # 区间并集：完全重叠的 24h 只能计 24，而不是 48。
    assert hours(op["sessions"]) == 24
    # 两段签到都具备授课者资格/签到/离场/课程有效期证明，故 24h 全部有效。
    proven = valid_sessions(op["sessions"],
                            instructors=data["instructors"],
                            courses=data["courses"])
    assert {row["id"] for row in proven} == {"S1", "S2"}
    assert hours(proven) == 24
    # 40h 门槛远未达到，且实操未过：种子 OP-7 不得获得本站 O2/N2 授权。
    decision = decide(data, at=EARLY)
    assert decision["N2"] == TRAINEE
    # 去掉外部互认后，O2 同样只是 trainee（24h + 无实操）。
    local_only = decide(op, at=EARLY, site_rule=data["site_rule"],
                        instructors=data["instructors"], courses=data["courses"])
    assert set(local_only.media.values()) == {TRAINEE}
    assert local_only != AUTHORIZED


def test_seed_credited_hours_mutation_no_longer_flips_authority(data):
    op = data["operator"]
    less_op = {**op, "sessions": [
        op["sessions"][0], {**op["sessions"][1], "credited_hours": 10}]}
    # 旧逻辑：合计 34 < 40 -> trainee（时数改动直接改变授权）。
    assert raw_hours(less_op["sessions"]) == 34
    # 新逻辑按真实区间去重，第二张仍是同一整天：有效时数依旧只有 24，
    # 结论稳定为 trainee，而不是被记账数字任意拨动。
    assert hours(less_op["sessions"]) == 24
    assert decide(less_op, at=EARLY) == TRAINEE


def test_segment_proof_failures_are_visible(data):
    op = data["operator"]
    # 缺少签到/离场与课程引用的裸记录不能作为“逐段可证”的有效学时。
    from cylindertrail.training_credit import prove_session
    broken = {"id": "SX", "start": "2026-11-28T08:00:00",
              "end": "2026-11-28T16:00:00", "instructor": "I1"}
    report = prove_session(broken, instructors=data["instructors"],
                           courses=data["courses"])
    assert report["valid"] is False
    assert any("签到" in r for r in report["reasons"])
    assert any("离场" in r for r in report["reasons"])
    assert any("课程" in r for r in report["reasons"])
    # 记账时数超过真实时段同样被拒。
    inflated = {**op["sessions"][0], "id": "SBAD", "credited_hours": 99}
    bad_report = prove_session(inflated, instructors=data["instructors"],
                               courses=data["courses"])
    assert any("超过真实时段" in r for r in bad_report["reasons"])


# --------------------------------------------------------------------------- #
# 2. 本地完整路线 A：更晚生效，但 O2/N2 双介质完整范围
# --------------------------------------------------------------------------- #
def test_route_a_union_hours_reach_exactly_40(data, route_a):
    sessions = data["operator"]["sessions"] + data["route_a"]["additional_sessions"]
    assert raw_hours(sessions) == 64          # 24+24+8+8 的朴素虚增口径
    assert hours(sessions) == 40               # 重叠只计一次后的真实口径
    reached = hours_threshold_reached_at(sessions, 40)
    assert reached is not None
    assert reached.isoformat() == "2026-11-27T16:00:00+00:00"
    # 台账口径一致。
    assert route_a.decision("OP-7", at=HOURS_DONE).details["O2"]["local"]["hours"] == 40


def test_route_a_nothing_independent_before_all_items_pass(route_a):
    # 补课时段尚未凑满 40h。
    d = route_a.decision("OP-7", at="2026-11-27T15:59:00")
    assert d == TRAINEE and d.authorized_media == []
    # 40h 达成、理论题 96 分通过，但实操尚未考：一道理论题不能授权介质。
    d = route_a.decision("OP-7", at=HOURS_DONE)
    assert d == TRAINEE
    joined = " ".join(d.reasons)
    assert "实操" in joined and "设备项" in joined
    # 只过 O2 设备项：只能 O2，N2 仍 trainee —— 两介质项目分别覆盖。
    d = route_a.decision("OP-7", at=O2_EXAM_DONE)
    assert d.status == LIMITED
    assert d["O2"] == AUTHORIZED and d["N2"] == TRAINEE
    assert route_a.can_operate("OP-7", "O2", at=O2_EXAM_DONE)
    assert not route_a.can_operate("OP-7", "N2", at=O2_EXAM_DONE)


def test_route_a_full_station_authority_after_n2_item(route_a):
    d = route_a.decision("OP-7", at=ROUTE_A_DONE)
    assert d == AUTHORIZED
    assert d.authorized_media == ["N2", "O2"]
    assert route_a.can_operate("OP-7", "N2", at=ROUTE_A_DONE)
    assert route_a.can_operate("OP-7", "O2", at=ROUTE_A_DONE)
    # 独立考官不是 OP-7 本人，且考官资格在有效期内。
    o2_basis = d.details["O2"]["local"]["practical"]
    assert o2_basis["independence_ok"] and o2_basis["examiner"] == "EX-1"


def test_route_a_makeup_cannot_be_backdated_onto_finished_fill():
    # 补课在 11-28 才录入，却声称时段在 11-26 上午（一笔已完成充装之前）。
    makeup = {"id": "S9", "start": "2026-11-26T06:00:00",
              "end": "2026-11-26T10:00:00", "credited_hours": 4,
              "recorded_at": "2026-11-28T10:00:00"}
    fills = [{"id": "F-OLD", "filled_at": "2026-11-26T10:00:00"}]
    assert backfill_conflicts([makeup], fills)
    # 以充装时刻为“已知时点”重算，这张倒填补课因录入时间晚而贡献 0 小时。
    assert hours_available_at([makeup], "2026-11-26T10:00:00",
                               knowledge_at="2026-11-26T10:00:00") == 0
    # 正常课后录入（录入晚于课程结束但早于后续充装）不受影响。
    normal = {**makeup, "id": "S10",
              "start": "2026-11-25T06:00:00", "end": "2026-11-25T10:00:00",
              "recorded_at": "2026-11-25T10:05:00"}
    assert hours_available_at([normal], "2026-11-26T10:00:00",
                               knowledge_at="2026-11-26T10:00:00") == 4


# --------------------------------------------------------------------------- #
# 3. 互认有限范围路线 B：立即但较窄的 O2
# --------------------------------------------------------------------------- #
def test_route_b_grants_only_o2_during_validity(data, route_b):
    report = verify_reciprocal(data["reciprocal_credential"], person="OP-7",
                               at="2026-11-26T09:00:00", issuers=data["issuers"])
    assert report["valid"] and report["media"] == ["O2"]
    # 充装时刻：O2 可独立操作，N2 继续 trainee；总体为 limited。
    d = route_b.decision("OP-7", at="2026-11-26T10:00:00")
    assert d.status == LIMITED
    assert d["O2"] == AUTHORIZED and d["N2"] == TRAINEE
    assert route_b.authorized_media("OP-7", at="2026-11-26T10:00:00") == ["O2"]
    # 有效期内的任意当前班次结论稳定。
    assert route_b.can_operate("OP-7", "O2", at="2026-12-31T23:00:00")
    assert not route_b.can_operate("OP-7", "N2", at="2026-12-31T23:00:00")


def test_route_b_expiry_changes_status(data, route_b):
    d = route_b.decision("OP-7", medium="O2", at="2027-02-02T08:00:00")
    assert d.status == EXPIRED
    assert not route_b.can_operate("OP-7", "O2", at="2027-02-02T08:00:00")


@pytest.mark.parametrize("mutation,expected_fragment", [
    ({"operator": "OP-9"}, "身份不匹配"),
    ({"identity_verified": False}, "实名"),
    ({"recognized": False}, "未被本站承认"),
    ({"issuer": "UNKNOWN-ISSUER"}, "签名"),
])
def test_reciprocal_rejects_identity_recognition_failures(data, mutation,
                                                           expected_fragment):
    cred = {**data["reciprocal_credential"], **mutation}
    report = verify_reciprocal(cred, person="OP-7", at="2026-11-26",
                               issuers=data["issuers"])
    assert report["valid"] is False
    assert any(expected_fragment in reason for reason in report["reasons"])


def test_reciprocal_rejects_tampered_signature(data):
    cred = {**data["reciprocal_credential"], "signature": "sha256=" + "0" * 64}
    report = verify_reciprocal(cred, person="OP-7", at="2026-11-26",
                               issuers=data["issuers"])
    assert report["valid"] is False
    assert any("签名" in r for r in report["reasons"])


def test_reciprocal_medium_scope_is_exact(data):
    # 即使一张真证书写的是 N2，也只给 N2，不会顺便给 O2。
    n2_cred = {**data["reciprocal_credential"], "id": "EXT-7N2", "medium": "N2"}
    n2_cred["signature"] = sign_credential(n2_cred,
                                           data["issuers"]["EXT-AUTH"]["secret"])
    report = verify_reciprocal(n2_cred, person="OP-7", at="2026-11-26",
                               issuers=data["issuers"])
    assert report["valid"] and report["media"] == ["N2"]
    # 而 fixture 中的 EXT-7 明确只标 O2：任何查询都不派生 N2。
    report_o2 = verify_reciprocal(data["reciprocal_credential"], person="OP-7",
                                  at="2026-11-26", issuers=data["issuers"])
    assert "N2" not in report_o2["media"]


def test_reciprocal_never_confers_inspector_role(route_b, data):
    assert route_b.has_inspector_role("OP-7") is False
    from cylindertrail.operator_authority import roles_for
    # 即便带着 EXT-7 互认，OP-7 在本站检验员名册中仍无角色。
    assert roles_for("OP-7", data["inspectors"], at="2026-11-26") == set()
    # INS-1 则具备本站独立检验员角色。
    assert route_b.has_inspector_role("INS-1") is True


# --------------------------------------------------------------------------- #
# 4. 同一瓶作业：操作员与检验签字人分离
# --------------------------------------------------------------------------- #
def test_operator_and_inspector_must_differ_for_same_bottle(route_b):
    # CYL-7001 的检验签字 INS-1 来自台账检验事件，充装操作是 OP-7。
    result = route_b.check_separation(bottle_id="CYL-7001", operator="OP-7")
    assert result["separated"] is True
    assert result["inspector"] == "INS-1"
    assert result["inspector_qualified"] is True
    # 同一自然人不能既操作又检验签字 —— 直接拒绝并抛错。
    violation = route_b.check_separation(operator="OP-7", inspector="OP-7",
                                         bottle_id="CYL-SELF")
    assert violation["separated"] is False
    with pytest.raises(SeparationViolation):
        route_b.assert_separated(operator="OP-7", inspector="OP-7")
    # 缺少任一方签字时结论不可验证，不能默认放行。
    missing = check_separation(operator="OP-7", bottle_id="CYL-NONE")
    assert missing["separated"] is False and missing["verifiable"] is False


def test_external_o2_scope_does_not_include_self_inspection(data):
    # 路线 B 下 OP-7 可以充装 O2，但不能用“互认”为自己的瓶子做检验签字。
    ledger = build_ledger(data, include_route_a=False)
    assert ledger.can_operate("OP-7", "O2", at="2026-11-26T10:00:00")
    assert not ledger.has_inspector_role("OP-7")
    with pytest.raises(SeparationViolation):
        ledger.assert_separated(operator="OP-7", inspector="OP-7",
                                bottle_id="CYL-7001")


# --------------------------------------------------------------------------- #
# 5. 迟到撤销：撤未来排班，保留既往充装与当时依据
# --------------------------------------------------------------------------- #
def test_issuer_invalidation_revokes_future_o2_shift_only(data):
    ledger = build_ledger(data, include_route_a=False)
    fill_before = copy.deepcopy(ledger.audit_fill("FILL-7001")["record"])

    # 11-30 外部发证机构失效（迟到事件）。
    ledger.invalidate_issuer("EXT-AUTH", at="2026-11-30T09:00:00")
    result = ledger.reconcile_shifts(as_of=RECONCILE_AT)

    # 唯一原已授权的未来 O2 排班被撤销；从未授权的 N2 排班只是 blocked。
    assert [row["target"] for row in result["revoked"]] == ["SHIFT-O2-1202"]
    assert result["blocked"] == ["SHIFT-N2-1203"]
    view = ledger.shift_view("SHIFT-O2-1202", as_of=RECONCILE_AT)
    assert view["current_status"] == "authorization_revoked"
    assert view["medium_status"]["O2"] == REVOKED
    assert not ledger.can_operate("OP-7", "O2", at=DEC_O2_SHIFT)
    # 对账幂等：再次执行不产生第二条撤销事件。
    again = ledger.reconcile_shifts(as_of=RECONCILE_AT)
    assert again["revoked"] == []


def test_completed_fill_and_its_basis_survive_late_revocation(data):
    ledger = build_ledger(data, include_route_a=False)
    fill_before = copy.deepcopy(next(e for e in ledger.events
                                     if e.get("id") == "FILL-7001"))
    event_count_before = len(ledger)

    ledger.invalidate_issuer("EXT-AUTH", at="2026-11-30T09:00:00")
    ledger.reconcile_shifts(as_of=RECONCILE_AT)

    # 旧充装记录与撤销前逐字段一致：没有被覆盖或删除。
    fill_after = next(e for e in ledger.events if e.get("id") == "FILL-7001")
    assert fill_after == fill_before
    assert len(ledger) > event_count_before  # 只允许追加新证据/撤销事件

    audit = ledger.audit_fill("FILL-7001")
    assert audit["stored_permitted"] is True
    # 当时依据明确是 EXT-7 的 O2 互认，而不是 40h 本地路线。
    assert audit["basis"]["media"]["O2"] == AUTHORIZED
    assert any(b.get("credential") == "EXT-7"
               for b in audit["basis"]["bases"]["O2"])
    # 追查视图同时指出该依据事后因机构失效而被波及。
    assert any(item["target"] == "EXT-7" and item.get("via_issuer") == "EXT-AUTH"
               for item in audit["later_revoked_basis"])
    # 既往充装仍是“已完成”，不会被改写成“从未作业”。
    assert ledger.shift_view("SHIFT-EARLY-O2", as_of=RECONCILE_AT)["current_status"] == "completed"


def test_late_exam_review_hits_n2_but_local_o2_survives(data):
    # 完整路线 A（含 EXT-7 与全部排班）。
    ledger = build_ledger(data, route="A")
    assert ledger.can_operate("OP-7", "N2", at=DEC_N2_SHIFT)
    assert ledger.can_operate("OP-7", "O2", at=DEC_O2_SHIFT)

    # 11-29 N2 实操复核改判不合格。
    ledger.review_exam("PE-N2-7", passed=False, at="2026-11-29T10:00:00")
    result = ledger.reconcile_shifts(as_of=RECONCILE_AT)
    revoked_targets = [row["target"] for row in result["revoked"]]
    assert "SHIFT-N2-1203" in revoked_targets
    # O2 设备项考核未被推翻，O2 12 月排班不动。
    assert "SHIFT-O2-1202" not in revoked_targets
    assert ledger.can_operate("OP-7", "O2", at=DEC_O2_SHIFT)
    assert ledger.decision("OP-7", medium="N2", at=DEC_N2_SHIFT).status == REVOKED

    # 即便随后外部机构也失效，O2 仍有独立的本地路线 A 依据 —— 两路后果不同。
    ledger.invalidate_issuer("EXT-AUTH", at="2026-11-30T09:00:00")
    ledger.reconcile_shifts(as_of=RECONCILE_AT)
    assert ledger.can_operate("OP-7", "O2", at=DEC_O2_SHIFT)
    o2 = ledger.decision("OP-7", medium="O2", at=DEC_O2_SHIFT)
    assert any(b["route"] == "local" for b in o2.details["O2"]["bases"])


def test_late_signin_revocation_drops_hours_and_revokes_future_shifts(data):
    # 纯本地路线 + 排班（不带外部互认），两张 12 月排班原本都已授权。
    ledger = build_ledger(data, route="A", include_credential=False)
    assert ledger.can_operate("OP-7", "O2", at=DEC_O2_SHIFT)
    assert ledger.can_operate("OP-7", "N2", at=DEC_N2_SHIFT)

    # 后到的签到撤销：唯一的 8h 补课时段 S3 被撤，非重叠学时 40 -> 32。
    ledger.revoke_training("S3", at="2026-11-28T15:00:00",
                           reason="签到册系补签且无现场记录")
    local = ledger.decision("OP-7", medium="O2", at=DEC_O2_SHIFT)
    assert local.details["O2"]["local"]["hours"] == 32
    assert not ledger.can_operate("OP-7", "O2", at=DEC_O2_SHIFT)
    result = ledger.reconcile_shifts(as_of="2026-11-29T09:00:00")
    assert {row["target"] for row in result["revoked"]} == {
        "SHIFT-O2-1202", "SHIFT-N2-1203"}
    # 已完成的 11-28 排班属于历史，不被对账回改。
    assert ledger.shift_view("SHIFT-O2-1128",
                             as_of="2026-11-29T09:00:00")["executed"] is True


def test_full_fixture_ledger_replays_and_keeps_history(data):
    ledger = ledger_from_fixture(data)
    assert isinstance(ledger, QualificationLedger)
    # 台账可序列化/重载，事件 seq 与内容保持一致。
    restored = QualificationLedger.from_dict(json.loads(ledger.dumps()))
    assert restored.events == ledger.events
    # 重放后执行对账，未来排班被迟到证据处理，历史充装依据仍在。
    restored.reconcile_shifts(as_of=RECONCILE_AT)
    audit = restored.audit_fill("FILL-7001")
    assert audit["basis"]["media"]["O2"] == AUTHORIZED
    assert audit["record"]["operator"] == "OP-7"
    # 逐人员的当前名册给出 OP-7 的介质范围，而不是瓶样本/纯度结论。
    roster = restored.roster(at=ROUTE_A_DONE)
    assert "OP-7" in roster


def test_main_result_is_when_and_which_media_for_operator(data):
    # 同一 OP-7 沿两条合规路线，在不同时点得到不同介质范围 —— 这是主营结果。
    route_b = build_ledger(data, include_route_a=False)
    assert route_b.authorized_media("OP-7", at="2026-11-26T10:00:00") == ["O2"]

    route_a = build_ledger(data, route="A", include_credential=False,
                           include_fills=False, include_shifts=False)
    assert route_a.authorized_media("OP-7", at=O2_EXAM_DONE) == ["O2"]
    assert route_a.authorized_media("OP-7", at=ROUTE_A_DONE) == ["N2", "O2"]

    timeline = route_a.scope_timeline("OP-7")
    assert timeline and all({"at", "authorized_media", "status"} <= set(entry)
                            for entry in timeline)


# --------------------------------------------------------------------------- #
# 连续性回归：原种子测试同名函数，结论已被纠正
# --------------------------------------------------------------------------- #
def test_overlapping_classes_grant_unearned_authority(data):
    operator = data["operator"]
    # 旧实现直接相加得到 48 并误判 authorized；现在同一分钟只计一次（24h），
    # 实操未过 -> 绝不能返回 authorized。
    assert raw_hours(operator["sessions"]) == 48
    assert hours(operator["sessions"]) == 24
    assert decide(data, at=EARLY) != AUTHORIZED
    less = {**operator, "sessions": [
        operator["sessions"][0],
        {**operator["sessions"][1], "credited_hours": 10}]}
    assert raw_hours(less["sessions"]) == 34
    assert decide(less, at=EARLY) == TRAINEE


def test_late_exam_review_removes_operator_from_current_shift(data):
    ledger = build_ledger(data, route="A", include_shifts=False)
    ledger.schedule_shift({
        "id": "SHIFT-CURRENT-N2", "operator": "OP-7", "medium": "N2",
        "start": "2026-11-29T08:00:00", "end": "2026-11-29T16:00:00",
        "status": "scheduled"})
    at_nine = ledger.shift_view("SHIFT-CURRENT-N2",
                                as_of="2026-11-29T09:00:00")
    assert at_nine["in_progress"] is True
    assert at_nine["all_authorized"] is True

    ledger.review_exam("PE-N2-7", passed=False, at="2026-11-29T10:00:00")
    result = ledger.reconcile_shifts(as_of="2026-11-29T11:00:00")
    assert [row["target"] for row in result["revoked"]] == ["SHIFT-CURRENT-N2"]
    view = ledger.shift_view("SHIFT-CURRENT-N2", as_of="2026-11-29T11:00:00")
    assert view["in_progress"] is True
    assert view["current_status"] == "authorization_revoked"
    assert view["medium_status"]["N2"] == REVOKED
