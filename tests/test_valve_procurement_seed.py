import json
from pathlib import Path
from cylindertrail.supplier_order import ordered_units
from cylindertrail.valve_receipt import decide


def test_count_only_receipt_accepts_failed_valves():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'valve-procurement.json').read_text())
    assert ordered_units(data['order']) == 10
    assert decide(data['order'], data['valves']) == 'accepted'
    assert sum(v['torque_nm'] < data['order']['minimum_torque_nm'] for v in data['valves']) == 2
    larger_order = {**data['order'], 'quantity': 11}
    assert decide(larger_order, data['valves']) == 'pending'
