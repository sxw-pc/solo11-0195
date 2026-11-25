"""Valve procurement capture: receipt, exception negotiation, settlement.

The original data model accepted a shipment whenever the box count matched the
order quantity.  That is wrong in three independent ways this module makes
explicit and auditable:

1. **Physical acceptance** is per valve serial number and per real torque test
   record.  A reading is a measurement with uncertainty; the *worst* side must
   stay inside the contract band.  Failed serials are quarantined; they cannot
   be averaged away and one serial cannot be registered twice.
2. **Money** is recognised only for qualified units.  The supplier invoice is
   reconciled against the purchase order, the per-unit physical receipt and the
   qualified quantity.  A payment may never prepay the rejected/reworked units.
   Bank payments, supplier credits and open payable must balance to the cent.
3. **Disposition** has two contract-authorised closures that are mutually
   exclusive for the same physical valve: return the original serial and
   receive a *new* serial number for a fresh test, or keep the original serial
   and perform a supplier-authorised rework plus an independent retest.  A
   later calibration finding recalls an accepted unit without rewriting the
   receipt row or the cash already paid; it opens a recovery version instead.

The ledger is append-only: every call to :meth:`ProcurementCapture.record`
keeps the event, recomputes the state, and never edits an earlier receipt,
inspection or payment.  :meth:`ProcurementCapture.verify` replays the log to
prove the snapshot is reproducible.
"""

from __future__ import annotations

import copy

from .supplier_order import (
    contract_limits,
    ordered_units,
    po_value_cents,
    replacement_authorized,
    rework_available,
    supplier_credit_cents,
    to_cents,
    torque_uncertainty_nm,
    unit_price_cents,
)
from .valve_receipt import inspect_valve

QUALIFIED_STATUSES = frozenset({'qualified', 'reworked', 'installed'})
OPEN_RECOVERY = 'open'


class ProcurementError(ValueError):
    """A procurement event violates the contract or the physical flow."""


def _require(condition, message):
    if not condition:
        raise ProcurementError(message)


class ProcurementCapture:
    """Append-only procurement state machine for one purchase order."""

    EVENT_KINDS = frozenset({
        'receipt',
        'return_to_supplier',
        'rework_authorization',
        'rework',
        'retest',
        'replacement_arrival',
        'payment',
        'calibration_void',
        'install',
    })

    def __init__(self, order, invoice=None):
        self.order = copy.deepcopy(order)
        self.invoice = copy.deepcopy(invoice or {})
        self._events = []
        self._history = []
        self._revision = 0
        self._opening_paid_cents = to_cents(self.invoice.get('paid', 0))
        self._state = self._empty_state()
        if self._opening_paid_cents:
            self._state['payments'].append({
                'event_id': None,
                'kind': 'opening_balance',
                'at': None,
                'amount_cents': self._opening_paid_cents,
                'reference': self.invoice.get('id'),
            })

    # ------------------------------------------------------------------ setup
    @classmethod
    def from_fixture(cls, fixture, *, record_receipt=True,
                     receipt_id='receipt-initial', at=None):
        """Build a capture from the fixture shape and record the delivery."""
        order = copy.deepcopy(fixture['order'])
        # Contract authorisations may sit either on the order or at fixture
        # level; normalise them onto the order the ledger reads.
        for term in ('replacement', 'rework'):
            if term in fixture and term not in order:
                order[term] = copy.deepcopy(fixture[term])
        capture = cls(order, fixture.get('supplier_invoice'))
        if record_receipt:
            capture.record({
                'id': receipt_id,
                'kind': 'receipt',
                'at': at if at is not None else fixture.get('received_at'),
                'valves': fixture.get('valves', []),
            })
        return capture

    @staticmethod
    def _empty_state():
        return {
            'serials': {},
            'serial_order': [],
            'event_ids': set(),
            'receipts': [],
            'payments': [],
            'installs': [],
        }

    # --------------------------------------------------------------- ingestion
    def record(self, event):
        """Validate and append one event; the previous state is never mutated."""
        event = copy.deepcopy(dict(event))
        kind = event.get('kind')
        _require(kind in self.EVENT_KINDS, f'unknown procurement event kind {kind!r}')
        if not event.get('id'):
            event['id'] = f'E{len(self._events) + 1}'
        event.setdefault('at', None)
        _require(event['id'] not in self._state['event_ids'],
                 f"duplicate event id {event['id']!r}")

        new_state = copy.deepcopy(self._state)
        summary = self._dispatch(event, new_state)
        new_state['event_ids'].add(event['id'])
        self._state = new_state
        self._events.append(event)
        self._revision += 1
        self._history.append({
            'revision': self._revision,
            'event_id': event['id'],
            'kind': kind,
            'at': event.get('at'),
            'summary': summary,
        })
        return summary

    def _dispatch(self, event, state):
        handler = {
            'receipt': self._apply_receipt,
            'return_to_supplier': self._apply_return,
            'rework_authorization': self._apply_rework_authorization,
            'rework': self._apply_rework,
            'retest': self._apply_retest,
            'replacement_arrival': self._apply_replacement,
            'payment': self._apply_payment,
            'calibration_void': self._apply_calibration_void,
            'install': self._apply_install,
        }[event['kind']]
        return handler(event, state)

    # ------------------------------------------------------------- primitives
    def _serial(self, state, serial):
        _require(serial in state['serials'], f'unknown valve serial {serial!r}')
        return state['serials'][serial]

    def _add_serial(self, state, serial, origin, event):
        _require(bool(serial), 'every valve needs a unique serial number')
        _require(serial not in state['serials'],
                 f'serial {serial!r} is already registered; the same identity '
                 'cannot be counted as a second valve')
        state['serials'][serial] = {
            'serial': serial,
            'origin': origin,
            'inspections': [],
            'event_ids': [event['id']],
            'location': 'station',
            'rework_authorized': None,
            'rework': None,
            'void': None,
            'installed': None,
            'returned_event': None,
            'replaced_by': None,
            'replacement_for': [],
        }
        state['serial_order'].append(serial)

    def _inspect(self, state, serial, torque, event, source, independent=None):
        reading = torque
        _require(reading is not None,
                 f'valve {serial!r} has no real torque test record')
        try:
            reading = float(reading)
        except (TypeError, ValueError):
            raise ProcurementError(f'valve {serial!r} torque is not numeric') from None
        minimum, maximum = contract_limits(self.order)
        result = inspect_valve({'serial': serial, 'torque_nm': reading},
                               minimum, maximum, torque_uncertainty_nm(self.order))
        result.update({
            'event_id': event['id'],
            'at': event.get('at'),
            'source': source,
            'instrument': event.get('instrument'),
            'operator': event.get('operator'),
        })
        if independent is not None:
            result['independent'] = bool(independent)
        # keep the inspection as an immutable historical record
        state['serials'][serial]['inspections'].append(copy.deepcopy(result))
        state['serials'][serial]['event_ids'].append(event['id'])
        return result

    @staticmethod
    def _current(entry):
        return entry['inspections'][-1] if entry['inspections'] else None

    @classmethod
    def _qualified(cls, entry):
        current = cls._current(entry)
        return bool(current and current['qualified']
                    and not entry['void'] and entry['location'] == 'station')

    # -------------------------------------------------------------- handlers
    def _apply_receipt(self, event, state):
        valves = event.get('valves') or []
        units = ordered_units(self.order)
        _require(len(valves) > 0, 'a receipt needs at least one valve')
        already = sum(len(r['inspections']) for r in state['receipts'])
        _require(already + len(valves) <= units,
                 f'receipt would exceed the {units}-unit order '
                 f'({already} already received)')
        serials = [v.get('serial') for v in valves]
        _require(len(set(serials)) == len(serials),
                 'a delivery cannot contain the same serial number twice')
        receipt = {'event_id': event['id'], 'at': event.get('at'),
                   'inspections': []}
        for valve in valves:
            serial = valve.get('serial')
            self._add_serial(state, serial, 'initial', event)
            receipt['inspections'].append(
                self._inspect(state, serial, valve.get('torque_nm'),
                              event, 'receipt'))
        state['receipts'].append(receipt)
        return {
            'received': serials,
            'qualified': [r['serial'] for r in receipt['inspections'] if r['qualified']],
            'quarantined': [r['serial'] for r in receipt['inspections']
                            if not r['qualified']],
        }

    def _apply_return(self, event, state):
        serials = event.get('serials') or []
        _require(bool(serials), 'return_to_supplier needs serials')
        _require(len(set(serials)) == len(serials), 'duplicate serial in return')
        for serial in serials:
            entry = self._serial(state, serial)
            _require(entry['location'] == 'station',
                     f'{serial} is not physically at the station')
            _require(not entry['void'],
                     f'{serial} was voided by calibration; it is settled by a '
                     'credit/recovery, not by a replacement')
            _require(not entry['rework_authorized'],
                     f'{serial} is on the signed rework path; return and rework '
                     'are mutually exclusive dispositions')
            _require(not entry['rework'],
                     f'{serial} has already been reworked; it cannot also be '
                     'returned for replacement')
            _require(not self._qualified(entry),
                     f'{serial} is qualified; only a rejected unit may go back')
            _require(entry['replaced_by'] is None, f'{serial} already replaced')
        for serial in serials:
            entry = state['serials'][serial]
            entry['location'] = 'supplier'
            entry['returned_event'] = event['id']
            entry['event_ids'].append(event['id'])
        return {'returned': list(serials)}

    def _apply_rework_authorization(self, event, state):
        _require(rework_available(self.order),
                 'the contract does not allow supplier-authorised rework')
        _require(event.get('supplier_signed') is True,
                 'a rework authorization must be signed by the supplier')
        serials = event.get('serials') or []
        _require(bool(serials), 'rework authorization needs serials')
        _require(len(set(serials)) == len(serials), 'duplicate serial in authorization')
        for serial in serials:
            entry = self._serial(state, serial)
            _require(entry['location'] == 'station',
                     f'{serial} is not at the station; a returned unit cannot be reworked')
            _require(not entry['void'], f'{serial} is voided by calibration')
            _require(entry['rework_authorized'] is None,
                     f'{serial} already has a rework authorization')
            _require(entry['rework'] is None, f'{serial} is already reworked')
            _require(not self._qualified(entry),
                     f'{serial} is already qualified; rework is unnecessary')
        for serial in serials:
            entry = state['serials'][serial]
            entry['rework_authorized'] = {
                'event_id': event['id'],
                'at': event.get('at'),
                'supplier_signed': True,
            }
            entry['event_ids'].append(event['id'])
        return {'authorized': list(serials),
                'supplier_signed': True,
                'supplier_credit_cents': supplier_credit_cents(self.order)}

    @staticmethod
    def _rework_hours(event):
        entries = event.get('entries')
        if entries is not None:
            _require(isinstance(entries, list) and entries,
                     'rework entries must be a non-empty list')
            mapping = {}
            for item in entries:
                serial = item.get('serial')
                _require(bool(serial), 'every rework entry needs a serial')
                _require(serial not in mapping,
                         f'duplicate rework entry for {serial}')
                mapping[serial] = item.get('hours')
            declared = event.get('serials')
            if declared is not None:
                _require(set(declared) == set(mapping),
                         'rework serials do not match rework entries')
            return mapping
        serials = event.get('serials') or []
        _require(bool(serials), 'rework needs serials or entries')
        return {serial: event.get('hours') for serial in serials}

    def _apply_rework(self, event, state):
        hours_by_serial = self._rework_hours(event)
        _require(len(set(hours_by_serial)) == len(hours_by_serial),
                 'duplicate serial in rework')
        for serial, hours in hours_by_serial.items():
            entry = self._serial(state, serial)
            _require(entry['rework_authorized'] is not None,
                     f'{serial} has no signed supplier rework authorization')
            _require(entry['location'] == 'station',
                     f'{serial} has been returned; it cannot be reworked')
            _require(entry['rework'] is None, f'{serial} is already reworked')
            _require(not entry['void'], f'{serial} is voided by calibration')
            _require(not self._qualified(entry),
                     f'{serial} is qualified; rework is unnecessary')
            _require(isinstance(hours, (int, float)) and not isinstance(hours, bool)
                     and hours > 0,
                     f'{serial} needs real, positive rework hours')
        for serial, hours in hours_by_serial.items():
            entry = state['serials'][serial]
            entry['rework'] = {
                'event_id': event['id'],
                'at': event.get('at'),
                'hours': float(hours),
                'operator': event.get('operator'),
            }
            entry['event_ids'].append(event['id'])
        return {'reworked': dict(hours_by_serial)}

    def _apply_retest(self, event, state):
        serial = event.get('serial')
        entry = self._serial(state, serial)
        _require(entry['location'] == 'station',
                 f'{serial} is not at the station and cannot be retested')
        _require(not entry['void'], f'{serial} is voided by calibration')
        _require(entry['rework'] is not None,
                 f'{serial} has not been reworked; a retest is only valid after '
                 'real rework work, not as a way to re-register a failed serial')
        _require(event.get('independent') is True,
                 'a rework retest must be independent')
        result = self._inspect(state, serial, event.get('torque_nm'),
                               event, 'rework_retest', independent=True)
        return {'serial': serial,
                'qualified': result['qualified'],
                'worst_case_low_nm': result['worst_case_low_nm'],
                'worst_case_high_nm': result['worst_case_high_nm']}

    def _apply_replacement(self, event, state):
        _require(replacement_authorized(self.order),
                 'the contract does not authorise replacement')
        replaces = event.get('replaces') or []
        valves = event.get('valves') or []
        _require(bool(replaces), 'replacement_arrival needs the returned serials')
        _require(len(replaces) == len(valves),
                 'each returned valve needs exactly one replacement valve')
        _require(len(set(replaces)) == len(replaces), 'duplicate replaced serial')
        new_serials = [v.get('serial') for v in valves]
        _require(len(set(new_serials)) == len(new_serials),
                 'duplicate replacement serial in one arrival')
        for old in replaces:
            entry = self._serial(state, old)
            _require(entry['location'] == 'supplier',
                     f'{old} must be physically returned before a replacement arrives')
            _require(entry['replaced_by'] is None, f'{old} already has a replacement')
            _require(entry['rework'] is None,
                     f'{old} has been reworked; the same physical valve cannot '
                     'also be replaced')
            _require(entry['rework_authorized'] is None,
                     f'{old} is on the rework path; return and rework are exclusive')
            _require(not entry['void'], f'{old} is voided by calibration')
        for serial in new_serials:
            _require(serial and serial not in state['serials'],
                     f'replacement serial {serial!r} must be new and unused')
        for old, valve in zip(replaces, valves):
            serial = valve['serial']
            self._add_serial(state, serial, 'replacement', event)
            state['serials'][serial]['replacement_for'] = [old]
            state['serials'][old]['replaced_by'] = serial
            state['serials'][old]['event_ids'].append(event['id'])
            self._inspect(state, serial, valve.get('torque_nm'),
                          event, 'replacement_receipt')
        return {'replaced': list(replaces), 'arrived': new_serials,
                'qualified': [s for s in new_serials
                              if self._qualified(state['serials'][s])],
                'quarantined': [s for s in new_serials
                                if not self._qualified(state['serials'][s])]}

    def _apply_payment(self, event, state):
        try:
            amount = to_cents(event.get('amount'))
        except ValueError as exc:
            raise ProcurementError(str(exc)) from None
        _require(amount > 0, 'payment amount must be positive')
        recognised = self._recognised(state)
        paid = sum(p['amount_cents'] for p in state['payments'])
        _require(paid + amount <= recognised,
                 f'payment of {amount} cents would prepay unqualified valves: '
                 f'already paid {paid}, currently recognised payable {recognised}')
        state['payments'].append({
            'event_id': event['id'],
            'kind': 'payment',
            'at': event.get('at'),
            'amount_cents': amount,
            'reference': event.get('reference') or event['id'],
        })
        return {'paid_cents': amount, 'cumulative_paid_cents': paid + amount,
                'recognised_payable_cents': recognised,
                'outstanding_cents': recognised - (paid + amount)}

    def _apply_calibration_void(self, event, state):
        serials = event.get('serials') or []
        if event.get('serial'):
            serials = [*serials, event['serial']]
        serials = list(dict.fromkeys(serials))
        _require(bool(serials), 'calibration_void needs serials')
        for serial in serials:
            entry = self._serial(state, serial)
            _require(entry['void'] is None, f'{serial} is already voided')
            _require(self._qualified(entry),
                     f'{serial} is not an accepted unit; only an accepted unit '
                     'can be recalled by a calibration finding')
        for serial in serials:
            entry = state['serials'][serial]
            entry['void'] = {
                'event_id': event['id'],
                'at': event.get('at'),
                'instrument': event.get('instrument'),
                'reason': event.get('reason'),
            }
            entry['event_ids'].append(event['id'])
        unit = unit_price_cents(self.order)
        return {'voided': list(serials),
                'recovery_receivable_cents': unit * len(serials),
                'cash_retained': True}

    def _apply_install(self, event, state):
        serial = event.get('serial')
        entry = self._serial(state, serial)
        _require(self._qualified(entry),
                 f'{serial} is not qualified and cannot be put into service')
        _require(entry['installed'] is None, f'{serial} is already installed')
        entry['installed'] = event['id']
        entry['event_ids'].append(event['id'])
        state['installs'].append({'serial': serial, 'event_id': event['id'],
                                  'at': event.get('at')})
        return {'installed': serial}

    # ------------------------------------------------------------- accounting
    def _qualified_entries(self, state):
        return [state['serials'][s] for s in state['serial_order']
                if self._qualified(state['serials'][s])]

    def _rework_credit_ready(self, state):
        authorised = [e for e in state['serials'].values()
                      if e['rework_authorized'] is not None]
        if not authorised:
            return False
        return all(
            self._qualified(entry)
            and entry['rework'] is not None
            and self._current(entry).get('source') == 'rework_retest'
            for entry in authorised
        )

    def _credits(self, state):
        if not self._rework_credit_ready(state):
            return []
        amount = supplier_credit_cents(self.order)
        if amount <= 0:
            return []
        authorised = [e for e in state['serials'].values()
                      if e['rework_authorized'] is not None]
        return [{
            'id': 'CREDIT-REWORK-1',
            'kind': 'supplier_credit',
            'amount_cents': amount,
            'reason': 'contract credit for supplier-authorised local rework',
            'source_events': [e['rework_authorized']['event_id'] for e in authorised]
                             + [e['rework']['event_id'] for e in authorised],
        }]

    def _recoveries(self, state):
        unit = unit_price_cents(self.order)
        recoveries = []
        for serial in state['serial_order']:
            entry = state['serials'][serial]
            if not entry['void']:
                continue
            recoveries.append({
                'id': f'RECOVERY-{serial}',
                'kind': 'calibration_recovery',
                'serial': serial,
                'amount_cents': unit,
                'event_id': entry['void']['event_id'],
                'instrument': entry['void'].get('instrument'),
                'reason': entry['void'].get('reason'),
                'installed': bool(entry['installed']),
                'status': OPEN_RECOVERY,
            })
        return recoveries

    def _recognised(self, state):
        value = len(self._qualified_entries(state)) * unit_price_cents(self.order)
        credit = sum(c['amount_cents'] for c in self._credits(state))
        return value - credit

    # --------------------------------------------------------------- snapshot
    def snapshot(self):
        return self._snapshot_from(self._state)

    def _snapshot_from(self, state):
        unit = unit_price_cents(self.order)
        units = ordered_units(self.order)
        invoice_cents = to_cents(self.invoice.get('amount', po_value_cents(self.order)))
        po_value = po_value_cents(self.order)

        rows = [self._serial_row(s, state) for s in state['serial_order']]
        by_serial = {row['serial']: row for row in rows}
        qualified = [row for row in rows if row['status'] in QUALIFIED_STATUSES]
        quarantined = [row for row in rows if row['status'] == 'quarantined']
        replaced = [row for row in rows if row['status'] == 'replaced']
        recalled = [row for row in rows if row['status'].startswith('recalled')]

        qualified_value = len(qualified) * unit
        credits = self._credits(state)
        credit_cents = sum(c['amount_cents'] for c in credits)
        recognised = qualified_value - credit_cents
        paid = sum(p['amount_cents'] for p in state['payments'])
        outstanding = recognised - paid
        unqualified_value = invoice_cents - qualified_value
        recoveries = self._recoveries(state)
        recovery_cents = sum(r['amount_cents'] for r in recoveries)
        overpayment = max(0, paid - recognised)

        received_count = sum(len(r['inspections']) for r in state['receipts'])
        invoice_matches_po = invoice_cents == po_value
        received_matches_po = received_count == units
        invoice_covers_received = invoice_cents == received_count * unit
        three_way_ok = (invoice_matches_po and received_matches_po
                        and invoice_covers_received)
        fully_qualified = len(qualified) == units
        identity_ok = (paid + credit_cents + outstanding
                       + unqualified_value == invoice_cents)

        reconciliation = {
            'po_id': self.order.get('id'),
            'supplier': self.order.get('supplier'),
            'invoice_id': self.invoice.get('id'),
            'po_units': units,
            'unit_price_cents': unit,
            'po_value_cents': po_value,
            'invoice_cents': invoice_cents,
            'invoice_matches_po': invoice_matches_po,
            'received_count': received_count,
            'received_matches_po': received_matches_po,
            'invoice_covers_received_value': invoice_covers_received,
            'three_way_ok': three_way_ok,
            'qualified_count': len(qualified),
            'qualified_value_cents': qualified_value,
            'unqualified_value_cents': unqualified_value,
            'fully_qualified': fully_qualified,
            'paid_cents': paid,
            'credit_cents': credit_cents,
            'outstanding_cents': outstanding,
            'recovery_receivable_cents': recovery_cents,
            'identity_ok': identity_ok,
        }

        return {
            'order_id': self.order.get('id'),
            'supplier': self.order.get('supplier'),
            'invoice_id': self.invoice.get('id'),
            'invoice_cents': invoice_cents,
            'po_units': units,
            'unit_price_cents': unit,
            'po_value_cents': po_value,
            'qualified_serials': [row['serial'] for row in qualified],
            'quarantined_serials': [row['serial'] for row in quarantined],
            # Physically sent back, whether or not a replacement has arrived.
            'returned_serials': [row['serial'] for row in rows
                                 if row['returned_event_id'] is not None],
            'replaced_serials': [row['serial'] for row in replaced],
            'recalled_serials': [row['serial'] for row in recalled],
            'qualified_count': len(qualified),
            'qualified_value_cents': qualified_value,
            'rework_credit_cents': credit_cents,
            'recognized_payable_cents': recognised,
            'paid_cents': paid,
            'outstanding_cents': outstanding,
            'unqualified_value_cents': unqualified_value,
            'recovery_receivable_cents': recovery_cents,
            'overpayment_cents': overpayment,
            'serials': by_serial,
            'serial_rows': rows,
            'payments': copy.deepcopy(state['payments']),
            'credits': credits,
            'recoveries': recoveries,
            'installs': copy.deepcopy(state['installs']),
            'reconciliation': reconciliation,
            'settlement_complete': bool(fully_qualified and outstanding == 0
                                        and not recoveries),
            'revisions': self._revision,
            'history': copy.deepcopy(self._history),
            'events': copy.deepcopy(self._events),
        }

    def _serial_row(self, serial, state):
        entry = state['serials'][serial]
        current = self._current(entry)
        qualified = self._qualified(entry)
        if entry['void']:
            status = 'recalled_in_service' if entry['installed'] else 'recalled'
        elif entry['location'] == 'supplier':
            status = 'replaced' if entry['replaced_by'] else 'returned_to_supplier'
        elif qualified:
            if entry['installed']:
                status = 'installed'
            elif entry['rework']:
                status = 'reworked'
            else:
                status = 'qualified'
        elif current is not None:
            status = 'quarantined'
        else:
            status = 'awaiting_inspection'
        return {
            'serial': serial,
            'origin': entry['origin'],
            'status': status,
            'location': entry['location'],
            'qualified': qualified,
            'value_cents': unit_price_cents(self.order) if qualified else 0,
            'current_torque_nm': current['torque_nm'] if current else None,
            'current_source': current['source'] if current else None,
            'worst_case_low_nm': current['worst_case_low_nm'] if current else None,
            'worst_case_high_nm': current['worst_case_high_nm'] if current else None,
            'service_blocked': bool(entry['void']),
            'voided': entry['void'] is not None,
            'void_event_id': entry['void']['event_id'] if entry['void'] else None,
            'installed_event_id': entry['installed'],
            'rework_authorized': entry['rework_authorized'] is not None,
            'rework_authorized_event_id': (entry['rework_authorized'] or {}).get('event_id'),
            'rework_hours': (entry['rework'] or {}).get('hours'),
            'reworked': entry['rework'] is not None,
            'returned_event_id': entry['returned_event'],
            'replaced_by': entry['replaced_by'],
            'replacement_for': list(entry['replacement_for']),
            'inspections': copy.deepcopy(entry['inspections']),
            'event_ids': list(entry['event_ids']),
        }

    # ------------------------------------------------------------------ audit
    def verify(self):
        """Replay the append-only log and confirm the snapshot is identical."""
        reborn = type(self)(self.order, self.invoice)
        for event in self._events:
            reborn.record(copy.deepcopy(event))
        return reborn.snapshot() == self.snapshot()


def ledger_from_events(order, invoice, events, *, initial_valves=None):
    """Convenience wrapper: replay events and return the snapshot."""
    capture = ProcurementCapture(order, invoice)
    if initial_valves is not None:
        capture.record({'id': 'receipt-initial', 'kind': 'receipt',
                        'at': None, 'valves': initial_valves})
    for event in events:
        capture.record(copy.deepcopy(event))
    return capture.snapshot()
