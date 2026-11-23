import json
import tempfile
import unittest
from pathlib import Path

from cylindertrail.store import audit, connect, import_file, report

ROOT = Path(__file__).resolve().parents[1]


class CylinderTrailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = connect(Path(self.temp.name) / 'state.sqlite')
        self.addCleanup(self.db.close)

    def load(self, kind, name):
        return import_file(self.db, kind, ROOT / 'fixtures' / name)

    def result(self, name):
        return report(self.db)['decisions']['CYL-1/' + name]['result']

    def test_inspection_fill_and_dispatch_late_recall(self):
        for kind, name in [('bottle', 'bottle-ok.json'), ('fill', 'fill-ok.json'),
                           ('dispatch', 'dispatch-ok.json')]:
            self.load(kind, name)
        self.assertEqual(self.result('出库双签是否转移保管'), '用气单位')
        self.assertEqual(self.result('充装封签是否与批次同源'), '同源')
        self.load('bottle', 'bottle-bad.json')
        self.assertEqual(self.result('定检证书是否覆盖充装时刻'), '无效')
        self.assertEqual(self.result('出库双签是否转移保管'), '用气单位')
        self.load('fill', 'fill-bad.json')
        self.load('dispatch', 'dispatch-bad.json')
        self.assertEqual(self.result('批次召回是否追踪在外瓶'), '在外召回')
        self.assertEqual(self.result('召回送达是否解除用气风险'), '仅通知')
        self.assertTrue(audit(self.db)['ok'])
        self.assertEqual(len(report(self.db)['entities']), 3)
        self.assertEqual(len(report(self.db)['business_records']), 12)

    def test_identity_conflict_pressure_and_return_contamination(self):
        self.load('bottle', 'bottle-ok.json')
        self.load('fill', 'fill-ok.json')
        self.load('dispatch', 'dispatch-ok.json')
        modified = json.loads((ROOT / 'fixtures' / 'bottle-bad.json').read_text(encoding='utf-8'))
        modified[0]['volume'] = 20
        bottle_path = Path(self.temp.name) / 'identity.json'
        bottle_path.write_text(json.dumps(modified), encoding='utf-8')
        import_file(self.db, 'bottle', bottle_path)
        self.assertEqual(self.result('瓶号冲突是否冻结身份'), '冻结')
        fill = json.loads((ROOT / 'fixtures' / 'fill-bad.json').read_text(encoding='utf-8'))
        fill[0]['quantity'] = 50
        fill_path = Path(self.temp.name) / 'overfill.json'
        fill_path.write_text(json.dumps(fill), encoding='utf-8')
        import_file(self.db, 'fill', fill_path)
        self.assertIn('超限', self.result('温压换算是否超瓶体额定量'))
        dispatch = json.loads((ROOT / 'fixtures' / 'dispatch-bad.json').read_text(encoding='utf-8'))
        dispatch[0]['return'] = {'station_signature': 'ST1', 'residual_pressure': 12,
                                  'max_residual_pressure': 5, 'contaminated': False}
        dispatch_path = Path(self.temp.name) / 'return.json'
        dispatch_path.write_text(json.dumps(dispatch), encoding='utf-8')
        import_file(self.db, 'dispatch', dispatch_path)
        self.assertEqual(self.result('退瓶残压是否要求隔离'), '隔离')

    def test_replay_and_atomic_collision(self):
        self.load('bottle', 'bottle-ok.json')
        self.assertTrue(self.load('bottle', 'bottle-ok.json')['idempotent'])
        before = report(self.db)
        data = json.loads((ROOT / 'fixtures' / 'bottle-ok.json').read_text(encoding='utf-8'))
        data[0]['volume'] = 99
        target = Path(self.temp.name) / 'collision.json'
        target.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            import_file(self.db, 'bottle', target)
        self.assertEqual(report(self.db), before)

    def test_media_purity_seal_and_unsigned_dispatch(self):
        self.load('bottle', 'bottle-ok.json')
        self.load('fill', 'fill-ok.json')
        self.load('dispatch', 'dispatch-ok.json')
        fill = json.loads((ROOT / 'fixtures' / 'fill-bad.json').read_text(encoding='utf-8'))
        fill[0]['medium'] = 'N2'
        fill[0]['batch']['purity'] = 90
        fill[0]['batch']['recalled'] = False
        fill[0]['seal']['receipt_id'] = 'WRONG'
        target = Path(self.temp.name) / 'bad-fill.json'
        target.write_text(json.dumps(fill), encoding='utf-8')
        import_file(self.db, 'fill', target)
        self.assertEqual(self.result('瓶体许可介质是否匹配'), '错配')
        self.assertEqual(self.result('气体批次纯度是否允许充装'), '隔离')
        self.assertEqual(self.result('充装封签是否与批次同源'), '失配')
        dispatch = json.loads((ROOT / 'fixtures' / 'dispatch-bad.json').read_text(encoding='utf-8'))
        dispatch[0]['outbound']['customer_signature'] = None
        target.write_text(json.dumps(dispatch), encoding='utf-8')
        import_file(self.db, 'dispatch', target)
        self.assertEqual(self.result('出库双签是否转移保管'), '充装站')

    def test_same_batch_recall_reaches_every_dispatched_bottle(self):
        for kind in ('bottle', 'fill', 'dispatch'):
            self.load(kind, f'{kind}-ok.json')
        self.assertEqual(self.result('批次召回是否追踪在外瓶'), '无召回')
        bottle = json.loads((ROOT / 'fixtures' / 'bottle-ok.json').read_text(encoding='utf-8'))
        bottle[0].update(id='B-OTHER', bottle_id='CYL-2')
        target = Path(self.temp.name) / 'other.json'
        target.write_text(json.dumps(bottle), encoding='utf-8')
        import_file(self.db, 'bottle', target)
        fill = json.loads((ROOT / 'fixtures' / 'fill-ok.json').read_text(encoding='utf-8'))
        fill[0].update(id='F-OTHER', bottle_id='CYL-2', version=2)
        fill[0]['seal']['bottle_id'] = 'CYL-2'
        fill[0]['batch']['recalled'] = True
        target.write_text(json.dumps(fill), encoding='utf-8')
        import_file(self.db, 'fill', target)
        self.assertEqual(self.result('批次召回是否追踪在外瓶'), '在外召回')
        self.assertEqual(self.result('气体批次纯度是否允许充装'), '隔离')
        self.assertEqual(self.result('召回送达是否解除用气风险'), '风险未结')
        evidence = report(self.db)['decisions']['CYL-1/批次召回是否追踪在外瓶']['evidence']
        self.assertIn('F-OTHER', evidence)
        self.assertTrue(audit(self.db)['ok'])


if __name__ == '__main__':
    unittest.main()
