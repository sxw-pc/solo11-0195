"""Current sitewide mean pressure from hospital branch readings."""


def mean_bar(readings):
    return sum(row['outlet_bar'] for row in readings) / len(readings)
