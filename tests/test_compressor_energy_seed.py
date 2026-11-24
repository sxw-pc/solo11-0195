import json
from pathlib import Path
from cylindertrail.power_tariff import price_at
from cylindertrail.compressor_board import plan


def test_cheapest_individual_start_overloads_feeder():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'compressor-energy.json').read_text())
    assert price_at(600, data['tariff']) == 1
    rows = plan(data['jobs'], data['tariff'])
    assert [r['start_minute'] for r in rows] == [600, 600]
    assert sum(job['power_kw'] for job in data['jobs']) > data['feeder_limit_kw']
    reversed_tariff = [{**row, 'price_per_kwh': 3 - row['price_per_kwh']} for row in data['tariff']]
    assert [r['start_minute'] for r in plan(data['jobs'], reversed_tariff)] == [540, 540]
