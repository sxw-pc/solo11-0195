"""Per-tank discharge verdicts and the daily outfall quota gate.

Nothing here ever combines two tank readings into one number.  A batch
verdict is admissible only when every tank is individually discharge-ready
and the total volume fits inside the remaining daily quota.  Tanks that have
not been neutralized with a passing independent endpoint re-test cannot pass,
no matter what an aggregate pH suggests.
"""
from .effluent_sample import DISCHARGE_MAX_PH, DISCHARGE_MIN_PH


def tank_discharge_verdict(tank, lower=DISCHARGE_MIN_PH, upper=DISCHARGE_MAX_PH):
    """Verdict for one tank's current physical state.

    ``tank`` is a ledger state object carrying the original sample, the
    neutralization/endpoint record (when applicable) and any freeze flag.
    """
    if tank.get('frozen'):
        return {'allowed': False, 'reason': '槽已被冻结，等待超标调查，不得进排口'}
    if tank.get('location') in ('carrier', 'terminal'):
        return {'allowed': False, 'reason': '槽内水已移交承运人/终端，本站无权再开阀'}
    sample = tank.get('endpoint_sample') or tank.get('sample')
    if not sample:
        return {'allowed': False, 'reason': '缺少本槽独立采样，不能排放'}
    if not sample.get('calibration_ok', False):
        return {'allowed': False, 'reason': '采样校准不可采信，不能排放'}
    ph = float(sample['ph'])
    error = float(sample.get('ph_error', 0.0))
    if not (lower <= ph <= upper and ph + error <= upper and ph - error >= lower):
        return {
            'allowed': False,
            'reason': f'本槽复测 pH {ph:g}±{error:g} 不在排口窗口 {lower:g}–{upper:g}',
        }
    if tank.get('needs_neutralization') and not tank.get('neutralized'):
        return {'allowed': False, 'reason': '碱性槽未完成中和并独立复测，禁止进市政排口'}
    return {'allowed': True, 'reason': '本槽独立采样合格，可以进排口'}


def batch_verdict(tanks, lower=DISCHARGE_MIN_PH, upper=DISCHARGE_MAX_PH):
    """A whole batch may discharge only when every tank passes on its own."""
    per_tank = {tank['id']: tank_discharge_verdict(tank, lower, upper) for tank in tanks}
    total_litres = sum(float(tank['litres']) for tank in tanks)
    if any(not item['allowed'] for item in per_tank.values()):
        return {'verdict': 'hold', 'per_tank': per_tank, 'total_litres': total_litres,
                'reason': '存在不能独立合格的槽，禁止按整批排放'}
    return {'verdict': 'discharge-eligible', 'per_tank': per_tank,
            'total_litres': total_litres,
            'reason': '逐槽合格；仍须通过当日剩余限额与实际阀门计量'}


def discharged_today(discharge_events, day):
    """Effective volume already released through the outfall on ``day``."""
    return sum(
        float(event['effective_litres'])
        for event in discharge_events
        if event['kind'] == 'valve-closed' and event['day'] == day
    )


def remaining_quota(discharge_events, day, daily_limit_litres):
    used = discharged_today(discharge_events, day)
    return float(daily_limit_litres) - used


def authorize_discharge(tank, requested_litres, discharge_events, day, daily_limit_litres,
                        lower=DISCHARGE_MIN_PH, upper=DISCHARGE_MAX_PH):
    """Gate one valve opening: tank eligibility plus the daily quota."""
    verdict = tank_discharge_verdict(tank, lower, upper)
    if not verdict['allowed']:
        return {'allowed': False, 'reason': verdict['reason']}
    requested_litres = float(requested_litres)
    if requested_litres > float(tank['litres']):
        return {'allowed': False,
                'reason': f'请求排放 {requested_litres:g}L 超过槽内余水 {float(tank["litres"]):g}L'}
    remaining = remaining_quota(discharge_events, day, daily_limit_litres)
    if requested_litres > remaining:
        return {
            'allowed': False,
            'reason': (f'当日剩余限额 {remaining:g}L 不足 {requested_litres:g}L；'
                       f'已计入 {discharged_today(discharge_events, day):g}L，'
                       f'日限额 {float(daily_limit_litres):g}L'),
        }
    return {'allowed': True, 'remaining_before': remaining, 'reason': '允许开阀'}


def carrier_authorized(carrier, litres, media, day):
    """Check the hazardous-waste carrier before custody leaves the site."""
    if not carrier.get('authorized') or carrier.get('permit_revoked_at'):
        return {'allowed': False, 'reason': '承运人废物介质许可缺失或已被撤销'}
    if day > carrier.get('permit_valid_until', ''):
        return {'allowed': False, 'reason': '承运人许可已过期'}
    if media not in carrier.get('licensed_media', ()):
        return {'allowed': False, 'reason': f'许可不含废物介质 {media}'}
    if float(litres) > float(carrier['capacity_litres']):
        return {'allowed': False,
                'reason': f'单车容量 {float(carrier["capacity_litres"]):g}L 不足 {float(litres):g}L'}
    return {'allowed': True, 'reason': '承运人许可、介质与容量均满足'}
