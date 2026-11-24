"""采录储位决策所需的离线证据, 每条采录带可校验的内容哈希。

Capture kinds (采录类别):
  bottle            瓶体介质类别与最新封签
  cage              储笼位置/容量
  incompatibility   场地不相容矩阵
  handler           搬运员权限
  scan              移动设备扫描
  outbound          客户出库双签 (可转换为 rules.calculate 的 dispatch 事件)
  door              笼门锁状态
  offline_receipt   离线分站占位回执
  freeze            安全冻结
"""
import hashlib
import json

REQUIRED = {
    'bottle': ('id', 'class', 'medium', 'seal', 'location'),
    'cage': ('id', 'capacity', 'position'),
    'incompatibility': ('pairs',),
    'handler': ('id', 'may_handle'),
    'scan': ('id', 'purpose', 'bottle_id', 'handler', 'at'),
    'outbound': ('id', 'bottle_id', 'version', 'effective_at', 'outbound'),
    'door': ('cage', 'locked', 'at'),
    'offline_receipt': ('id', 'branch', 'bottle_id', 'target', 'scan', 'signed_by'),
    'freeze': ('bottle_id', 'reason', 'by', 'at'),
}


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def content_hash(event):
    return hashlib.sha256(canonical(event).encode('utf-8')).hexdigest()


def capture_event(kind, **fields):
    """Build one validated capture event with its evidence hash."""
    if kind not in REQUIRED:
        raise ValueError(f'unknown capture kind: {kind}')
    missing = [key for key in REQUIRED[kind] if key not in fields]
    if missing:
        raise ValueError(f'{kind} capture missing fields: {missing}')
    event = {'kind': kind, **fields}
    event['evidence'] = content_hash(event)
    return event


def verify(event):
    """Recompute the content hash; 任何字段被改动都使证据失效。"""
    body = {key: value for key, value in event.items() if key != 'evidence'}
    return bool(event.get('evidence')) and content_hash(body) == event['evidence']


def capture_fixture(data):
    """Normalize the fixture's capture section into ordered capture events."""
    cap = data['capture']
    events = [capture_event('bottle', **bottle) for bottle in cap['bottles']]
    events += [capture_event('cage', **cage) for cage in cap['cages']]
    events.append(capture_event('incompatibility', pairs=cap['incompatible_pairs']))
    events += [capture_event('handler', **handler) for handler in cap['handlers']]
    events += [capture_event('scan', **scan) for scan in cap['scans']]
    events += [capture_event('outbound', **outbound) for outbound in cap['outbound']]
    events += [capture_event('door', **door) for door in cap['doors']]
    events += [capture_event('offline_receipt', **receipt) for receipt in cap['offline_receipts']]
    events += [capture_event('freeze', **freeze) for freeze in cap.get('freezes', [])]
    return events


def rules_events(events):
    """客户出库双签采录 → rules.calculate 可消费的 dispatch 事件。"""
    converted = []
    for event in events:
        if event['kind'] == 'outbound':
            body = {key: value for key, value in event.items() if key != 'evidence'}
            body['kind'] = 'dispatch'
            converted.append(body)
    return converted
