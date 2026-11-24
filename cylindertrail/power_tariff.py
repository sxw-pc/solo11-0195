"""Station electrical price by one-hour interval."""


def price_at(minute, tariff):
    return next(row['price_per_kwh'] for row in tariff if row['start_minute'] <= minute < row['end_minute'])


def segments_between(start, end, tariff):
    """Split [start, end) at tariff boundaries so each piece bills exactly one price."""
    if end <= start:
        return []
    pieces = []
    minute = start
    while minute < end:
        row = next(r for r in tariff if r['start_minute'] <= minute < r['end_minute'])
        piece_end = min(end, row['end_minute'])
        pieces.append({'start_minute': minute, 'end_minute': piece_end,
                       'price_per_kwh': row['price_per_kwh']})
        minute = piece_end
    return pieces


def energy_kwh(power_kw, minutes):
    return power_kw * minutes / 60


def cost_between(start, end, power_kw, tariff):
    """Bill every real metering segment at its own price; never flat-rate the total."""
    return sum(energy_kwh(power_kw, piece['end_minute'] - piece['start_minute'])
               * piece['price_per_kwh']
               for piece in segments_between(start, end, tariff))
