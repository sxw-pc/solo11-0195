"""Bay stop/run commands driven by duct-wide propagation, not single points.

The baseline :func:`local_commands` only stops the bay owning the alarming
sensor and is kept as the documented unsafe reference. The real boundary in
:func:`commands` groups every bay whose return-air path is still connected
through shared ducts and open dampers: one unsafe reading on the path puts
every connected bay into the stop/evacuation sequence.

Dampers only break the path when a real receipt proves the blade is closed in
position. A sent command without a receipt, or a ``state: open`` damper, keeps
the bays connected — a target state is never treated as an executed state.
"""
from .air_monitor import NORMAL, alarming, grade_reading


def local_commands(readings, bays):
    """Baseline behaviour: stop only the bay owning an alarming sensor."""
    stopped = {r['bay'] for r in readings if alarming(r)}
    return {bay['id']: 'stop' if bay['id'] in stopped else 'run' for bay in bays}


# Backwards-compatible alias for the baseline name.
def commands(readings, bays, dampers=None):
    """Duct-wide stop/run map.

    A bay is unsafe when any of its own sensors is unsafe (including stale or
    missing sensors), or when an unsafe bay shares a still-connected return
    duct with it. Missing/stale sensors are never counted as zero.
    """
    dampers = dampers or []
    scope = propagation_scope(readings, bays, dampers)
    stopped = set()
    for bay in bays:
        own = [r for r in readings if r.get('bay') == bay['id']]
        own_unsafe = any(grade_reading(r) != NORMAL for r in own)
        declared = bay.get('sensors')
        if declared and not own:
            own_unsafe = True  # declared sensor produced no sample: fault
        if own_unsafe or bay['id'] in scope['affected_bays']:
            stopped.add(bay['id'])
    return {bay['id']: ('stop' if bay['id'] in stopped else 'run') for bay in bays}


def _connected_pairs(bays, dampers):
    """Pairs of bays that share a return duct with no closed-in-position damper."""
    pairs = set()
    ducts = {}
    for bay in bays:
        ducts.setdefault(bay['return_duct'], set()).add(bay['id'])
    sealed = {
        (d['bay'], d['return_duct'])
        for d in dampers if d.get('receipt') == 'in_position'
    }
    for duct, members in ducts.items():
        open_members = [b for b in members if (b, duct) not in sealed]
        for i, left in enumerate(open_members):
            for right in open_members[i + 1:]:
                pairs.add(frozenset((left, right)))
    return pairs


def propagation_scope(readings, bays, dampers):
    """Map every unsafe bay to the full set of bays that must stop with it.

    Connectivity is recomputed from ducts plus physical damper receipts, so a
    proven closed damper removes the bay from the propagation path while a
    mere command does not.
    """
    pairs = _connected_pairs(bays, dampers)
    unsafe = set()
    reasons = {}
    for bay in bays:
        own = [r for r in readings if r.get('bay') == bay['id']]
        if any(grade_reading(r) != NORMAL for r in own):
            unsafe.add(bay['id'])
            reasons[bay['id']] = 'sensor'
        elif bay.get('sensors') and not own:
            unsafe.add(bay['id'])
            reasons[bay['id']] = 'missing_sensor'
    affected = set(unsafe)
    changed = True
    while changed:
        changed = False
        for pair in pairs:
            if pair & affected and not pair <= affected:
                affected |= pair
                changed = True
    scope = {}
    for bay in bays:
        if bay['id'] in unsafe:
            linked = {b for pair in pairs if bay['id'] in pair for b in pair}
            scope[bay['id']] = sorted(linked | {bay['id']})
    return {
        'unsafe_bays': sorted(unsafe),
        'affected_bays': sorted(affected),
        'origin_reasons': reasons,
        'scope_by_bay': scope,
    }
