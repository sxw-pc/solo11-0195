import copy
import json
from pathlib import Path

import pytest

from cylindertrail import effluent_sample
from cylindertrail import discharge_gate
from cylindertrail.effluent_capture import (
    AcidStockError,
    CustodyError,
    EffluentLedger,
    EndpointFailure,
    NeutralizerCapacity,
    QuotaExceeded,
    TankFrozen,
)
from cylindertrail.effluent_sample import blended_ph, verify_samples

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'effluent-disposal.json'

DAY1 = '2026-11-24'
DAY2 = '2026-11-25'


def load_config():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def build_ledger(config):
    """Seed ledger: per-tank samples verified against their own calibrations."""
    ledger = EffluentLedger(config)
    ledger.record_sample('T-NEUTRAL', 'S-N-1', 7.0, 0.2, 2.0, 'CAL-N-001',
                         f'{DAY1}T08:10')
    ledger.record_sample('T-ALKALINE', 'S-A-1', 11.0, 0.2, 50.0, 'CAL-A-001',
                         f'{DAY1}T08:15')
    return ledger


def discharge_neutral(ledger, day=DAY1, minute='09:00'):
    hh, mm = minute.split(':')
    ledger.open_valve('T-NEUTRAL', 'V-100', f'{day}T{hh}:{mm}', 'operator-li', 1000)
    ledger.close_valve('V-100', f'{day}T{hh}:{int(mm) + 12:02d}', 'operator-li',
                       ledger.meter_reading + 1000, 'SIGNED-N-1')


# --------------------------------------------------------------- seed mis-release
def test_seed_arithmetic_batch_discharge_is_blocked_per_tank():
    config = load_config()
    # The isolated replica used to average 7 and 11 arithmetically to 9 and
    # release the whole 2000 L batch. No such averaging API exists anymore.
    assert not hasattr(effluent_sample, 'mean_ph')
    samples = verify_samples(config['tanks'], config['calibrations'])
    by_id = {item['tank_id']: item for item in samples}
    assert by_id['T-NEUTRAL']['verdict'] == '可直接排放'
    assert by_id['T-ALKALINE']['verdict'] == '须中和或外运'

    ledger = build_ledger(config)
    verdict = discharge_gate.batch_verdict(list(ledger.tanks.values()))
    assert verdict['verdict'] == 'hold'
    assert verdict['per_tank']['T-ALKALINE']['allowed'] is False

    # The pH-11 tank must not reach the outfall without neutralization.
    with pytest.raises(QuotaExceeded):
        ledger.open_valve('T-ALKALINE', 'V-200', f'{DAY1}T09:00', 'operator-li')

    # pH is logarithmic: a real equal-volume blend is not the arithmetic mean 9.
    real_blend = blended_ph([(1000, 7.0), (1000, 11.0)])
    assert real_blend == pytest.approx(10.70, abs=0.05)
    assert real_blend > 9.0
    # The stronger reading used to flip the old mean-based gate.
    stronger = copy.deepcopy(config)
    stronger['tanks'][1]['ph'] = 12.0
    strong_ledger = build_ledger(stronger)
    assert discharge_gate.batch_verdict(list(strong_ledger.tanks.values()))['verdict'] == 'hold'
    assert blended_ph([(1000, 7.0), (1000, 12.0)]) == pytest.approx(11.70, abs=0.05)


# ------------------------------------------------------------- route A
def test_route_a_neutralize_today_discharge_under_tomorrow_quota():
    ledger = build_ledger(load_config())
    discharge_neutral(ledger)
    assert ledger.discharged_on(DAY1) == 1000

    # Acid demand follows the titrated alkalinity: 50 mmol/L * 1000 L.
    ledger.neutralize('T-ALKALINE', 'EV-NEUT-A-1', f'{DAY1}T11:00',
                      dosed_acid_mmol=50000, endpoint_ph=8.2, endpoint_ph_error=0.2,
                      endpoint_sample_id='S-A-EP-1', operator='operator-wang')
    assert ledger.acid_stock == 10000
    ledger.seal_on_site('T-ALKALINE', 'EV-SEAL-A-1', f'{DAY1}T12:00', 'operator-wang')

    # The second 1000 L cannot be squeezed into today's 1500 L quota.
    with pytest.raises(QuotaExceeded):
        ledger.open_valve('T-ALKALINE', 'V-201', f'{DAY1}T13:00', 'operator-wang')

    # New day, fresh quota: the sealed, independently re-tested batch goes out.
    ledger.open_valve('T-ALKALINE', 'V-202', f'{DAY2}T08:30', 'operator-wang', 1000)
    ledger.close_valve('V-202', f'{DAY2}T08:45', 'operator-wang',
                       ledger.meter_reading + 1000, 'SIGNED-A-1')
    assert ledger.discharged_on(DAY2) == 1000
    assert ledger.discharged_total() == 2000
    assert ledger.tank('T-ALKALINE')['location'] == 'discharged'
    trace = ledger.trace('T-ALKALINE')
    assert [e['kind'] for e in trace][:3] == ['sample-recorded', 'acid-adjusted',
                                              'neutralization-completed']
    assert ledger.verify_chain()


def test_route_a_fails_when_endpoint_retest_fails():
    ledger = build_ledger(load_config())
    discharge_neutral(ledger)
    with pytest.raises(EndpointFailure):
        ledger.neutralize('T-ALKALINE', 'EV-NEUT-A-BAD', f'{DAY1}T11:00',
                          dosed_acid_mmol=50000, endpoint_ph=9.6, endpoint_ph_error=0.2,
                          endpoint_sample_id='S-A-EP-BAD', operator='operator-wang')
    tank = ledger.tank('T-ALKALINE')
    assert tank['frozen'] is True
    assert tank['neutralized'] is False
    # Failing endpoint cannot become tomorrow's inventory discharge.
    with pytest.raises(TankFrozen):
        ledger.open_valve('T-ALKALINE', 'V-203', f'{DAY2}T08:30', 'operator-wang')
    # Acid was physically consumed even though the batch failed.
    assert ledger.acid_stock == 10000
    assert ledger.discharged_total() == 1000


# ------------------------------------------------------------- route B
def test_route_b_licensed_carrier_takes_alkaline_tank_with_receipt():
    ledger = build_ledger(load_config())
    discharge_neutral(ledger)
    ledger.create_manifest('T-ALKALINE', 'MAN-A-1', f'{DAY1}T14:00')
    # Handing over on site does not complete disposal.
    ledger.handover('MAN-A-1', f'{DAY1}T15:00', 'station-chief', 'carrier-driver-77')
    assert ledger.tank('T-ALKALINE')['location'] == 'carrier'
    assert ledger.discharged_total() == 1000
    assert ledger.acid_stock == 60000
    ledger.receive_at_terminal('MAN-A-1', f'{DAY1}T18:30', 'TRC-9001', 'terminal-op-3')
    assert ledger.tank('T-ALKALINE')['location'] == 'terminal'
    # No second outfall discharge for this tank: it never entered the municipal drain.
    assert ledger.discharged_on(DAY1) == 1000


def test_route_b_collapses_if_permit_revoked_before_handover():
    ledger = build_ledger(load_config())
    discharge_neutral(ledger)
    ledger.create_manifest('T-ALKALINE', 'MAN-A-2', f'{DAY1}T14:00')
    ledger.revoke_carrier_permit(f'{DAY1}T14:20', '例行稽核撤销介质许可')
    with pytest.raises(CustodyError):
        ledger.handover('MAN-A-2', f'{DAY1}T15:00', 'station-chief', 'carrier-driver-77')
    # Even a fresh manifest is refused while the permit is revoked.
    with pytest.raises(CustodyError):
        ledger.create_manifest('T-ALKALINE', 'MAN-A-3', f'{DAY1}T15:30')
    assert ledger.tank('T-ALKALINE')['location'] == 'on-site-tank'


def test_carrier_requires_dual_signature_and_terminal_receipt():
    ledger = build_ledger(load_config())
    ledger.create_manifest('T-ALKALINE', 'MAN-A-4', f'{DAY1}T14:00')
    with pytest.raises(CustodyError):
        ledger.handover('MAN-A-4', f'{DAY1}T15:00', 'station-chief', None)
    ledger.handover('MAN-A-4', f'{DAY1}T15:05', 'station-chief', 'carrier-driver-77')
    with pytest.raises(CustodyError):
        ledger.receive_at_terminal('MAN-A-4', f'{DAY1}T18:30', None, 'terminal-op-3')
    assert ledger.tank('T-ALKALINE')['location'] == 'carrier'


# ------------------------------------------------------------- daily quota
def test_daily_limit_blocks_two_full_tanks_and_meter_overrun():
    ledger = build_ledger(load_config())
    discharge_neutral(ledger)
    # Only 500 L remain today; a full second tank is refused at valve opening.
    # Neutralize and confirm a full 1000 L request cannot open the valve.
    ledger.neutralize('T-ALKALINE', 'EV-NEUT-A-2', f'{DAY1}T11:00',
                      dosed_acid_mmol=50000, endpoint_ph=8.0, endpoint_ph_error=0.1,
                      endpoint_sample_id='S-A-EP-2', operator='operator-wang')
    with pytest.raises(QuotaExceeded):
        ledger.open_valve('T-ALKALINE', 'V-301', f'{DAY1}T12:00', 'operator-wang', 1000)
    # A partial 500 L release fits, and the meter enforces the same ceiling.
    ledger.open_valve('T-ALKALINE', 'V-302', f'{DAY1}T12:30', 'operator-wang', 500)
    with pytest.raises(QuotaExceeded):
        ledger.close_valve('V-302', f'{DAY1}T12:40', 'operator-wang',
                           ledger.meter_reading + 600, 'SIGNED-A-P')
    ledger.close_valve('V-302', f'{DAY1}T12:42', 'operator-wang',
                       ledger.meter_reading + 500, 'SIGNED-A-P')
    assert ledger.discharged_on(DAY1) == 1500
    assert ledger.tank('T-ALKALINE')['litres'] == 500


# ------------------------------------------------------------- acid stock
def test_acid_stock_cannot_be_double_committed_to_two_tanks():
    config = load_config()
    # Two alkaline batches, one 60000 mmol stock: each needs 50000 mmol.
    config['tanks'] = [
        {'id': 'T-ALKALINE', 'source': 'hydrotest-wash', 'litres': 1000, 'ph': 11,
         'ph_error': 0.2, 'alkalinity_mmol_per_litre': 50},
        {'id': 'T-ALKALINE-2', 'source': 'hydrotest-wash', 'litres': 1000, 'ph': 11,
         'ph_error': 0.2, 'alkalinity_mmol_per_litre': 50},
    ]
    config['calibrations'] = [
        {'calibration_id': 'CAL-A-001', 'tank_id': 'T-ALKALINE', 'instrument_id': 'PH-METER-2',
         'max_ph_error': 0.2, 'calibrated_at': f'{DAY1}T07:00', 'valid_until': '2026-12-23T08:00',
         'revoked': False},
        {'calibration_id': 'CAL-A-002', 'tank_id': 'T-ALKALINE-2', 'instrument_id': 'PH-METER-3',
         'max_ph_error': 0.2, 'calibrated_at': f'{DAY1}T07:00', 'valid_until': '2026-12-23T08:00',
         'revoked': False},
    ]
    ledger = EffluentLedger(config)
    ledger.record_sample('T-ALKALINE', 'S-A-1', 11.0, 0.2, 50.0, 'CAL-A-001', f'{DAY1}T08:15')
    ledger.record_sample('T-ALKALINE-2', 'S-A2-1', 11.0, 0.2, 50.0, 'CAL-A-002', f'{DAY1}T08:20')
    ledger.neutralize('T-ALKALINE', 'EV-N-1', f'{DAY1}T10:00', 50000, 8.1, 0.2,
                      'S-EP-1', 'operator-wang')
    assert ledger.acid_stock == 10000
    with pytest.raises(AcidStockError):
        ledger.neutralize('T-ALKALINE-2', 'EV-N-2', f'{DAY1}T11:00', 50000, 8.1, 0.2,
                          'S-EP-2', 'operator-wang')
    # The second tank's state and stock were untouched by the refused batch.
    assert ledger.acid_stock == 10000
    assert ledger.tank('T-ALKALINE-2')['neutralized'] is False


def test_neutralizer_capacity_and_under_dosing_are_rejected():
    ledger = build_ledger(load_config())
    with pytest.raises(AcidStockError):
        ledger.neutralize('T-ALKALINE', 'EV-NEUT-UNDER', f'{DAY1}T10:00',
                          dosed_acid_mmol=40000, endpoint_ph=8.0, endpoint_ph_error=0.2,
                          endpoint_sample_id='S-UNDER', operator='operator-wang')
    oversized = copy.deepcopy(load_config())
    oversized['neutralization_unit']['capacity_litres'] = 500
    small = EffluentLedger(oversized)
    small.record_sample('T-ALKALINE', 'S-A-1', 11.0, 0.2, 50.0, 'CAL-A-001', f'{DAY1}T08:15')
    with pytest.raises(NeutralizerCapacity):
        small.neutralize('T-ALKALINE', 'EV-NEUT-BIG', f'{DAY1}T10:00', 50000, 8.0, 0.2,
                         'S-BIG', 'operator-wang')


# ------------------------------------------------------------- late invalidation
def test_late_calibration_preserves_discharge_and_freezes_remainder():
    ledger = build_ledger(load_config())
    discharge_neutral(ledger)
    ledger.neutralize('T-ALKALINE', 'EV-NEUT-A-3', f'{DAY1}T11:00',
                      dosed_acid_mmol=50000, endpoint_ph=8.2, endpoint_ph_error=0.2,
                      endpoint_sample_id='S-A-EP-3', operator='operator-wang')
    ledger.seal_on_site('T-ALKALINE', 'EV-SEAL-A-3', f'{DAY1}T12:00', 'operator-wang')

    # Late evidence: the pH meter calibration is withdrawn after the fact.
    result = ledger.invalidate_sample(
        'T-NEUTRAL', 'EV-NC-1', f'{DAY1}T17:00',
        reason='事后核查发现校准证书已被发证机构撤销',
        late_calibration_id='CAL-N-001',
        late_sample={'sample_id': 'S-N-LATE', 'ph': 9.8, 'ph_error': 0.3},
        notify_authority='生态环境局')

    # Actual outfall flow is an immutable fact: never moved back into stock.
    assert result['discharged_litres_preserved'] == 1000
    assert ledger.discharged_total() == 1000
    assert ledger.tank('T-NEUTRAL')['discharged_litres'] == 1000
    assert len(result['cases']) == 1 and len(result['notifications']) == 1
    # All remaining on-site water is frozen pending the investigation.
    assert ledger.tank('T-ALKALINE')['frozen'] is True
    assert ledger.tank('T-NEUTRAL')['frozen'] is True
    with pytest.raises(TankFrozen):
        ledger.open_valve('T-ALKALINE', 'V-400', f'{DAY2}T08:30', 'operator-wang')
    # The frozen tank cannot leave by carrier either.
    with pytest.raises((TankFrozen, CustodyError)):
        ledger.create_manifest('T-ALKALINE', 'MAN-FROZEN', f'{DAY2}T09:00')
    assert ledger.verify_chain()


def test_calibration_error_bounds_make_reading_unverifiable():
    config = load_config()
    config['calibrations'][0]['max_ph_error'] = 0.1
    ledger = EffluentLedger(config)
    ledger.record_sample('T-NEUTRAL', 'S-N-1', 7.0, 0.2, 2.0, 'CAL-N-001', f'{DAY1}T08:10')
    ledger.record_sample('T-ALKALINE', 'S-A-1', 11.0, 0.2, 50.0, 'CAL-A-001', f'{DAY1}T08:15')
    tank = ledger.tank('T-NEUTRAL')
    assert tank['sample']['calibration_ok'] is False
    with pytest.raises(QuotaExceeded):
        ledger.open_valve('T-NEUTRAL', 'V-500', f'{DAY1}T09:00', 'operator-li')
    # A fresh in-tolerance sample restores the tank's discharge readiness.
    config['calibrations'][0]['max_ph_error'] = 0.3
    ledger2 = EffluentLedger(config)
    ledger2.record_sample('T-NEUTRAL', 'S-N-2', 7.0, 0.2, 2.0, 'CAL-N-001', f'{DAY1}T08:40')
    ledger2.open_valve('T-NEUTRAL', 'V-501', f'{DAY1}T09:00', 'operator-li', 1000)
    ledger2.close_valve('V-501', f'{DAY1}T09:12', 'operator-li',
                        ledger2.meter_reading + 1000, 'SIGNED-N-2')
    assert ledger2.discharged_total() == 1000


def test_carrier_without_matching_media_licence_is_rejected():
    config = load_config()
    config['hazard_carrier']['licensed_media'] = ['equipment-wash']
    ledger = EffluentLedger(config)
    ledger.record_sample('T-NEUTRAL', 'S-N-1', 7.0, 0.2, 2.0, 'CAL-N-001', f'{DAY1}T08:10')
    ledger.record_sample('T-ALKALINE', 'S-A-1', 11.0, 0.2, 50.0, 'CAL-A-001', f'{DAY1}T08:15')
    with pytest.raises(CustodyError):
        ledger.create_manifest('T-ALKALINE', 'MAN-X', f'{DAY1}T14:00')
    assert ledger.tank('T-ALKALINE')['manifests'] == []


def test_replay_rebuilds_identical_physical_ledger():
    ledger = build_ledger(load_config())
    discharge_neutral(ledger)
    ledger.neutralize('T-ALKALINE', 'EV-NEUT-A-4', f'{DAY1}T11:00',
                      dosed_acid_mmol=50000, endpoint_ph=8.2, endpoint_ph_error=0.2,
                      endpoint_sample_id='S-A-EP-4', operator='operator-wang')
    ledger.seal_on_site('T-ALKALINE', 'EV-SEAL-A-4', f'{DAY1}T12:00', 'operator-wang')
    raw_events = copy.deepcopy(ledger.events)

    rebuilt = EffluentLedger.replay(load_config(), raw_events)
    assert rebuilt.summary() == ledger.summary()
    assert rebuilt.trace('T-ALKALINE') == ledger.trace('T-ALKALINE')

    # Tampering with a historical event breaks the hash chain.
    tampered = copy.deepcopy(raw_events)
    tampered[0]['payload']['ph'] = 6.1
    with pytest.raises(Exception):
        EffluentLedger.replay(load_config(), tampered)
