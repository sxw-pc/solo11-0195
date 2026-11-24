import json
import sqlite3
from pathlib import Path

import pytest

from cylindertrail import placement, rules
from cylindertrail.cage import (PlanInterrupted, apply_step, build_state, execute_plan,
                                free_slots, freeze_bottle, journal_rows, load_freezes,
                                merge_offline, occupancy_proof, proof, validate_state)
from cylindertrail.cage_capture import capture_event, capture_fixture, rules_events, verify

FIXTURE = Path(__file__).parents[1] / 'fixtures' / 'incompatible-cage.json'


def load():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def fresh_state(data=None, freezes=None):
    return build_state(capture_fixture(data or load()), freezes)


def decisions_for(data):
    return rules.calculate(rules_events(capture_fixture(data)))[0]


def scan_ctx(events):
    scans = [e for e in events if e['kind'] == 'scan']
    handlers = {e['id']: e for e in events if e['kind'] == 'handler'}
    receipts = [e for e in events if e['kind'] == 'offline_receipt']
    return scans, handlers, receipts


def test_open_slot_baseline_wrong_release():
    d = load()
    # 旧基线: 账面还有一个空位, 只看空位就放行
    assert free_slots(d['cage']) == 1
    assert placement.accepted(d['cage'], d['incoming']) is True
    assert [d['cage']['bottles'][0]['class'], d['incoming']['class']] in d['site_incompatible_pairs']
    # 同一空位接入场地不相容矩阵后必须拒绝
    assert placement.accepted(d['cage'], d['incoming'], d['site_incompatible_pairs']) is False
    # 上游占位数据直接决定下游入库许可: C1 额定容量 2→1
    shrunk = dict(d['cage'], capacity=1)
    assert free_slots(shrunk) == 0
    assert placement.accepted(shrunk, d['incoming']) is False
    shrunk_data = load()
    shrunk_data['capture']['cages'][0]['capacity'] = 1
    assert placement.intake_conflicts(fresh_state(shrunk_data), 'CYL-2', ['C1'])[0]['reason'] == 'capacity'
    # 新计划器给出最小冲突 (A/B 不可同笼, 占用瓶 CYL-1)
    state = fresh_state(d)
    assert placement.intake_conflicts(state, 'CYL-2', ['C1']) == [
        {'cage': 'C1', 'reason': 'incompatible', 'bottle': 'CYL-2',
         'occupant': 'CYL-1', 'classes': ['A', 'B']}]
    # 不能先临时放 C1 再补记移动: 第一步即被拒绝
    plan = placement.plan_steps(state, [{'op': 'place', 'bottle': 'CYL-2', 'to': 'C1'},
                                        {'op': 'move', 'bottle': 'CYL-1', 'from': 'C1', 'to': 'C2'}])
    assert plan['executable'] is False
    assert plan['conflict']['seq'] == 0 and plan['conflict']['reason'] == 'incompatible'


def test_capture_events_carry_verifiable_evidence():
    events = capture_fixture(load())
    assert {e['kind'] for e in events} == {'bottle', 'cage', 'incompatibility', 'handler',
                                           'scan', 'outbound', 'door', 'offline_receipt'}
    assert all(verify(e) for e in events)
    tampered = capture_event('scan', id='S1', purpose='move', bottle_id='CYL-2',
                             handler='HND-1', at='2026-11-24T09:00')
    tampered['bottle_id'] = 'CYL-9'
    assert not verify(tampered)
    with pytest.raises(ValueError):
        capture_event('scan', id='S2')


def test_disposition_direct_to_c2_keeps_a_in_c1():
    state = fresh_state()
    res = placement.disposition_direct(state, 'CYL-2', 'C2')
    assert res['executable']
    final = res['state']
    assert final['bottles']['CYL-2']['location'] == 'C2'     # B 入 C2
    assert final['bottles']['CYL-1']['location'] == 'C1'     # A 保持 C1
    assert final['cages']['C2']['bottles'] == ['CYL-2']      # C2 被占用
    assert all(s['step']['op'] != 'outbound' for s in res['steps'])  # 原客户出库不提前
    assert validate_state(final) == []
    # 后续入库能力: C2 已满, 再入即报容量最小冲突
    assert placement.intake_conflicts(final, 'CYL-3', ['C2'])[0]['reason'] == 'capacity'
    # 笼门锁状态采录参与可执行性
    locked_data = load()
    locked_data['capture']['doors'] = [
        dict(door, locked=True) if door['cage'] == 'C2' else door
        for door in locked_data['capture']['doors']]
    locked = placement.disposition_direct(fresh_state(locked_data), 'CYL-2', 'C2')
    assert locked['executable'] is False and locked['conflict']['reason'] == 'door_locked'


def test_disposition_outbound_first_empties_c1():
    data = load()
    state = fresh_state(data)
    decisions = decisions_for(data)
    # 接入 rules.py 既有出库交接裁定: 双签齐备, 保管已转移
    assert decisions['CYL-1/出库双签是否转移保管']['result'] == '用气单位'
    scans = [e for e in capture_fixture(data) if e['kind'] == 'scan']
    res = placement.disposition_outbound_first(state, 'CYL-1', 'CYL-2', decisions, scans)
    assert res['executable']
    assert [s['step']['op'] for s in res['steps']] == ['outbound', 'place']
    final = res['state']
    assert final['bottles']['CYL-1']['location'] == 'CUSTOMER'  # A 转客户保管
    assert final['bottles']['CYL-2']['location'] == 'C1'        # B 入腾空后的 C1
    assert final['cages']['C2']['bottles'] == []                # C2 继续可用
    assert placement.intake_conflicts(final, 'CYL-3', ['C2']) == []
    assert validate_state(final) == []


def test_disposition_outbound_first_refused_without_evidence():
    data = load()
    # 客户双签缺失: 不得靠改状态字段假装可行
    data['capture']['outbound'][0]['outbound']['customer_signature'] = None
    state = fresh_state(data)
    decisions = decisions_for(data)
    assert decisions['CYL-1/出库双签是否转移保管']['result'] == '充装站'
    scans = [e for e in capture_fixture(data) if e['kind'] == 'scan']
    res = placement.disposition_outbound_first(state, 'CYL-1', 'CYL-2', decisions, scans)
    assert res['executable'] is False
    assert res['conflict']['seq'] == 0
    assert res['conflict']['reason'] == 'custody_not_transferred'
    assert res['conflict']['decision'] == '充装站'
    assert state['bottles']['CYL-1']['location'] == 'C1'  # 状态未被假装改写
    # 物理离站扫描缺失同样拒绝
    data2 = load()
    data2['capture']['scans'] = [s for s in data2['capture']['scans'] if s['purpose'] != 'offsite']
    scans2 = [e for e in capture_fixture(data2) if e['kind'] == 'scan']
    res2 = placement.disposition_outbound_first(fresh_state(data2), 'CYL-1', 'CYL-2',
                                                decisions_for(data2), scans2)
    assert res2['executable'] is False and res2['conflict']['reason'] == 'offsite_scan_missing'


def test_offline_moves_merge_exclusive_and_deterministic():
    d = load()
    # 基线: 两个断网入口各自只看到 C2 空位, 逐单都被接受
    assert all(placement.accepted(d['alternate_cage'], {'id': m['bottle_id']})
               for m in d['offline_moves'])
    assert len(d['offline_moves']) > d['alternate_cage']['capacity']
    events = capture_fixture(d)
    state = build_state(events)
    scans, handlers, receipts = scan_ctx(events)
    merged, conflicts = merge_offline(state, receipts, scans, handlers)
    # 排他占位: 规范序 MOVE-A 先得 C2, MOVE-B 报容量最小冲突
    assert merged['bottles']['CYL-2']['location'] == 'C2'
    assert merged['bottles']['CYL-3']['location'] == 'INTAKE'
    assert conflicts == [{'move': 'MOVE-B', 'conflict': {'reason': 'capacity', 'cage': 'C2',
                                                         'capacity': 1, 'occupants': ['CYL-2']}}]
    # 合并结果与回传先后无关
    merged_rev, conflicts_rev = merge_offline(state, list(reversed(receipts)), scans, handlers)
    assert proof(merged_rev) == proof(merged)
    assert conflicts_rev == conflicts
    # 重放同一批回执: 已执行的 MOVE-A 幂等跳过, 状态不变;
    # 未决的 MOVE-B 确定性地重新报出同一冲突 (储位释放后仍可被重试)
    again, again_conflicts = merge_offline(merged, receipts, scans, handlers)
    assert proof(again) == proof(merged)
    assert again_conflicts == conflicts
    assert again['applied_moves'] == ['MOVE-A']
    # 可证状态: 无违例, C2 排他被占
    attestation = occupancy_proof(merged)
    assert attestation['violations'] == []
    assert attestation['cages']['C2'] == {'free': 0, 'occupants': ['CYL-2']}
    assert attestation['applied_moves'] == ['MOVE-A']


def test_executed_move_cannot_be_stolen_by_late_receipt():
    events = capture_fixture(load())
    state = build_state(events)
    scans, handlers, receipts = scan_ctx(events)
    move_b = [r for r in receipts if r['id'] == 'MOVE-B']
    # MOVE-B 先执行并入账
    merged, conflicts = merge_offline(state, move_b, scans, handlers)
    assert conflicts == [] and merged['bottles']['CYL-3']['location'] == 'C2'
    # MOVE-A 回传更晚也不得抢走已执行移动, 即使规范序在前
    late, conflicts = merge_offline(merged, receipts, scans, handlers)
    assert late['bottles']['CYL-3']['location'] == 'C2'
    assert late['bottles']['CYL-2']['location'] == 'INTAKE'
    assert [c['move'] for c in conflicts] == ['MOVE-A']
    assert conflicts[0]['conflict']['reason'] == 'capacity'


def test_interrupted_plan_resumes_without_duplicate_release(tmp_path):
    data = load()
    state = fresh_state(data)
    decisions = decisions_for(data)
    scans = [e for e in capture_fixture(data) if e['kind'] == 'scan']
    ctx = {'custody': {'CYL-1': placement.custody_of(decisions, 'CYL-1')}, 'scans': scans}
    steps = [{'op': 'outbound', 'bottle': 'CYL-1', 'from': 'C1'},
             {'op': 'place', 'bottle': 'CYL-2', 'to': 'C1'}]
    db_path = tmp_path / 'cage.sqlite'
    db = sqlite3.connect(db_path)
    # 搬运中断: 第一步出库已入账, 第二步未做
    with pytest.raises(PlanInterrupted):
        execute_plan(db, 'PLAN-2', steps, state, ctx, stop_after=1)
    rows = journal_rows(db, 'PLAN-2')
    assert len(rows) == 1 and json.loads(rows[0]['step'])['op'] == 'outbound'
    assert json.loads(rows[0]['evidence'])['scans'] == ['SCAN-OFF-CYL1']
    db.close()
    # 恢复: 新连接只补做尚未完成的动作
    db = sqlite3.connect(db_path)
    result = execute_plan(db, 'PLAN-2', steps, state, ctx)
    assert result['complete'] and len(result['journal']) == 2
    assert result['journal'][0]['status'] == 'kept'
    rows = journal_rows(db, 'PLAN-2')
    assert len(rows) == 2
    assert sum(1 for r in rows if json.loads(r['step'])['op'] == 'outbound') == 1  # 不重复出库
    assert rows[0]['post_proof'] == rows[1]['pre_proof']                          # 可回放链
    assert json.loads(rows[0]['pre_state'])['bottles']['CYL-1']['location'] == 'C1'
    final = result['state']
    assert final['bottles']['CYL-1']['location'] == 'CUSTOMER'
    assert final['bottles']['CYL-2']['location'] == 'C1'
    # 再次执行空转, 不反复放行
    again = execute_plan(db, 'PLAN-2', steps, state, ctx)
    assert all(j['status'] == 'kept' for j in again['journal'])
    assert len(journal_rows(db, 'PLAN-2')) == 2
    db.close()


def test_old_freeze_readback_survives_rebuild(tmp_path):
    db_path = tmp_path / 'cage.sqlite'
    db = sqlite3.connect(db_path)
    assert freeze_bottle(db, 'CYL-3', '残压超标安全冻结', 'EV-FREEZE-1', '2026-11-24T09:00') is True
    # 旧冻结不得被新记录覆盖
    assert freeze_bottle(db, 'CYL-3', '误操作解除', 'EV-FAKE', '2026-11-24T10:00') is False
    db.close()
    db = sqlite3.connect(db_path)  # 旧冻结回读
    freezes = load_freezes(db)
    assert freezes['CYL-3']['reason'] == '残压超标安全冻结'
    assert freezes['CYL-3']['evidence'] == 'EV-FREEZE-1'
    events = capture_fixture(load())
    state = build_state(events, freezes)
    scans, handlers, receipts = scan_ctx(events)
    merged, conflicts = merge_offline(state, receipts, scans, handlers)
    # 冻结瓶不得占位移动, 未冻结瓶不受影响
    assert merged['bottles']['CYL-3']['location'] == 'INTAKE'
    assert merged['bottles']['CYL-2']['location'] == 'C2'
    assert conflicts == [{'move': 'MOVE-B', 'conflict': {'reason': 'frozen', 'bottle': 'CYL-3',
                                                         'freeze': freezes['CYL-3']}}]
    db.close()


def test_cross_cage_migration_is_replayable(tmp_path):
    state = fresh_state()
    steps = [{'op': 'move', 'bottle': 'CYL-1', 'from': 'C1', 'to': 'C2'}]
    db = sqlite3.connect(tmp_path / 'cage.sqlite')
    result = execute_plan(db, 'PLAN-X', steps, state)
    row = journal_rows(db, 'PLAN-X')[0]
    # 前置状态与完成证据可回放
    assert json.loads(row['pre_state'])['bottles']['CYL-1']['location'] == 'C1'
    assert json.loads(row['post_state'])['bottles']['CYL-1']['location'] == 'C2'
    assert row['pre_proof'] == proof(state)
    replayed = apply_step(state, steps[0])
    assert proof(replayed) == row['post_proof']
    # 一瓶仅占一处, 每个中间状态合法
    final = result['state']
    assert validate_state(final) == []
    assert final['cages']['C1']['bottles'] == []
    assert final['cages']['C2']['bottles'] == ['CYL-1']
    db.close()
