import json
from pathlib import Path
from cylindertrail.regulator_test import mean_bar
from cylindertrail.site_commission import decide


def test_mean_pressure_commissions_site_with_unsafe_branch():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'pipe-commission.json').read_text())
    assert mean_bar(data['readings']) == 4.5
    assert decide(data['site'], data['readings']) == 'commissioned'
    assert data['readings'][1]['outlet_bar'] > data['site']['maximum_bar']
    high = [{**row, 'outlet_bar': 5.0} for row in data['readings']]
    assert decide(data['site'], high) == 'hold'
