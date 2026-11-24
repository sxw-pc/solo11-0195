import json
from pathlib import Path
from cylindertrail.air_monitor import alarming
from cylindertrail.bay_control import commands


def test_local_stop_ignores_shared_return_duct():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'ventilation-shutdown.json').read_text())
    assert alarming(data['readings'][0])
    assert commands(data['readings'], data['bays']) == {'A': 'stop', 'B': 'run'}
    lower = {**data['readings'][0], 'gas_ppm': 800}
    assert commands([lower, data['readings'][1]], data['bays']) == {'A': 'run', 'B': 'run'}
