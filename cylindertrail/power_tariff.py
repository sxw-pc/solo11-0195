"""Station electrical price by one-hour interval."""


def price_at(minute, tariff):
    return next(row['price_per_kwh'] for row in tariff if row['start_minute'] <= minute < row['end_minute'])
