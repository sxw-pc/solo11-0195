import json
from pathlib import Path

import pytest

from cylindertrail.compressor_board import plan, plan_station, replay
from cylindertrail.power_capture import Capture
from cylindertrail.power_tariff import cost_between, price_at, segments_between

DATA = json.loads((Path(__file__).parents[1] / 'fixtures' / 'compressor-energy.json').read_text())
TARIFF = DATA['tariff']
COMPS = DATA['compressors']
LIMIT = DATA['feeder_limit_kw']
ICU_FIRST = {'order': ['O2-ICU', 'O2-OR'], 'version': 1, 'authorized_by': 'dispatch-lead'}
OR_FIRST = {'order': ['O2-OR', 'O2-ICU'], 'version': 1, 'authorized_by': 'dispatch-lead'}
EVIDENCE = {'pressure_kpa': 15000, 'flow_m3h': 30, 'tank_margin_m3': 40}


def starts(result):
    return {a['job']: a['start_minute'] for a in result['assignments']}


def execute_icu_first(cap):
    first = cap.replan(540, priority=ICU_FIRST)
    assert starts(first) == {'O2-ICU': 540, 'O2-OR': 600}
    cap.record_execution('O2-ICU', 'CP-1', 540, 600, 60, 30, EVIDENCE)
    return first


def test_seed_naive_per_order_pick_double_books_the_feeder():
    rows = plan(DATA['jobs'], TARIFF)
    assert [r['start_minute'] for r in rows] == [600, 600]
    assignments = [{**row, 'power_kw': job['power_kw'], 'gas_m3': job['gas_m3'],
                    'duration_minutes': job['duration_minutes'],
                    'deadline_minute': job['deadline_minute']}
                   for row, job in zip(rows, DATA['jobs'])]
    violations = replay(assignments, lambda minute: LIMIT)
    overloads = {v['minute'] for v in violations if v['kind'] == 'feeder_overload'}
    assert overloads == set(range(600, 660))
    assert all(v['load_kw'] == 100 for v in violations)
    reversed_tariff = [{**row, 'price_per_kwh': 3 - row['price_per_kwh']} for row in TARIFF]
    again = plan(DATA['jobs'], reversed_tariff)
    assert [r['start_minute'] for r in again] == [540, 540]  # still a shared-hour collision


def test_two_valid_orders_both_meet_deadline_and_feeder_limit():
    icu_first = plan_station(DATA['jobs'], TARIFF, LIMIT, COMPS, priority=ICU_FIRST, now=540)
    assert starts(icu_first) == {'O2-ICU': 540, 'O2-OR': 600}
    assert icu_first['total_cost'] == 160
    assert {a['job']: a['end_minute'] for a in icu_first['assignments']} == {'O2-ICU': 600, 'O2-OR': 660}
    cost_first = plan_station(DATA['jobs'], TARIFF, LIMIT, COMPS, now=540)
    assert starts(cost_first) == {'O2-OR': 540, 'O2-ICU': 600}
    assert cost_first['total_cost'] == 140
    for result in (icu_first, cost_first):
        assert result['feasible']
        assert max(row['load_kw'] for row in result['feeder_profile']) <= LIMIT
        assert all(a['end_minute'] <= 660 for a in result['assignments'])
        assert replay(result['assignments'], lambda minute: LIMIT, COMPS) == []
        machines = {a['job']: a['compressor'] for a in result['assignments']}
        assert machines == {'O2-ICU': 'CP-1', 'O2-OR': 'CP-2'}


def test_task_crossing_the_tariff_boundary_is_billed_per_segment():
    assert price_at(570, TARIFF) == 2 and price_at(600, TARIFF) == 1
    assert segments_between(570, 630, TARIFF) == [
        {'start_minute': 570, 'end_minute': 600, 'price_per_kwh': 2},
        {'start_minute': 600, 'end_minute': 630, 'price_per_kwh': 1}]
    assert cost_between(570, 630, 30, TARIFF) == 45
    job = {'id': 'O2-CROSS', 'start_options': [570], 'duration_minutes': 60,
           'deadline_minute': 660, 'power_kw': 30, 'gas_m3': 15}
    result = plan_station([job], TARIFF, LIMIT, COMPS, now=540)
    (assignment,) = result['assignments']
    assert assignment['cost'] == 45
    assert assignment['cost'] != 30 * 1  # never final price x total energy
    assert assignment['cost'] != 30 * 2  # nor start price x total energy


def test_mid_flight_derate_keeps_executed_work_and_revokes_the_rest():
    cap = Capture(DATA)
    execute_icu_first(cap)
    cap.derate(600, 30)
    result = cap.replan(600, priority=ICU_FIRST)
    assert not result['feasible']
    assert result['revoked'] == ['O2-OR']
    assert result['conflict_interval'] == (600, 660)
    assert result['executed_kwh'] == 60
    assert result['executed_cost'] == 120
    assert result['gas_m3'] == {'O2-ICU': 30, 'O2-OR': 0}
    assert result['total_cost'] == 120  # no silent overload, no re-priced totals


def test_milder_derate_still_delivers_both_orders():
    cap = Capture(DATA)
    execute_icu_first(cap)
    cap.derate(600, 40)
    result = cap.replan(600, priority=ICU_FIRST)
    assert result['feasible']
    assert starts(result) == {'O2-OR': 600}
    assert result['total_cost'] == 160


def test_derate_under_the_reversed_order_preserves_the_other_customer():
    cap = Capture(DATA)
    first = cap.replan(540, priority=OR_FIRST)
    assert starts(first) == {'O2-OR': 540, 'O2-ICU': 600}
    cap.record_execution('O2-OR', 'CP-2', 540, 600, 40, 20, {**EVIDENCE, 'flow_m3h': 20})
    cap.derate(600, 50)
    result = cap.replan(600, priority=OR_FIRST)
    assert not result['feasible']
    assert result['revoked'] == ['O2-ICU']
    assert result['conflict_interval'] == (600, 660)
    assert result['executed_cost'] == 80
    assert result['gas_m3']['O2-OR'] == 20


def test_tariff_revision_never_reprices_consumed_energy_or_revives_gas():
    cap = Capture(DATA)
    execute_icu_first(cap)
    cap.revise_tariff([{'start_minute': 540, 'end_minute': 600, 'price_per_kwh': 5},
                       {'start_minute': 600, 'end_minute': 660, 'price_per_kwh': 4}],
                      effective_minute=600)
    result = cap.replan(600)
    assert result['executed_kwh'] == 60
    assert result['executed_cost'] == 120            # frozen at the metered price
    assert result['planned_cost'] == 160             # 40 kWh at the revised 10:00 price
    assert result['total_cost'] == 280
    assert result['total_cost'] != 4 * 100           # not final price x total energy
    assert result['total_cost'] != 5 * 60 + 4 * 40   # and not re-priced history either
    assert result['gas_m3']['O2-ICU'] == 30          # tanked gas is not rolled back
    assert starts(result) == {'O2-OR': 600}


def test_partial_execution_cooldown_and_vented_gas_are_not_backfilled():
    cfg = {**DATA, 'jobs': [DATA['jobs'][0]]}
    cap = Capture(cfg)
    cap.record_execution('O2-ICU', 'CP-1', 540, 570, 60, 15, EVIDENCE)
    cap.record_shutdown('O2-ICU', 570, vented_m3=2)
    result = cap.replan(570, priority={'order': ['O2-ICU'], 'version': 1,
                                       'authorized_by': 'dispatch-lead'})
    assert result['feasible']
    (assignment,) = result['assignments']
    assert assignment['start_minute'] == 585   # 15-minute cooldown after the stop
    assert assignment['end_minute'] == 615
    assert assignment['gas_m3'] == 15          # only the missing, evidence-proven amount
    assert result['planned_cost'] == 45        # 15 min at the 09:00 price + 15 min at the 10:00 price
    assert result['executed_cost'] == 60
    assert result['total_cost'] == 105
    assert result['gas_m3']['O2-ICU'] == 15    # planned gas is not booked before it is metered
    assert result['vented_m3']['O2-ICU'] == 2  # vented gas is tracked, never credited


def test_only_evidence_proven_gas_counts_toward_the_order():
    cap = Capture(DATA)
    with pytest.raises(ValueError):
        cap.record_execution('O2-ICU', 'CP-1', 540, 600, 60, 30, {'flow_m3h': 30})
    with pytest.raises(ValueError):
        cap.record_execution('O2-ICU', 'CP-1', 540, 600, 60, 31, EVIDENCE)  # above physical output
    with pytest.raises(ValueError):
        cap.record_execution('O2-ICU', 'CP-1', 540, 600, 60, 30, {**EVIDENCE, 'tank_margin_m3': 10})
    assert cap.executed == []


def test_customer_priority_change_requires_an_authorized_version():
    cap = Capture(DATA)
    first = cap.replan(540, priority=OR_FIRST)
    assert starts(first) == {'O2-OR': 540, 'O2-ICU': 600}
    with pytest.raises(PermissionError):
        cap.replan(540, priority={'order': ['O2-ICU', 'O2-OR'], 'version': 1})
    with pytest.raises(PermissionError):
        cap.replan(540, priority={'order': ['O2-ICU', 'O2-OR'], 'version': 1,
                                  'authorized_by': 'dispatch-lead'})
    swapped = cap.replan(540, priority={'order': ['O2-ICU', 'O2-OR'], 'version': 2,
                                        'authorized_by': 'dispatch-lead'})
    assert starts(swapped) == {'O2-ICU': 540, 'O2-OR': 600}
    assert swapped['priority_version'] == 2


def test_tank_capacity_limits_force_a_resolve_not_a_hardcoded_order():
    cfg = json.loads(json.dumps(DATA))
    cfg['tank'] = {'capacity_m3': 35}
    icu_first = Capture(cfg).replan(540, priority=ICU_FIRST)
    assert starts(icu_first) == {'O2-ICU': 540}
    assert icu_first['revoked'] == ['O2-OR']
    or_first = Capture(cfg).replan(540, priority=OR_FIRST)
    assert starts(or_first) == {'O2-OR': 540}
    assert or_first['revoked'] == ['O2-ICU']
