"""Estimate the returnable deposit from collected receipts."""
from .deposit import paid

def due(receipts, damage_fee=0):
    return max(0, paid(receipts) - damage_fee)
