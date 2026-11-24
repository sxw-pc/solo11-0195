import json
from pathlib import Path
from cylindertrail.effluent_sample import mean_ph
from cylindertrail.discharge_gate import decide


def test_mean_ph_approves_two_unmixed_tanks():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'effluent-disposal.json').read_text())
    assert mean_ph(data['tanks']) == 9
    assert decide(data['tanks']) == 'discharge'
    stronger = [data['tanks'][0], {**data['tanks'][1], 'ph': 12}]
    assert decide(stronger) == 'hold'
