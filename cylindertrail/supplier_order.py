"""Purchase-order terms for the valve contract.

The order is the single source of truth for *commercial* quantities and
prices.  Physical acceptance is decided per serial number in
``valve_receipt`` and the money is settled in ``procurement_capture``.
"""

from decimal import Decimal, ROUND_HALF_UP

DEFAULT_MINIMUM_TORQUE_NM = 50.0
DEFAULT_MAXIMUM_TORQUE_NM = 60.0
# A torque reading is only the centre of a measurement.  The true torque may
# sit anywhere inside the instrument uncertainty, so acceptance uses the
# worse of the two sides.
DEFAULT_TORQUE_UNCERTAINTY_NM = 2.0


def ordered_units(order):
    """Number of valve bodies ordered on the PO."""
    return int(order['quantity'])


def contract_limits(order):
    """Return ``(minimum_nm, maximum_nm)`` allowed by the contract."""
    return (float(order.get('minimum_torque_nm', DEFAULT_MINIMUM_TORQUE_NM)),
            float(order.get('maximum_torque_nm', DEFAULT_MAXIMUM_TORQUE_NM)))


def torque_uncertainty_nm(order):
    """Instrument uncertainty applied to each real torque reading."""
    return float(order.get('torque_uncertainty_nm', DEFAULT_TORQUE_UNCERTAINTY_NM))


def to_cents(value):
    """Convert a currency amount to integer cents, refusing sub-cent values."""
    if value is None:
        raise ValueError('amount is required')
    amount = Decimal(str(value)) * 100
    cents = amount.to_integral_value(rounding=ROUND_HALF_UP)
    if amount != cents:
        raise ValueError(f'amount {value!r} is not exact to cents')
    return int(cents)


def unit_price_cents(order):
    return to_cents(order['unit_price'])


def po_value_cents(order):
    return ordered_units(order) * unit_price_cents(order)


def replacement_terms(order):
    return order.get('replacement') or {}


def replacement_authorized(order):
    """Contract permits a supplier replacement for rejected units."""
    return bool(replacement_terms(order).get('authorized'))


def rework_terms(order):
    return order.get('rework') or {}


def rework_available(order):
    return bool(rework_terms(order).get('available'))


def supplier_credit_cents(order):
    """Credit granted when the supplier authorises a local rework."""
    return to_cents(rework_terms(order).get('supplier_credit', 0))
