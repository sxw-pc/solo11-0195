import json
from pathlib import Path
from cylindertrail.deposit import paid
from cylindertrail.refund import due

def test_relayed_deposit_makes_double_refund():
    d = json.loads((Path(__file__).parents[1] / 'fixtures' / 'deposit-relay.json').read_text())
    assert paid(d['receipts']) == 200
    assert due(d['receipts'], d['damage_fee']) == 200
    assert {r['reservation'] for r in d['receipts']} == {'H7'}
