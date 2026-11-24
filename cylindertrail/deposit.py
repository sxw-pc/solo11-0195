"""Total distinct offline deposit receipt IDs."""

def paid(receipts):
    return sum({r['receipt_id']: r['amount'] for r in receipts}.values())
