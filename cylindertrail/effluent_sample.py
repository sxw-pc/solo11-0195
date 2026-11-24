"""Current arithmetic pH summary across separate wash tanks."""


def mean_ph(tanks):
    return sum(tank['ph'] for tank in tanks) / len(tanks)
