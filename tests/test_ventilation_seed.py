"""Ventilation safety boundary tests.

The old seed logic treated one local sensor as the whole bay verdict and let
B keep producing while both bays shared return duct D1. These tests pin the
recomputable boundary instead: worst-case alarm grades, duct propagation,
physical damper isolation, personnel muster, two legal restart dispositions
and revocation when late evidence invalidates an earlier restart promise.
"""
import json
from pathlib import Path

import pytest

from cylindertrail.air_monitor import (
    ALARM, FAULT, NORMAL, WARNING, alarming, grade_reading, worst_case_ppm,
)
from cylindertrail.bay_control import commands, local_commands, propagation_scope
from cylindertrail.ventilation_capture import (
    FRESH_AIR_MIN_FLOW, REQUIRED_SAFE_SAMPLES,
    A_AUTHORIZATION_MISSING, A_PURGE_MISSING, B_DAMPER_NOT_PROVEN,
    B_FLOW_BELOW_MINIMUM, B_MUSTER_INCOMPLETE, B_SAMPLES_INSUFFICIENT,
    B_SOURCE_BAY_LOCKED, VentilationCapture,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'ventilation-shutdown.json'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def ev(kind, minute, **body):
    body.setdefault('id', f'{kind.upper().replace("_", "-")}-{minute}-{len(body)}')
    return {'kind': kind, 'at_minute': minute, **body}


def muster(cap, employee, minute):
    cap.append(ev('muster', minute, id=f'MUSTER-{employee}-{minute}',
                  employee=employee, assembly_point='M1',
                  signature=f'SIG-{employee}', signed_at_minute=minute))


def safe_retest(cap, bay, minute, gas_ppm=100, error=50):
    cap.append(ev('bay_retest', minute, id=f'RETEST-{bay}-{minute}', bay=bay,
                  gas_ppm=gas_ppm, alarm_ppm=1000,
                  calibration_error_ppm=error, result='safe'))


def fresh_readings(cap, minute, a_ppm=1100, b_ppm=100, error=100):
    """New samples for the same logical sensors, keeping them fresh."""
    cap.append(ev('sensor_reading', minute, id=f'S-A-{minute}', sensor_id='S-A',
                  bay='A', gas_ppm=a_ppm, alarm_ppm=1000,
                  calibration_error_ppm=error, status='ok'))
    cap.append(ev('sensor_reading', minute, id=f'S-B-{minute}', sensor_id='S-B',
                  bay='B', gas_ppm=b_ppm, alarm_ppm=1000,
                  calibration_error_ppm=error, status='ok'))


def isolate_b(cap, minute, receipt='in_position'):
    cap.append(ev('damper_command', minute, id=f'CMD-DB-{minute}', damper_id='DB',
                  bay='B', return_duct='D1', command='close'))
    cap.append(ev('damper_receipt', minute + 1, id=f'RCPT-DB-{minute}', damper_id='DB',
                  receipt=receipt))


# --------------------------------------------------------------------- seed flaw
def test_seed_local_logic_lets_shared_duct_bay_run_but_propagation_stops_both():
    data = load_fixture()
    reading_a, reading_b = data['readings']

    # The baseline single-point logic is the documented unsafe seed: A stops,
    # B is told to run although both return ducts are D1.
    assert local_commands(data['readings'], data['bays']) == {'A': 'stop', 'B': 'run'}

    # Worst-case upper bound, not the raw point, drives the alarm grade.
    assert worst_case_ppm(reading_a) == 1200
    assert grade_reading(reading_a) == ALARM
    assert alarming(reading_a)

    # Duct-wide propagation puts both bays into the stop sequence.
    assert commands(data['readings'], data['bays'], data['dampers']) == {
        'A': 'stop', 'B': 'stop'}
    scope = propagation_scope(data['readings'], data['bays'], data['dampers'])
    assert scope['affected_bays'] == ['A', 'B']

    # The seed also flipped A back to run on a lower point. With calibration
    # uncertainty the verdict cannot be a single raw-point comparison.
    lower = {**reading_a, 'gas_ppm': 800}
    assert grade_reading(lower) == NORMAL            # 800 + 100 = 900 stays under 1000
    band = {**reading_a, 'gas_ppm': 950}             # point under, upper bound 1050 over
    assert grade_reading(band) == WARNING
    assert commands([band, reading_b], data['bays'], data['dampers']) == {
        'A': 'stop', 'B': 'stop'}

    fully_safe = {**reading_a, 'gas_ppm': 700, 'calibration_error_ppm': 100}
    safe_b = {**reading_b, 'gas_ppm': 50, 'calibration_error_ppm': 100}
    assert grade_reading(fully_safe) == NORMAL
    assert commands([fully_safe, safe_b], data['bays'], data['dampers']) == {
        'A': 'run', 'B': 'run'}


def test_stale_and_missing_sensors_are_not_treated_as_zero():
    data = load_fixture()
    # Explicit stale status is a fault even without a time window.
    stale = {**data['readings'][1], 'id': 'S-B-STALE', 'gas_ppm': 100,
             'at_minute': 500, 'status': 'stale'}
    assert grade_reading(stale) == FAULT
    assert alarming(stale)
    # Time-window staleness is opt-in: the same reading without an explicit
    # status stays normal unless the caller passes a window.
    aged = {**stale, 'id': 'S-B-AGED', 'status': 'ok'}
    assert grade_reading(aged, now=540, stale_after_minutes=5) == FAULT
    assert grade_reading(aged, now=540) == NORMAL
    missing = {'id': 'S-X', 'bay': 'B', 'gas_ppm': None, 'alarm_ppm': 1000,
               'calibration_error_ppm': 0, 'at_minute': 540}
    assert grade_reading(missing) == FAULT

    cap = VentilationCapture.from_fixture(data)
    # A's fixed sensor explicitly goes stale; a fresh B sample cannot make the
    # shared D1 path safe while A is in fault.
    cap.append(ev('sensor_reading', 560, id='S-A-560', sensor_id='S-A', bay='A',
                  gas_ppm=1100, alarm_ppm=1000,
                  calibration_error_ppm=100, status='stale'))
    cap.append(ev('sensor_reading', 560, id='S-B-560', sensor_id='S-B', bay='B',
                  gas_ppm=80, alarm_ppm=1000,
                  calibration_error_ppm=10, status='ok'))
    snap = cap.snapshot()
    assert snap['bays']['A']['alarm_grade'] == FAULT
    assert snap['bays']['A']['equipment'] == 'stop'
    assert snap['bays']['B']['equipment'] == 'stop'
    # A declared sensor that never appears anywhere in the evidence is a fault.
    data_missing = json.loads(FIXTURE.read_text(encoding='utf-8'))
    data_missing['readings'] = [r for r in data_missing['readings'] if r['id'] != 'S-B']
    cap_missing = VentilationCapture.from_fixture(data_missing)
    assert cap_missing.snapshot()['bays']['B']['alarm_grade'] == FAULT


# ------------------------------------------------------------- immediate response
def test_alarm_stops_and_evacuates_both_bays_with_muster_evidence():
    data = load_fixture()
    cap = VentilationCapture.from_fixture(data)
    snap = cap.snapshot()

    assert snap['bays']['A']['equipment'] == 'stop'
    assert snap['bays']['B']['equipment'] == 'stop'
    assert [c['command'] for c in snap['command_ledger']] == ['stop', 'stop']
    assert {e['bay'] for e in snap['evacuation_ledger']} == {'A', 'B'}
    for entry in snap['evacuation_ledger']:
        assert entry['assembly_point'] == 'M1'
        assert entry['missing'] == entry['staff']  # no sign-in yet

    # Each present employee needs a time-stamped muster sign-in.
    muster(cap, 'E1', 541)
    muster(cap, 'E2', 541)
    snap = cap.snapshot()
    assert snap['bays']['A']['missing_staff'] == []
    assert snap['bays']['B']['missing_staff'] == []
    assert snap['bays']['A']['evacuation'] == 'complete'


# ------------------------------------------------------------- two legal plans
def test_plan_a_full_lockdown_then_separate_returns_after_purge_and_release():
    data = load_fixture()
    cap = VentilationCapture.from_fixture(data)
    muster(cap, 'E1', 541)
    muster(cap, 'E2', 541)

    # Before purge/authorization neither bay may restart under plan A.
    snap = cap.snapshot()
    assert A_PURGE_MISSING in snap['bays']['B']['blockers']['A']
    assert A_AUTHORIZATION_MISSING in snap['bays']['B']['blockers']['A']
    assert cap.try_resume('B', 'A', 546) is False
    assert cap.snapshot()['command_ledger'][-1]['command'] == 'run_rejected'

    cap.append(ev('duct_purged', 547, id='PURGE-D1-547', duct='D1'))
    cap.append(ev('release_authorization', 548, id='AUTH-A-548', bay='A', authorized=True))
    cap.append(ev('release_authorization', 549, id='AUTH-B-549', bay='B', authorized=True))
    # Fresh repeat sampling and safe fixed-sensor readings after the purge.
    safe_retest(cap, 'A', 550)
    safe_retest(cap, 'A', 551)
    safe_retest(cap, 'B', 550)
    safe_retest(cap, 'B', 551)
    fresh_readings(cap, 551, a_ppm=120, b_ppm=80, error=50)
    cap.append(ev('plan_selected', 552, id='PLAN-A-552', plan='A'))

    assert cap.try_resume('B', 'A', 553) is True
    assert cap.try_resume('A', 'A', 554) is True
    snap = cap.snapshot()
    assert snap['bays']['B']['equipment'] == 'run'
    assert snap['bays']['A']['equipment'] == 'run'
    b_evidence = snap['command_ledger'][-2]['evidence']
    assert any(str(e).startswith('purge:D1') for e in b_evidence)
    assert 'authorization:B' in b_evidence
    # No production gap is forgotten.
    assert snap['lost_production']['A']['intervals'][0]['stop_minute'] == 540
    assert snap['lost_production']['B']['lost_minutes'] == 13
    # Source bay A needed its own repeat sampling and explicit release.
    a_run = next(c for c in snap['command_ledger']
                 if c['bay'] == 'A' and c['command'] == 'run')
    assert all(rid in a_run['evidence'] for rid in ('RETEST-A-550', 'RETEST-A-551'))
    assert 'authorization:A' in a_run['evidence']


def test_plan_b_restarts_only_b_behind_proven_damper_and_keeps_a_sealed():
    data = load_fixture()
    cap = VentilationCapture.from_fixture(data)
    muster(cap, 'E1', 541)
    muster(cap, 'E2', 541)
    safe_retest(cap, 'B', 542)
    safe_retest(cap, 'B', 543)
    isolate_b(cap, 543)
    fresh_readings(cap, 544)
    cap.append(ev('fresh_air', 545, id='FLOW-B-545', bay='B',
                  flow_m3_per_minute=400))
    cap.append(ev('plan_selected', 545, id='PLAN-B-545', plan='B'))
    snap = cap.snapshot()

    # Source bay A can never use the independent-air shortcut.
    assert snap['bays']['A']['plan_eligible']['B'] is False
    assert B_SOURCE_BAY_LOCKED in snap['bays']['A']['blockers']['B']
    assert snap['bays']['B']['plan_eligible']['B'] is True
    assert snap['bays']['B']['physically_isolated'] is True

    assert cap.try_resume('B', 'B', 546) is True
    assert cap.try_resume('A', 'B', 546) is False
    snap = cap.snapshot()
    assert snap['bays']['B']['equipment'] == 'run'
    assert snap['bays']['B']['resume_plan'] == 'B'
    assert snap['bays']['A']['equipment'] == 'stop'

    run_entry = next(c for c in snap['command_ledger']
                     if c['bay'] == 'B' and c['command'] == 'run')
    assert any(str(e).startswith('damper_receipt:DB') for e in run_entry['evidence'])
    assert 'FLOW-B-545' in run_entry['evidence']
    assert not any(str(e).startswith('purge:') for e in run_entry['evidence'])
    # B's capacity comes back earlier under plan B than under plan A.
    assert snap['lost_production']['B']['lost_minutes'] == 6
    assert snap['lost_production']['A']['intervals'][0]['resume_minute'] is None


def test_same_inputs_support_both_dispositions_with_different_evidence():
    data = load_fixture()
    plan_a = VentilationCapture.from_fixture(data)
    plan_b = VentilationCapture.from_fixture(data)
    for cap in (plan_a, plan_b):
        muster(cap, 'E1', 541)
        muster(cap, 'E2', 541)

    safe_retest(plan_a, 'A', 550)
    safe_retest(plan_a, 'A', 551)
    safe_retest(plan_a, 'B', 550)
    safe_retest(plan_a, 'B', 551)
    plan_a.append(ev('duct_purged', 547, id='PURGE-D1-A', duct='D1'))
    plan_a.append(ev('release_authorization', 548, id='AUTH-A-A', bay='A', authorized=True))
    plan_a.append(ev('release_authorization', 549, id='AUTH-B-A', bay='B', authorized=True))
    fresh_readings(plan_a, 551, a_ppm=120, b_ppm=80, error=50)
    plan_a.append(ev('plan_selected', 552, id='SEL-A', plan='A'))
    plan_a.try_resume('B', 'A', 553)
    plan_a.try_resume('A', 'A', 554)

    safe_retest(plan_b, 'B', 542)
    safe_retest(plan_b, 'B', 543)
    isolate_b(plan_b, 543)
    fresh_readings(plan_b, 544)
    plan_b.append(ev('fresh_air', 545, id='FLOW-B-X', bay='B', flow_m3_per_minute=400))
    plan_b.append(ev('plan_selected', 545, id='SEL-B', plan='B'))
    plan_b.try_resume('B', 'B', 546)

    a_snap, b_snap = plan_a.snapshot(), plan_b.snapshot()
    # Same initial physical response: both stop and evacuate at minute 540.
    assert a_snap['command_ledger'][:2] == b_snap['command_ledger'][:2]
    # Different restart moments and different physical evidence.
    assert b_snap['lost_production']['B']['lost_minutes'] < a_snap['lost_production']['B']['lost_minutes']
    assert b_snap['bays']['A']['equipment'] == 'stop'
    assert a_snap['bays']['A']['equipment'] == 'run'
    assert any('damper_receipt' in str(e)
               for c in b_snap['command_ledger'] for e in c['evidence'])
    assert not any('damper_receipt' in str(e)
                   for c in a_snap['command_ledger'] for e in c['evidence'])


# -------------------------------------------------- physical isolation prerequisites
def test_damper_command_without_receipt_forces_plan_a_lockdown():
    data = load_fixture()
    cap = VentilationCapture.from_fixture(data)
    muster(cap, 'E1', 541)
    muster(cap, 'E2', 541)
    safe_retest(cap, 'B', 542)
    safe_retest(cap, 'B', 543)
    fresh_readings(cap, 544)
    # Command sent, but no in-position receipt ever arrives.
    cap.append(ev('damper_command', 543, id='CMD-DB-NORECEIPT', damper_id='DB',
                  bay='B', return_duct='D1', command='close'))
    cap.append(ev('fresh_air', 545, id='FLOW-B-NORCPT', bay='B', flow_m3_per_minute=400))
    cap.append(ev('plan_selected', 545, id='PLAN-B-NO', plan='B'))
    snap = cap.snapshot()

    assert snap['bays']['B']['physically_isolated'] is False
    assert B_DAMPER_NOT_PROVEN in snap['bays']['B']['blockers']['B']
    assert cap.try_resume('B', 'B', 546) is False
    assert cap.snapshot()['connected_pairs'] == [['A', 'B']]  # path still connected
    assert cap.snapshot()['bays']['B']['equipment'] == 'stop'

    # A fault receipt is no better than a missing one.
    cap.append(ev('damper_receipt', 547, id='RCPT-DB-FAULT', damper_id='DB',
                  receipt='fault'))
    snap = cap.snapshot()
    assert B_DAMPER_NOT_PROVEN in snap['bays']['B']['blockers']['B']
    assert snap['bays']['B']['equipment'] == 'stop'


def test_muster_and_repeat_sampling_gate_every_restart():
    data = load_fixture()
    cap = VentilationCapture.from_fixture(data)
    muster(cap, 'E1', 541)  # E2 never signs in
    safe_retest(cap, 'B', 542)
    isolate_b(cap, 543)
    fresh_readings(cap, 544)
    cap.append(ev('fresh_air', 545, id='FLOW-B-MUSTER', bay='B', flow_m3_per_minute=400))
    cap.append(ev('plan_selected', 545, id='PLAN-B-MUSTER', plan='B'))
    snap = cap.snapshot()
    assert B_MUSTER_INCOMPLETE in snap['bays']['B']['blockers']['B']
    assert B_SAMPLES_INSUFFICIENT in snap['bays']['B']['blockers']['B']
    assert cap.try_resume('B', 'B', 546) is False

    muster(cap, 'E2', 546)
    safe_retest(cap, 'B', 547)
    assert cap.try_resume('B', 'B', 548) is True


# ------------------------------------------------------------- late evidence revokes
def test_late_calibration_revokes_b_restart_and_keeps_history():
    data = load_fixture()
    cap = VentilationCapture.from_fixture(data)
    muster(cap, 'E1', 541)
    muster(cap, 'E2', 541)
    safe_retest(cap, 'B', 542)
    safe_retest(cap, 'B', 543)
    isolate_b(cap, 543)
    fresh_readings(cap, 544)
    cap.append(ev('fresh_air', 545, id='FLOW-B-CAL', bay='B', flow_m3_per_minute=400))
    cap.append(ev('plan_selected', 545, id='PLAN-B-CAL', plan='B'))
    assert cap.try_resume('B', 'B', 546) is True
    before = cap.snapshot()
    assert before['bays']['B']['equipment'] == 'run'
    earlier_commands = len(before['command_ledger'])
    earlier_lost = before['lost_production']['B']['lost_minutes']

    # Keep the fixed sensors fresh while the late calibration lands: B's
    # 100 ppm point now has a 950 ppm error band (upper bound 1050 >= 1000).
    cap.append(ev('sensor_reading', 559, id='S-A-559', sensor_id='S-A', bay='A',
                  gas_ppm=1100, alarm_ppm=1000, calibration_error_ppm=100, status='ok'))
    cap.append(ev('sensor_reading', 559, id='S-B-559', sensor_id='S-B', bay='B',
                  gas_ppm=100, alarm_ppm=1000, calibration_error_ppm=100, status='ok'))
    cap.append(ev('calibration_update', 560, id='CAL-SB-LATE',
                  sensor_id='S-B', calibration_error_ppm=950))
    snap = cap.snapshot()
    assert snap['bays']['B']['equipment'] == 'stop'
    assert snap['bays']['B']['alarm_grade'] == WARNING
    revoke = snap['command_ledger'][-1]
    assert revoke['reason'] == 'late_evidence_revokes_restart'
    assert revoke['revoked_plan'] == 'B'

    # History is preserved, not rewritten.
    assert len(snap['command_ledger']) == earlier_commands + 1
    assert snap['lost_production']['B']['intervals'][0] == {
        'stop_minute': 540, 'resume_minute': 546, 'plan': 'B'}
    # At the revocation minute the new downtime has only just started.
    assert snap['lost_production']['B']['lost_minutes'] == earlier_lost
    assert snap['lost_production']['B']['intervals'][1]['ongoing_minutes'] == 0
    assert len(snap['evacuation_ledger']) == 3
    # A was never released and remains locked as the leak source.
    assert snap['bays']['A']['equipment'] == 'stop'
    assert snap['bays']['A']['alarm_source'] is True

    # Five minutes later the second lockdown is still in force and its lost
    # production accumulates on top of the preserved first interval.
    muster(cap, 'E2', 565)
    later = cap.snapshot()
    assert later['bays']['B']['equipment'] == 'stop'
    assert later['lost_production']['B']['lost_minutes'] == earlier_lost + 5
    assert later['lost_production']['B']['intervals'][0]['plan'] == 'B'
    assert later['lost_production']['B']['intervals'][1]['ongoing_minutes'] == 5


def test_fresh_air_drop_and_damper_fault_revoke_plan_b_commitment():
    data = load_fixture()
    cap = VentilationCapture.from_fixture(data)
    muster(cap, 'E1', 541)
    muster(cap, 'E2', 541)
    safe_retest(cap, 'B', 542)
    safe_retest(cap, 'B', 543)
    isolate_b(cap, 543)
    fresh_readings(cap, 544)
    cap.append(ev('fresh_air', 545, id='FLOW-B-OK', bay='B', flow_m3_per_minute=400))
    cap.append(ev('plan_selected', 545, id='PLAN-B-FLOW', plan='B'))
    assert cap.try_resume('B', 'B', 546) is True
    # Keep the sensors fresh so the revocation is driven by flow, not staleness.
    fresh_readings(cap, 549, a_ppm=1100, b_ppm=100)

    cap.append(ev('fresh_air', 550, id='FLOW-B-LOW', bay='B',
                  flow_m3_per_minute=FRESH_AIR_MIN_FLOW - 10))
    snap = cap.snapshot()
    assert snap['bays']['B']['equipment'] == 'stop'
    assert B_FLOW_BELOW_MINIMUM in snap['command_ledger'][-1]['blockers']
    assert snap['command_ledger'][-1]['revoked_plan'] == 'B'
    # The run episode and its 6 minutes of production remain in the books.
    assert snap['lost_production']['B']['intervals'][0]['plan'] == 'B'

    # Restoring flow alone cannot restart B: post-revocation muster/sampling
    # evidence must be rebuilt before a new commitment.
    cap.append(ev('fresh_air', 555, id='FLOW-B-RESTORED', bay='B', flow_m3_per_minute=420))
    assert cap.try_resume('B', 'B', 556) is False
    assert B_MUSTER_INCOMPLETE in cap.snapshot()['bays']['B']['blockers']['B']


def test_damper_fault_receipt_reconnects_duct_and_revokes_b():
    data = load_fixture()
    cap = VentilationCapture.from_fixture(data)
    muster(cap, 'E1', 541)
    muster(cap, 'E2', 541)
    safe_retest(cap, 'B', 542)
    safe_retest(cap, 'B', 543)
    isolate_b(cap, 543)
    fresh_readings(cap, 544)
    cap.append(ev('fresh_air', 545, id='FLOW-B-DF', bay='B', flow_m3_per_minute=400))
    cap.append(ev('plan_selected', 545, id='PLAN-B-DF', plan='B'))
    assert cap.try_resume('B', 'B', 546) is True
    assert cap.snapshot()['connected_pairs'] == []

    # A is still alarming behind the proven damper. A fault receipt on DB loses
    # the physical isolation, reconnects D1 and must revoke B's restart.
    cap.append(ev('damper_receipt', 552, id='RCPT-DB-FAULT-552',
                  damper_id='DB', receipt='fault'))
    snap = cap.snapshot()
    assert snap['connected_pairs'] == [['A', 'B']]
    assert snap['bays']['B']['physically_isolated'] is False
    assert snap['bays']['B']['equipment'] == 'stop'
    revoke = snap['command_ledger'][-1]
    assert revoke['reason'] == 'late_evidence_revokes_restart'
    assert B_DAMPER_NOT_PROVEN in snap['bays']['B']['blockers']['B']
    assert B_DAMPER_NOT_PROVEN in revoke['blockers'] or \
        'shared_return_duct_propagation' in revoke['blockers']


def test_replay_is_deterministic_from_append_only_log():
    data = load_fixture()
    cap = VentilationCapture.from_fixture(data)
    muster(cap, 'E1', 541)
    muster(cap, 'E2', 541)
    safe_retest(cap, 'B', 542)
    safe_retest(cap, 'B', 543)
    isolate_b(cap, 543)
    fresh_readings(cap, 544)
    cap.append(ev('fresh_air', 545, id='FLOW-B-DET', bay='B', flow_m3_per_minute=400))
    cap.append(ev('plan_selected', 545, id='PLAN-B-DET', plan='B'))
    cap.try_resume('B', 'B', 546)
    cap.append(ev('sensor_reading', 559, id='S-A-559', sensor_id='S-A', bay='A',
                  gas_ppm=1100, alarm_ppm=1000, calibration_error_ppm=100, status='ok'))
    cap.append(ev('sensor_reading', 559, id='S-B-559', sensor_id='S-B', bay='B',
                  gas_ppm=100, alarm_ppm=1000, calibration_error_ppm=100, status='ok'))
    cap.append(ev('calibration_update', 560, id='CAL-DET',
                  sensor_id='S-B', calibration_error_ppm=950))

    rebuilt = VentilationCapture(data['bays'], data['dampers'], data['assembly_point'])
    for event in cap.events:
        body = {k: v for k, v in event.items() if k != '_seq'}
        rebuilt.append(body)
    assert rebuilt.snapshot() == cap.snapshot()


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q', '-p', 'no:cacheprovider']))
