"""按步骤可执行的储位安排: 每条处置给出逐步证明, 或无法执行时的最小冲突。

出库可行性接入 rules.calculate 的既有出库交接裁定 (出库双签是否转移保管),
不是只在状态表上加一列类别。
"""
from .cage import CUSTODY_CUSTOMER, Violation, apply_step, free_slots, proof

CUSTODY_DECISION = '出库双签是否转移保管'


def accepted(cage, incoming, incompatible_pairs=()):
    """入库许可视图。不传场地矩阵时退化为只看空位的旧基线 (会错误放行)。"""
    if free_slots(cage) <= 0:
        return False
    pairs = {tuple(sorted(pair)) for pair in incompatible_pairs}
    cls = incoming.get('class')
    return not any(tuple(sorted((cls, occupant.get('class')))) in pairs
                   for occupant in cage.get('bottles', []))


def custody_of(decisions, bottle_id):
    """读取 rules.calculate 既有出库交接裁定结果。"""
    item = decisions.get(f'{bottle_id}/{CUSTODY_DECISION}')
    return item['result'] if item else None


def plan_steps(state, steps, ctx=None):
    """Dry-run a step sequence: 每个中间状态都必须合法, 否则返回第一个最小冲突。"""
    journal = []
    current = state
    for seq, step in enumerate(steps):
        try:
            nxt = apply_step(current, step, ctx)
        except Violation as violation:
            return {'executable': False,
                    'conflict': {'seq': seq, 'step': step, **violation.conflict},
                    'completed': journal}
        journal.append({'seq': seq, 'step': step,
                        'pre_proof': proof(current), 'post_proof': proof(nxt)})
        current = nxt
    return {'executable': True, 'steps': journal, 'state': current, 'proof': proof(current)}


def intake_conflicts(state, bottle_id, cage_ids=None):
    """逐候选笼给出不可入的最小冲突; 空列表表示可直接入库。"""
    conflicts = []
    for cage_id in cage_ids or sorted(state['cages']):
        try:
            apply_step(state, {'op': 'place', 'bottle': bottle_id, 'to': cage_id})
        except Violation as violation:
            conflicts.append({'cage': cage_id, **violation.conflict})
    return conflicts


def disposition_direct(state, bottle_id, cage_id):
    """处置一: 待入瓶直接进备用笼, 在笼瓶不动, 原客户出库不提前。"""
    return plan_steps(state, [{'op': 'place', 'bottle': bottle_id, 'to': cage_id}])


def disposition_outbound_first(state, out_bottle, in_bottle, decisions, scans):
    """处置二: 既有出库双签裁定与物理离站扫描齐备, 先腾空原笼再置入新瓶。

    证据不齐时返回最小冲突, 不得靠改状态字段假装可行。
    """
    cage_id = state['bottles'][out_bottle]['location']
    ctx = {'custody': {out_bottle: custody_of(decisions, out_bottle)}, 'scans': scans}
    steps = [{'op': 'outbound', 'bottle': out_bottle, 'from': cage_id},
             {'op': 'place', 'bottle': in_bottle, 'to': cage_id}]
    return plan_steps(state, steps, ctx)
