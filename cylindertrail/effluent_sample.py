"""Per-tank sampling evidence for wash-tank effluent.

Two tanks that have never been transferred into the same homogenization
vessel are separate sampling objects.  pH is a logarithmic quantity, so an
arithmetic mean of two pH readings is not the pH of any real sample and can
never substitute for a post-mix titration and an independent re-test.
Every verdict here is computed for one tank at a time, from that tank's own
reading, calibration error and titrated alkalinity.
"""
import math

# Permitted municipal outfall window (inclusive bounds).
DISCHARGE_MIN_PH = 6.0
DISCHARGE_MAX_PH = 9.0


def verify_tank_sample(tank, calibration, lower=DISCHARGE_MIN_PH, upper=DISCHARGE_MAX_PH):
    """Verify one tank's own sample against its calibration record.

    The reading is rejected unless the calibration covers the sampling moment
    and the instrument's stated error stays inside the calibrated bound.
    A tank is directly discharge-ready only when even the worst-case reading
    (reading plus error) remains inside the outfall window.
    """
    reading = float(tank['ph'])
    error = float(tank['ph_error'])
    reasons = []
    calibration_ok = bool(
        calibration
        and not calibration.get('revoked')
        and calibration.get('valid_until', '') >= tank.get('sampled_at', '')
        and float(calibration.get('max_ph_error', math.inf)) >= error
    )
    if not calibration_ok:
        reasons.append('校准缺失、失效或误差超出校准范围，读数不可采信')
    worst_high = reading + error
    within_window = lower <= reading <= upper and worst_high <= upper and (reading - error) >= lower
    if not within_window:
        reasons.append(
            f'pH {reading:g}±{error:g} 不能在最不利方向仍落入排口窗口 {lower:g}–{upper:g}'
        )
    if calibration_ok and within_window:
        verdict = '可直接排放'
    elif calibration_ok and reading > upper:
        verdict = '须中和或外运'
    elif calibration_ok and reading < lower:
        verdict = '须回调或外运'
    else:
        verdict = '采样不可采信'
    return {
        'tank_id': tank['id'],
        'verdict': verdict,
        'calibration_ok': calibration_ok,
        'ph': reading,
        'ph_error': error,
        'ph_worst_case_high': round(worst_high, 4),
        'alkalinity_mmol_per_litre': float(tank['alkalinity_mmol_per_litre']),
        'litres': float(tank['litres']),
        'reasons': reasons,
    }


def verify_samples(tanks, calibrations, lower=DISCHARGE_MIN_PH, upper=DISCHARGE_MAX_PH):
    """Verify each tank independently; no cross-tank averaging occurs."""
    by_id = {item['tank_id']: item for item in calibrations}
    return [
        verify_tank_sample(tank, by_id.get(tank['id']), lower, upper)
        for tank in tanks
    ]


def blended_ph(parts, kw=1.0e-14):
    """Equilibrium pH of volumes actually mixed in one homogenized vessel.

    ``parts`` is a sequence of ``(litres, ph)`` pairs describing liquids that
    have physically been combined.  The balance [H+] = [OH-] - net strong
    base is solved at the shared equilibrium; an arithmetic mean of pH values
    would ignore the log scale and the water autoionization balance.  The
    result only describes a real blend.  It never authorizes discharge on its
    own: a blended batch still needs its own titration and independent
    endpoint re-test.
    """
    total = 0.0
    net_base = 0.0  # sum of V*([OH-]-[H+]) for the inputs
    for litres, ph in parts:
        litres = float(litres)
        h = 10.0 ** (-float(ph))
        net_base += litres * (kw / h - h)
        total += litres
    if total <= 0:
        raise ValueError('blend requires a positive total volume')
    b = net_base / total
    # h satisfies h = kw/h - b  ->  h^2 + b*h - kw = 0
    h = (math.sqrt(b * b + 4.0 * kw) - b) / 2.0
    return -math.log10(h)


def acid_demand_mmol(tank):
    """Strong-acid equivalents implied by the titrated alkalinity."""
    return float(tank['alkalinity_mmol_per_litre']) * float(tank['litres'])
