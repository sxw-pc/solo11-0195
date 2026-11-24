"""Measure pressure loss between two offline bottle samples."""

def drop(readings):
    first, last = readings[0], readings[-1]
    return first['pressure'] - last['pressure']
