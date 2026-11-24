"""储笼可证状态: 储位兼容、离线占用、安全冻结与可回放的跨笼迁移账。

任一计划的每个中间时刻都必须满足: 不相容瓶不共笼、容量不超额、一瓶仅占一处。
每一步迁移在 SQLite 账中留下前置状态、完成证据与内容证明, 中断后只补做未完成动作。
"""
import copy
import hashlib
import json

from .cage_capture import verify

INTAKE = 'INTAKE'
CUSTOMER = 'CUSTOMER'
CUSTODY_CUSTOMER = '用气单位'

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS cage_step (
  plan_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  step TEXT NOT NULL,
  evidence TEXT NOT NULL,
  pre_state TEXT NOT NULL,
  post_state TEXT NOT NULL,
  pre_proof TEXT NOT NULL,
  post_proof TEXT NOT NULL,
  PRIMARY KEY (plan_id, seq));
CREATE TABLE IF NOT EXISTS cage_freeze (
  bottle_id TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  evidence TEXT NOT NULL,
  frozen_at TEXT NOT NULL);
"""


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def proof(state):
    return hashlib.sha256(canonical(state).encode('utf-8')).hexdigest()


def free_slots(cage):
    """Raw slot view of one cage dict (上游占位数据), 不含兼容与离线占用。"""
    return cage['capacity'] - len(cage['bottles'])


class Violation(Exception):
    """A step that would break a safety invariant; carries the minimal conflict."""

    def __init__(self, conflict):
        self.conflict = conflict
        super().__init__(canonical(conflict))


class PlanInterrupted(Exception):
    """Raised after a committed step to simulate a handling interruption."""

    def __init__(self, plan_id, completed):
        self.plan_id = plan_id
        self.completed = completed
        super().__init__(f'{plan_id} interrupted after {completed} committed steps')


def build_state(events, freezes=None):
    """Build the provable site state from capture events (and known freezes)."""
    cages, bottles, pairs = {}, {}, set()
    door_events, captured_freezes = {}, {}
    for event in events:
        kind = event['kind']
        if kind == 'cage':
            cages[event['id']] = {'capacity': event['capacity'], 'position': event['position'],
                                  'door_locked': False, 'bottles': []}
        elif kind == 'door':
            door_events.setdefault(event['cage'], []).append((event['at'], bool(event['locked'])))
        elif kind == 'bottle':
            bottles[event['id']] = {'class': event['class'], 'medium': event['medium'],
                                    'seal': event['seal'], 'location': event['location']}
        elif kind == 'incompatibility':
            pairs = {tuple(sorted(pair)) for pair in event['pairs']}
        elif kind == 'freeze':
            captured_freezes[event['bottle_id']] = {'reason': event['reason'],
                                                    'evidence': event['evidence'],
                                                    'at': event['at']}
    for cage_id, history in door_events.items():
        if cage_id in cages:
            cages[cage_id]['door_locked'] = sorted(history)[-1][1]
    state = {'cages': cages, 'bottles': bottles,
             'incompatible_pairs': sorted([list(pair) for pair in pairs]),
             'applied_moves': [],
             'frozen': {**captured_freezes, **dict(freezes or {})}}
    for bottle_id, bottle in bottles.items():
        location = bottle['location']
        if location in cages:
            cages[location]['bottles'].append(bottle_id)
        elif location not in (INTAKE, CUSTOMER):
            raise ValueError(f'unknown location: {location}')
    for cage in cages.values():
        cage['bottles'].sort()
    violations = validate_state(state)
    if violations:
        raise ValueError('captured state violates invariants: ' + canonical(violations))
    return state


def validate_state(state):
    """Invariants that must hold at every intermediate moment of any plan."""
    violations = []
    pairs = {tuple(sorted(pair)) for pair in state['incompatible_pairs']}
    member = {}
    for cage_id, cage in sorted(state['cages'].items()):
        if len(cage['bottles']) > cage['capacity']:
            violations.append({'reason': 'capacity', 'cage': cage_id,
                               'capacity': cage['capacity'], 'occupants': list(cage['bottles'])})
        for index, first in enumerate(cage['bottles']):
            if first in member:
                violations.append({'reason': 'two_places', 'bottle': first,
                                   'cages': [member[first], cage_id]})
            member.setdefault(first, cage_id)
            for second in cage['bottles'][index + 1:]:
                classes = tuple(sorted((state['bottles'][first]['class'],
                                        state['bottles'][second]['class'])))
                if classes in pairs:
                    violations.append({'reason': 'incompatible', 'cage': cage_id,
                                       'bottles': [first, second], 'classes': list(classes)})
    for bottle_id, bottle in sorted(state['bottles'].items()):
        location = bottle['location']
        if location in state['cages']:
            if member.get(bottle_id) != location:
                violations.append({'reason': 'location_mismatch',
                                   'bottle': bottle_id, 'location': location})
        elif location in (INTAKE, CUSTOMER):
            if bottle_id in member:
                violations.append({'reason': 'location_mismatch',
                                   'bottle': bottle_id, 'location': location})
        else:
            violations.append({'reason': 'unknown_location',
                               'bottle': bottle_id, 'location': location})
    return violations


def available_slots(state, cage_id):
    cage = state['cages'][cage_id]
    return cage['capacity'] - len(cage['bottles'])


def incompatible_with(state, cage_id, bottle_id):
    """Minimal conflict if bottle_id may not share cage_id, else None."""
    pairs = {tuple(sorted(pair)) for pair in state['incompatible_pairs']}
    cls = state['bottles'][bottle_id]['class']
    for occupant in state['cages'][cage_id]['bottles']:
        other = state['bottles'][occupant]['class']
        if tuple(sorted((cls, other))) in pairs:
            return {'reason': 'incompatible', 'cage': cage_id, 'bottle': bottle_id,
                    'occupant': occupant, 'classes': sorted([cls, other])}
    return None


def occupancy_proof(state):
    """储位兼容与离线占用的可证状态: 内容证明 + 占用/兼容/冻结事实。"""
    return {'sha256': proof(state),
            'occupancy': {bottle_id: bottle['location']
                          for bottle_id, bottle in sorted(state['bottles'].items())},
            'cages': {cage_id: {'free': available_slots(state, cage_id),
                                'occupants': list(cage['bottles'])}
                      for cage_id, cage in sorted(state['cages'].items())},
            'applied_moves': list(state['applied_moves']),
            'frozen': sorted(state['frozen']),
            'violations': validate_state(state)}


def _open_cage(state, cage_id):
    cage = state['cages'].get(cage_id)
    if cage is None:
        raise Violation({'reason': 'unknown_cage', 'cage': cage_id})
    if cage['door_locked']:
        raise Violation({'reason': 'door_locked', 'cage': cage_id})
    return cage


def _enter_cage(state, bottle_id, cage_id):
    cage = _open_cage(state, cage_id)
    if available_slots(state, cage_id) <= 0:
        raise Violation({'reason': 'capacity', 'cage': cage_id,
                         'capacity': cage['capacity'], 'occupants': list(cage['bottles'])})
    conflict = incompatible_with(state, cage_id, bottle_id)
    if conflict:
        raise Violation(conflict)
    return cage


def apply_step(state, step, ctx=None):
    """One validated transition; returns a new state, never mutates the input."""
    ctx = ctx or {}
    new = copy.deepcopy(state)
    op = step.get('op')
    bottle_id = step.get('bottle')
    bottle = new['bottles'].get(bottle_id)
    if bottle is None:
        raise Violation({'reason': 'unknown_bottle', 'bottle': bottle_id})
    if bottle_id in new['frozen']:
        raise Violation({'reason': 'frozen', 'bottle': bottle_id,
                         'freeze': new['frozen'][bottle_id]})
    if op == 'place':
        _place(new, bottle, bottle_id, step)
    elif op == 'move':
        _move(new, bottle, bottle_id, step)
    elif op == 'outbound':
        _outbound(new, bottle, bottle_id, step, ctx)
    else:
        raise Violation({'reason': 'unknown_op', 'op': op})
    violations = validate_state(new)
    if violations:
        raise Violation({'reason': 'invariant', 'violations': violations})
    return new


def _place(state, bottle, bottle_id, step):
    if bottle['location'] != INTAKE:
        raise Violation({'reason': 'not_at_intake', 'bottle': bottle_id,
                         'location': bottle['location']})
    cage = _enter_cage(state, bottle_id, step['to'])
    bottle['location'] = step['to']
    cage['bottles'].append(bottle_id)
    cage['bottles'].sort()
    if step.get('move'):
        state['applied_moves'].append(step['move'])


def _move(state, bottle, bottle_id, step):
    source = step.get('from')
    if bottle['location'] != source:
        raise Violation({'reason': 'not_in_source', 'bottle': bottle_id,
                         'expected': source, 'location': bottle['location']})
    _open_cage(state, source)
    cage = _enter_cage(state, bottle_id, step['to'])
    state['cages'][source]['bottles'].remove(bottle_id)
    bottle['location'] = step['to']
    cage['bottles'].append(bottle_id)
    cage['bottles'].sort()
    if step.get('move'):
        state['applied_moves'].append(step['move'])


def _outbound(state, bottle, bottle_id, step, ctx):
    if bottle['location'] == CUSTOMER:
        raise Violation({'reason': 'already_released', 'bottle': bottle_id})
    source = step.get('from')
    if source is not None and bottle['location'] != source:
        raise Violation({'reason': 'not_in_source', 'bottle': bottle_id,
                         'expected': source, 'location': bottle['location']})
    custody = (ctx.get('custody') or {}).get(bottle_id)
    if custody != CUSTODY_CUSTOMER:
        raise Violation({'reason': 'custody_not_transferred', 'bottle': bottle_id,
                         'decision': custody or '无出库裁定'})
    scans = [scan for scan in ctx.get('scans', [])
             if scan.get('bottle_id') == bottle_id and scan.get('purpose') == 'offsite']
    if not scans:
        raise Violation({'reason': 'offsite_scan_missing', 'bottle': bottle_id})
    location = bottle['location']
    if location in state['cages']:
        _open_cage(state, location)
        state['cages'][location]['bottles'].remove(bottle_id)
    bottle['location'] = CUSTOMER


def merge_offline(state, receipts, scans, handlers):
    """合并离线分站占位回执。

    规范序(回执 id)确定结果, 与回传先后无关; 已执行移动留在账上,
    后到回执不得抢走另一班组已占的储位。重放同一批回执幂等。
    """
    new = copy.deepcopy(state)
    scans_by_id = {scan['id']: scan for scan in scans}
    conflicts = []
    for receipt in sorted(receipts, key=lambda item: item['id']):
        if receipt['id'] in new['applied_moves']:
            continue
        conflict = _receipt_conflict(new, receipt, scans_by_id, handlers)
        if conflict is None:
            try:
                new = apply_step(new, {'op': 'place', 'bottle': receipt['bottle_id'],
                                       'to': receipt['target'], 'move': receipt['id']})
            except Violation as violation:
                conflict = violation.conflict
        if conflict is not None:
            conflicts.append({'move': receipt['id'], 'conflict': conflict})
    return new, conflicts


def _receipt_conflict(state, receipt, scans_by_id, handlers):
    if not verify(receipt):
        return {'reason': 'receipt_evidence', 'move': receipt['id']}
    bottle = state['bottles'].get(receipt['bottle_id'])
    if bottle is None:
        return {'reason': 'unknown_bottle', 'bottle': receipt['bottle_id']}
    scan = scans_by_id.get(receipt['scan'])
    if (scan is None or not verify(scan) or scan['bottle_id'] != receipt['bottle_id']
            or scan.get('cage') != receipt['target'] or scan['handler'] != receipt['signed_by']):
        return {'reason': 'scan_mismatch', 'scan': receipt['scan']}
    handler = handlers.get(receipt['signed_by'])
    if handler is None or bottle['class'] not in handler['may_handle']:
        return {'reason': 'handler_permission',
                'handler': receipt['signed_by'], 'class': bottle['class']}
    return None


def ensure_ledger(db):
    db.executescript(LEDGER_SCHEMA)
    db.commit()


def freeze_bottle(db, bottle_id, reason, evidence, at):
    """记录安全冻结; 已存在的旧冻结一律保留, 不得覆盖。返回是否新冻结。"""
    ensure_ledger(db)
    cursor = db.execute('INSERT OR IGNORE INTO cage_freeze VALUES (?,?,?,?)',
                        (bottle_id, reason, evidence, at))
    db.commit()
    return cursor.rowcount == 1


def load_freezes(db):
    ensure_ledger(db)
    return {row[0]: {'reason': row[1], 'evidence': row[2], 'at': row[3]}
            for row in db.execute(
                'SELECT bottle_id, reason, evidence, frozen_at FROM cage_freeze ORDER BY bottle_id')}


def journal_rows(db, plan_id):
    ensure_ledger(db)
    cols = ('seq', 'step', 'evidence', 'pre_state', 'post_state', 'pre_proof', 'post_proof')
    return [dict(zip(cols, row)) for row in db.execute(
        'SELECT seq, step, evidence, pre_state, post_state, pre_proof, post_proof '
        'FROM cage_step WHERE plan_id=? ORDER BY seq', (plan_id,))]


def _step_evidence(step, ctx):
    ctx = ctx or {}
    evidence = {}
    if step.get('op') == 'outbound':
        evidence['custody'] = (ctx.get('custody') or {}).get(step.get('bottle'))
        evidence['scans'] = [scan['id'] for scan in ctx.get('scans', [])
                             if scan.get('bottle_id') == step.get('bottle')
                             and scan.get('purpose') == 'offsite']
    if step.get('move'):
        evidence['move'] = step['move']
    return evidence


def execute_plan(db, plan_id, steps, initial_state, ctx=None, stop_after=None):
    """Execute steps, committing one replayable journal row per step.

    已入账步骤在恢复时原样保留 (不重复放行、不重复出库), 只补做尚未完成的动作;
    账链与初始状态不一致时报错而不是另起分支。
    """
    ensure_ledger(db)
    done = journal_rows(db, plan_id)
    journal = []
    state = copy.deepcopy(initial_state)
    if done:
        if done[0]['pre_proof'] != proof(initial_state):
            raise ValueError('journal pre-state does not match the given initial state')
        for prev, nxt in zip(done, done[1:]):
            if prev['post_proof'] != nxt['pre_proof']:
                raise ValueError('journal chain broken')
        for row in done:
            if row['seq'] >= len(steps) or row['step'] != canonical(steps[row['seq']]):
                raise ValueError(f'plan changed at seq {row["seq"]}')
            if (proof(json.loads(row['pre_state'])) != row['pre_proof']
                    or proof(json.loads(row['post_state'])) != row['post_proof']):
                raise ValueError('journal state tampered')
        state = json.loads(done[-1]['post_state'])
        journal = [{'seq': row['seq'], 'status': 'kept',
                    'pre_proof': row['pre_proof'], 'post_proof': row['post_proof']}
                   for row in done]
    executed = 0
    for seq in range(len(done), len(steps)):
        step = steps[seq]
        nxt = apply_step(state, step, ctx)
        db.execute('INSERT INTO cage_step VALUES (?,?,?,?,?,?,?,?)',
                   (plan_id, seq, canonical(step), canonical(_step_evidence(step, ctx)),
                    canonical(state), canonical(nxt), proof(state), proof(nxt)))
        db.commit()
        journal.append({'seq': seq, 'status': 'done',
                        'pre_proof': proof(state), 'post_proof': proof(nxt)})
        state = nxt
        executed += 1
        if stop_after is not None and executed >= stop_after:
            raise PlanInterrupted(plan_id, len(done) + executed)
    return {'state': state, 'journal': journal, 'complete': True}
