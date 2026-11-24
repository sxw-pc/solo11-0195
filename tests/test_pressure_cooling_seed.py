import json
from pathlib import Path
from cylindertrail.pressure import drop
from cylindertrail.leak_triage import status

def test_pressure_drop_cannot_separate_cooling_and_leak():
    d = json.loads((Path(__file__).parents[1] / 'fixtures' / 'pressure-cooling.json').read_text())
    assert drop(d['readings']) == 10
    assert status(d['readings']) == 'isolate'
    assert 100 * 270 / 300 == 90
