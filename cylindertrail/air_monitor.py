"""Current fixed alarm on a bay's gas concentration reading."""


def alarming(reading):
    return reading['gas_ppm'] >= reading['alarm_ppm']
