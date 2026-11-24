import json
from pathlib import Path
from cylindertrail.training_credit import hours
from cylindertrail.operator_authority import decide


def test_overlapping_classes_grant_unearned_authority():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'operator-qualification.json').read_text())
    operator = data['operator']
    assert hours(operator['sessions']) == 48
    assert decide(operator) == 'authorized'
    assert operator['sessions'][0]['start'] == operator['sessions'][1]['start']
    less = {**operator, 'sessions': [operator['sessions'][0], {**operator['sessions'][1], 'credited_hours': 10}]}
    assert decide(less) == 'trainee'
