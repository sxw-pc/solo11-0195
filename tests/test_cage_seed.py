import json
from pathlib import Path
from cylindertrail.cage import free_slots
from cylindertrail.placement import accepted

def test_open_slot_hides_site_incompatibility():
    d = json.loads((Path(__file__).parents[1] / 'fixtures' / 'incompatible-cage.json').read_text())
    assert free_slots(d['cage']) == 1
    assert accepted(d['cage'], d['incoming']) is True
    assert [d['cage']['bottles'][0]['class'], d['incoming']['class']] in d['site_incompatible_pairs']
