"""Current bulk receipt accepts a shipment by count alone."""
from .supplier_order import ordered_units


def decide(order, valves):
    return 'accepted' if len(valves) >= ordered_units(order) else 'pending'
