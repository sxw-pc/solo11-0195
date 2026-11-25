"""Procurement capture tests: per-serial acceptance, two lawful closures,
duplicate identity, residual payable and late calibration recall."""

import json
from pathlib import Path

import pytest

from cylindertrail import valve_receipt
from cylindertrail.procurement_capture import (
    ProcurementCapture,
    ProcurementError,
)
from cylindertrail.supplier_order import ordered_units

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'valve-procurement.json'


def fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def capture():
    return ProcurementCapture.from_fixture(fixture())


def replay(capture, scenario):
    for event in fixture()['scenarios'][scenario]['events']:
        capture.record(event)
    return capture.snapshot()


# ----------------------------------------------------------- seed acceptance
def test_seed_no_longer_accepts_by_box_count():
    data = fixture()
    assert ordered_units(data['order']) == 10
    # The old count-only rule accepted ten boxes.  Now the two sub-50 N.m
    # valves are rejected on their own serial records.
    assert valve_receipt.decide(data['order'], data['valves']) == 'rejected'
    assert sum(v['torque_nm'] < data['order']['minimum_torque_nm']
               for v in data['valves']) == 2
    # Changing the order quantity no longer flips a physical verdict.
    larger_order = {**data['order'], 'quantity': 11}
    assert valve_receipt.decide(larger_order, data['valves']) != 'accepted'


def test_worst_case_side_of_measurement_decides():
    order = fixture()['order']  # 50..60, uncertainty 2
    qualified = valve_receipt.inspect_valve({'serial': 'X', 'torque_nm': 52}, 50, 60, 2)
    assert qualified['qualified'] and qualified['worst_case_low_nm'] == 50
    high_ok = valve_receipt.inspect_valve({'serial': 'Y', 'torque_nm': 58}, 50, 60, 2)
    assert high_ok['qualified'] and high_ok['worst_case_high_nm'] == 60
    low_bad = valve_receipt.inspect_valve({'serial': 'Z', 'torque_nm': 51.9}, 50, 60, 2)
    assert not low_bad['qualified'] and 'worst-case_low_below_minimum' in low_bad['failures']
    high_bad = valve_receipt.inspect_valve({'serial': 'W', 'torque_nm': 58.1}, 50, 60, 2)
    assert not high_bad['qualified']
    # V9's reading (45) cannot be padded by the other eight 55 N.m valves.
    assert valve_receipt.decide(order, fixture()['valves']) == 'rejected'


# --------------------------------------------------- initial state and money
def test_initial_receipt_quarantines_two_and_recognises_800():
    snap = capture().snapshot()
    assert snap['qualified_count'] == 8
    assert snap['quarantined_serials'] == ['V9', 'V10']
    assert snap['qualified_value_cents'] == 80000
    assert snap['recognized_payable_cents'] == 80000
    assert snap['unqualified_value_cents'] == 20000
    # Nothing paid yet: the 800 recognised for eight valves is still open.
    assert snap['paid_cents'] == 0
    assert snap['outstanding_cents'] == 80000
    recon = snap['reconciliation']
    assert recon['invoice_cents'] == 100000
    assert recon['po_value_cents'] == 100000
    assert recon['received_count'] == 10
    assert recon['three_way_ok'] is True
    assert recon['qualified_value_cents'] == 80000
    assert recon['identity_ok'] is True
    assert snap['settlement_complete'] is False


def test_cannot_prepay_quarantined_units():
    cap = capture()
    cap.record({'id': 'P1', 'kind': 'payment', 'amount': 800})
    with pytest.raises(ProcurementError):
        cap.record({'id': 'P2', 'kind': 'payment', 'amount': 1})


def test_invoice_split_accounting_balances_to_the_cent():
    cap = capture()
    cap.record({'id': 'P1', 'kind': 'payment', 'amount': 800})
    snap = cap.snapshot()
    recon = snap['reconciliation']
    # paid 800 + payable 0 + credited 0 + rejected 200 == invoice 1000
    assert (recon['paid_cents'] + recon['outstanding_cents']
            + recon['credit_cents'] + recon['unqualified_value_cents']
            == recon['invoice_cents'])


# ------------------------------------------------ path A: replacement branch
def test_path_a_replacement_returns_original_and_settles_1000():
    snap = replay(capture(), 'A_replacement')
    assert snap['returned_serials'] == ['V9', 'V10']
    assert snap['replaced_serials'] == ['V9', 'V10']
    assert snap['qualified_count'] == 10
    assert snap['qualified_serials'] == [f'V{i}' for i in range(1, 9)] + ['V11', 'V12']
    assert snap['qualified_value_cents'] == 100000
    # Replacement path carries no rework credit even though the contract offers one.
    assert snap['rework_credit_cents'] == 0
    assert snap['recognized_payable_cents'] == 100000
    assert snap['paid_cents'] == 100000
    assert snap['outstanding_cents'] == 0
    assert snap['settlement_complete'] is True
    assert snap['serials']['V11']['origin'] == 'replacement'
    assert snap['serials']['V11']['replacement_for'] == ['V9']
    assert snap['serials']['V9']['replaced_by'] == 'V11'
    assert snap['serials']['V9']['status'] == 'replaced'


def test_path_a_residual_after_800_is_200():
    cap = capture()
    cap.record({'id': 'A-PAY-1', 'kind': 'payment', 'amount': 800})
    cap.record({'id': 'A-RETURN', 'kind': 'return_to_supplier', 'serials': ['V9', 'V10']})
    snap = cap.snapshot()
    assert snap['recognized_payable_cents'] == 80000
    assert snap['outstanding_cents'] == 0
    cap.record({'id': 'A-REPL', 'kind': 'replacement_arrival',
                'replaces': ['V9', 'V10'],
                'valves': [{'serial': 'V11', 'torque_nm': 55},
                           {'serial': 'V12', 'torque_nm': 55}]})
    snap = cap.snapshot()
    assert snap['recognized_payable_cents'] == 100000
    assert snap['outstanding_cents'] == 20000
    cap.record({'id': 'A-PAY-2', 'kind': 'payment', 'amount': 200})
    assert cap.snapshot()['outstanding_cents'] == 0


# ----------------------------------------------------- path B: rework branch
def test_path_b_signed_rework_retest_credit_and_settles_900():
    snap = replay(capture(), 'B_rework')
    assert snap['qualified_count'] == 10
    assert snap['qualified_serials'] == [f'V{i}' for i in range(1, 11)]
    assert snap['serials']['V9']['status'] == 'reworked'
    assert snap['serials']['V9']['reworked'] is True
    assert snap['serials']['V9']['rework_hours'] == 1.5
    assert snap['serials']['V10']['rework_hours'] == 1.0
    # Original serials kept; a second, independent inspection is on record.
    inspections = snap['serials']['V9']['inspections']
    assert len(inspections) == 2
    assert inspections[0]['source'] == 'receipt' and inspections[0]['qualified'] is False
    assert inspections[1]['source'] == 'rework_retest' and inspections[1]['qualified'] is True
    assert inspections[1]['independent'] is True
    assert snap['rework_credit_cents'] == 10000
    assert snap['recognized_payable_cents'] == 90000
    assert snap['paid_cents'] == 90000
    assert snap['outstanding_cents'] == 0
    assert snap['settlement_complete'] is True


def test_path_b_residual_after_800_is_100():
    cap = capture()
    cap.record({'id': 'B-PAY-1', 'kind': 'payment', 'amount': 800})
    cap.record({'id': 'B-AUTH', 'kind': 'rework_authorization',
                'serials': ['V9', 'V10'], 'supplier_signed': True})
    cap.record({'id': 'B-REWORK', 'kind': 'rework',
                'entries': [{'serial': 'V9', 'hours': 1.5},
                            {'serial': 'V10', 'hours': 1.0}]})
    cap.record({'id': 'B-RETEST-V9', 'kind': 'retest', 'serial': 'V9',
                'torque_nm': 52, 'independent': True})
    cap.record({'id': 'B-RETEST-V10', 'kind': 'retest', 'serial': 'V10',
                'torque_nm': 54, 'independent': True})
    snap = cap.snapshot()
    assert snap['recognized_payable_cents'] == 90000
    assert snap['outstanding_cents'] == 10000
    with pytest.raises(ProcurementError):
        cap.record({'id': 'B-OVER', 'kind': 'payment', 'amount': 101})
    cap.record({'id': 'B-PAY-2', 'kind': 'payment', 'amount': 100})
    assert cap.snapshot()['settlement_complete'] is True


# --------------------------------------------- identity and path exclusivity
def test_same_serial_cannot_be_registered_as_two_good_valves():
    data = fixture()
    cap = ProcurementCapture.from_fixture(fixture(), record_receipt=False)
    duplicated = [dict(v) for v in data['valves']]
    duplicated.append({'serial': 'V9', 'torque_nm': 59})
    with pytest.raises(ProcurementError):
        cap.record({'id': 'DUP', 'kind': 'receipt', 'valves': duplicated})


def test_replacement_serial_must_be_new():
    cap = capture()
    cap.record({'id': 'R1', 'kind': 'return_to_supplier', 'serials': ['V9']})
    with pytest.raises(ProcurementError):
        cap.record({'id': 'R2', 'kind': 'replacement_arrival', 'replaces': ['V9'],
                    'valves': [{'serial': 'V9', 'torque_nm': 55}]})


def test_return_and_rework_are_mutually_exclusive():
    returned = ProcurementCapture.from_fixture(fixture())
    returned.record({'id': 'RET', 'kind': 'return_to_supplier', 'serials': ['V9']})
    with pytest.raises(ProcurementError):
        returned.record({'id': 'AUTH', 'kind': 'rework_authorization',
                         'serials': ['V9'], 'supplier_signed': True})

    reworked = ProcurementCapture.from_fixture(fixture())
    reworked.record({'id': 'AUTH', 'kind': 'rework_authorization',
                     'serials': ['V9'], 'supplier_signed': True})
    with pytest.raises(ProcurementError):
        reworked.record({'id': 'RET', 'kind': 'return_to_supplier', 'serials': ['V9']})
    reworked.record({'id': 'REWORK', 'kind': 'rework',
                     'entries': [{'serial': 'V9', 'hours': 1.0}]})
    reworked.record({'id': 'RETEST', 'kind': 'retest', 'serial': 'V9',
                     'torque_nm': 52, 'independent': True})
    with pytest.raises(ProcurementError):
        reworked.record({'id': 'REPL', 'kind': 'replacement_arrival',
                         'replaces': ['V9'],
                         'valves': [{'serial': 'V11', 'torque_nm': 55}]})


def test_rework_requires_signature_hours_and_independent_retest():
    cap = capture()
    with pytest.raises(ProcurementError):
        cap.record({'id': 'AUTH', 'kind': 'rework_authorization',
                    'serials': ['V9'], 'supplier_signed': False})
    cap.record({'id': 'AUTH', 'kind': 'rework_authorization',
                'serials': ['V9'], 'supplier_signed': True})
    with pytest.raises(ProcurementError):
        cap.record({'id': 'REWORK', 'kind': 'rework',
                    'entries': [{'serial': 'V9', 'hours': 0}]})
    # Retest cannot be used as a shortcut to re-register a failed serial.
    with pytest.raises(ProcurementError):
        cap.record({'id': 'EARLY', 'kind': 'retest', 'serial': 'V9',
                    'torque_nm': 52, 'independent': True})
    cap.record({'id': 'REWORK', 'kind': 'rework',
                'entries': [{'serial': 'V9', 'hours': 1.0}]})
    with pytest.raises(ProcurementError):
        cap.record({'id': 'NOT-INDEP', 'kind': 'retest', 'serial': 'V9',
                    'torque_nm': 52, 'independent': False})
    # A rework retest that fails stays quarantined; no credit is granted.
    cap.record({'id': 'RETEST', 'kind': 'retest', 'serial': 'V9',
                'torque_nm': 47, 'independent': True})
    snap = cap.snapshot()
    assert snap['serials']['V9']['status'] == 'quarantined'
    assert snap['rework_credit_cents'] == 0
    assert snap['recognized_payable_cents'] == 80000


# --------------------------------------------- late calibration recall case
def test_late_calibration_voids_accepted_valve_without_rewriting_cash():
    snap = replay(capture(), 'late_calibration')
    v1 = snap['serials']['V1']
    assert v1['status'] == 'recalled_in_service'
    assert v1['service_blocked'] is True
    assert v1['installed_event_id'] == 'L-INSTALL'
    # V2 was accepted but never put into service: it is stopped in stores.
    v2 = snap['serials']['V2']
    assert v2['status'] == 'recalled'
    assert v2['installed_event_id'] is None
    assert v2['service_blocked'] is True
    # The original accepted receipt is retained, not overwritten.
    assert v1['inspections'][0]['source'] == 'receipt'
    assert v1['inspections'][0]['qualified'] is True
    assert v1['value_cents'] == 0
    # Cash already paid to the supplier is preserved; the gap is a recovery.
    assert snap['paid_cents'] == 80000
    assert snap['qualified_count'] == 6
    assert snap['recognized_payable_cents'] == 60000
    assert snap['overpayment_cents'] == 20000
    assert snap['recovery_receivable_cents'] == 20000
    assert [r['serial'] for r in snap['recoveries']] == ['V1', 'V2']
    assert all(r['amount_cents'] == 10000 for r in snap['recoveries'])
    assert all(r['status'] == 'open' for r in snap['recoveries'])
    assert snap['recoveries'][0]['installed'] is True
    assert snap['recoveries'][1]['installed'] is False
    recon = snap['reconciliation']
    assert recon['identity_ok'] is True
    # Cash retained; further payments are blocked until the recovery resolves.
    cap = capture()
    cap.record({'id': 'L-PAY', 'kind': 'payment', 'amount': 800})
    cap.record({'id': 'L-CAL', 'kind': 'calibration_void', 'serials': ['V1', 'V2']})
    with pytest.raises(ProcurementError):
        cap.record({'id': 'L-PAY-2', 'kind': 'payment', 'amount': 10})


def test_cannot_void_an_already_rejected_unit():
    cap = capture()
    with pytest.raises(ProcurementError):
        cap.record({'id': 'CAL', 'kind': 'calibration_void', 'serial': 'V9'})


def test_receipts_cannot_exceed_order_across_deliveries():
    cap = ProcurementCapture.from_fixture(fixture(), record_receipt=False)
    cap.record({'id': 'R1', 'kind': 'receipt',
                'valves': [{'serial': f'S{i}', 'torque_nm': 55} for i in range(1, 7)]})
    # A second delivery of five would exceed the ten-unit PO.
    with pytest.raises(ProcurementError):
        cap.record({'id': 'R2', 'kind': 'receipt',
                    'valves': [{'serial': f'T{i}', 'torque_nm': 55} for i in range(1, 6)]})


def test_calibration_void_does_not_touch_the_payment_ledger():
    cap = capture()
    cap.record({'id': 'P1', 'kind': 'payment', 'amount': 800})
    before = [dict(p) for p in cap.snapshot()['payments']]
    cap.record({'id': 'CAL', 'kind': 'calibration_void', 'serial': 'V1'})
    after = cap.snapshot()['payments']
    # Same bank record; the recall is expressed as a credit/recovery, not by
    # deleting or rewriting the cash already paid.
    assert after == before
    assert cap.snapshot()['recoveries'][0]['event_id'] == 'CAL'


def test_voided_valve_cannot_be_installed_retested_or_returned():
    cap = capture()
    cap.record({'id': 'CAL', 'kind': 'calibration_void', 'serial': 'V1'})
    with pytest.raises(ProcurementError):
        cap.record({'id': 'INS', 'kind': 'install', 'serial': 'V1'})
    with pytest.raises(ProcurementError):
        cap.record({'id': 'RET', 'kind': 'return_to_supplier', 'serials': ['V1']})
    with pytest.raises(ProcurementError):
        cap.record({'id': 'AUTH', 'kind': 'rework_authorization',
                    'serials': ['V1'], 'supplier_signed': True})


def test_quarantined_valve_cannot_be_installed():
    cap = capture()
    with pytest.raises(ProcurementError):
        cap.record({'id': 'INS', 'kind': 'install', 'serial': 'V9'})


def test_duplicate_event_id_is_rejected():
    cap = capture()
    cap.record({'id': 'P1', 'kind': 'payment', 'amount': 100})
    with pytest.raises(ProcurementError):
        cap.record({'id': 'P1', 'kind': 'payment', 'amount': 100})


# ------------------------------------------------------ replay and integrity
@pytest.mark.parametrize('scenario', ['A_replacement', 'B_rework', 'late_calibration'])
def test_every_ledger_replays_identically(scenario):
    cap = capture()
    replay(cap, scenario)
    assert cap.verify() is True
    # The append-only log retains each event and revision.
    assert cap.snapshot()['revisions'] == len(fixture()['scenarios'][scenario]['events']) + 1


def test_paths_differ_in_cash_serials_and_obligation():
    a = replay(capture(), 'A_replacement')
    b = replay(capture(), 'B_rework')
    assert a['paid_cents'] == 100000 and b['paid_cents'] == 90000
    assert a['qualified_serials'][-2:] == ['V11', 'V12']
    assert b['qualified_serials'][-2:] == ['V9', 'V10']
    assert a['rework_credit_cents'] == 0 and b['rework_credit_cents'] == 10000
    # Both deliver ten qualified valves but not the same commercial outcome.
    assert a['qualified_count'] == b['qualified_count'] == 10
    assert a['settlement_complete'] and b['settlement_complete']
