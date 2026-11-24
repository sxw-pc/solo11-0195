"""Replayable append-only ledger of physical effluent movements.

Each event records something that physically happened (or a piece of
evidence that physically arrived).  Events are hash-chained and never
rewritten: a late calibration or a failing re-test that invalidates volume
already discharged is handled by freezing the remaining tanks and opening a
nonconformance investigation — the outfall flow that already occurred stays
in the ledger exactly as the meter recorded it.
"""
import hashlib
import json

from .discharge_gate import authorize_discharge, carrier_authorized
from .effluent_sample import verify_tank_sample


class LedgerError(RuntimeError):
    """Base class for ledger rule violations."""


class QuotaExceeded(LedgerError):
    """The valve would push the day's effective flow past the daily limit."""


class AcidStockError(LedgerError):
    """Not enough acid, or one stock reservation reused for two tanks."""


class NeutralizerCapacity(LedgerError):
    """A neutralization batch exceeds the unit's per-run capacity."""


class EndpointFailure(LedgerError):
    """The independent endpoint re-test after neutralization failed."""


class TankFrozen(LedgerError):
    """Operation refused because the tank is frozen pending investigation."""


class CustodyError(LedgerError):
    """Carrier permit, signatures or terminal receipt do not support custody transfer."""


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


class EffluentLedger:
    """Append-only physical movement ledger for wash-tank effluent."""

    def __init__(self, config):
        self.config = dict(config)
        self.outfall = dict(config['outfall'])
        self.unit = dict(config['neutralization_unit'])
        self.initial_acid_stock = float(self.unit['acid_stock_mmol'])
        self.acid_stock = float(self.unit['acid_stock_mmol'])
        self.carrier = dict(config['hazard_carrier'])
        self.calibrations = {}
        self.tanks = {}
        self.events = []
        self.open_valves = {}
        self.meter_reading = 0.0
        self.seq = 0
        self.last_hash = 'GENESIS'
        for calibration in config.get('calibrations', ()):
            self._apply(dict(calibration, kind='calibration-registered'))
        for tank in config['tanks']:
            self._apply({
                'kind': 'tank-registered',
                'tank_id': tank['id'],
                'source': tank['source'],
                'litres': float(tank['litres']),
            })

    # ------------------------------------------------------------------ chain
    def append(self, kind, **payload):
        payload = dict(kind=kind, **payload)
        self.seq += 1
        body = _canonical(payload)
        entry = {
            'seq': self.seq,
            'kind': kind,
            'payload': payload,
            'prev_hash': self.last_hash,
            'hash': hashlib.sha256(str(self.seq).encode() + b'|' + self.last_hash.encode()
                                   + b'|' + body).hexdigest(),
        }
        self.events.append(entry)
        self.last_hash = entry['hash']
        self._apply(payload)
        return entry

    def verify_chain(self):
        previous = 'GENESIS'
        for seq, entry in enumerate(self.events, start=1):
            body = _canonical(entry['payload'])
            expected = hashlib.sha256(str(seq).encode() + b'|' + previous.encode()
                                      + b'|' + body).hexdigest()
            if entry['seq'] != seq or entry['hash'] != expected or entry['prev_hash'] != previous:
                return False
            previous = entry['hash']
        return previous == self.last_hash

    @classmethod
    def replay(cls, config, events):
        """Rebuild the complete ledger state from raw persisted events."""
        ledger = cls(config)
        for entry in events:
            if entry['prev_hash'] != ledger.last_hash:
                raise LedgerError('event chain broken during replay')
            body = _canonical(entry['payload'])
            expected = hashlib.sha256(str(entry['seq']).encode()
                                      + b'|' + entry['prev_hash'].encode()
                                      + b'|' + body).hexdigest()
            if entry['hash'] != expected:
                raise LedgerError('event hash mismatch during replay')
            ledger.seq = entry['seq']
            ledger.last_hash = entry['hash']
            ledger.events.append(entry)
            ledger._apply(entry['payload'])
        return ledger

    # ------------------------------------------------------------- state apply
    def _apply(self, event):
        kind = event['kind']
        if kind == 'calibration-registered':
            self.calibrations[event['tank_id']] = dict(event, revoked=False)
        elif kind == 'calibration-revoked':
            for calibration in self.calibrations.values():
                if calibration['calibration_id'] == event['calibration_id']:
                    calibration['revoked'] = True
                    target_tank = self.tanks.get(calibration['tank_id'])
                    if target_tank is not None and target_tank['sample'] is not None:
                        target_tank['sample']['calibration_ok'] = False
                    break
        elif kind == 'nonconformance-opened':
            # Late evidence invalidates the old readings; the physical facts
            # (discharged volume, acid dosed) are never rewritten.
            target = self.tanks[event['tank_id']]
            revoked_cal = event.get('late_calibration_id')
            if revoked_cal:
                for calibration in self.calibrations.values():
                    if calibration['calibration_id'] == revoked_cal:
                        calibration['revoked'] = True
            if target['sample'] is not None:
                target['sample']['calibration_ok'] = False
            if target['endpoint_sample'] is not None:
                target['endpoint_sample']['calibration_ok'] = False
        elif kind == 'tank-registered':
            self.tanks[event['tank_id']] = {
                'id': event['tank_id'],
                'source': event['source'],
                'litres': float(event['litres']),
                'sample': None,
                'needs_neutralization': False,
                'neutralized': False,
                'neutralization_event': None,
                'endpoint_sample': None,
                'frozen': False,
                'freeze_reasons': [],
                'location': 'on-site-tank',
                'discharged_litres': 0.0,
                'manifests': [],
            }
        elif kind == 'sample-recorded':
            tank = self.tanks[event['tank_id']]
            sample = {k: event[k] for k in ('sample_id', 'ph', 'ph_error',
                                            'alkalinity_mmol_per_litre', 'calibration_id',
                                            'calibration_ok', 'sampled_at')}
            if tank['sample'] is None:
                tank['sample'] = sample
            tank.setdefault('sample_history', []).append(sample)
            tank['needs_neutralization'] = bool(event.get('needs_neutralization'))
        elif kind == 'acid-adjusted':
            self.acid_stock = float(event['stock_after_mmol'])
        elif kind == 'neutralization-completed':
            tank = self.tanks[event['tank_id']]
            tank['litres'] = float(event['litres'])
            tank['neutralized'] = bool(event['endpoint_passed'])
            tank['neutralization_event'] = event['event_id']
            tank['endpoint_sample'] = dict(event['endpoint_sample'])
            if not event['endpoint_passed']:
                self._freeze_state(tank, '中和终点复测不合格，禁止排放并启动超标调查')
        elif kind == 'tank-sealed-on-site':
            tank = self.tanks[event['tank_id']]
            tank['location'] = 'on-site-sealed'
        elif kind == 'tank-frozen':
            self._freeze_state(self.tanks[event['tank_id']], event['reason'])
        elif kind == 'valve-opened':
            self.open_valves[event['valve_id']] = event
        elif kind == 'valve-closed':
            self.open_valves.pop(event['valve_id'], None)
            self.meter_reading = float(event['meter_end_reading_litres'])
            tank = self.tanks[event['tank_id']]
            tank['litres'] = float(event['tank_remaining_litres'])
            tank['discharged_litres'] += float(event['effective_litres'])
            if tank['litres'] <= 1e-9:
                tank['location'] = 'discharged'
        elif kind == 'carrier-manifest-created':
            tank = self.tanks[event['tank_id']]
            tank['manifests'].append(event['manifest_id'])
        elif kind == 'custody-handed-over':
            self.tanks[event['tank_id']]['location'] = 'carrier'
        elif kind == 'terminal-receipt-recorded':
            self.tanks[event['tank_id']]['location'] = 'terminal'
        elif kind == 'carrier-permit-revoked':
            self.carrier['permit_revoked_at'] = event['at']
        elif kind in ('nonconformance-opened', 'notification-sent'):
            return
        else:
            raise LedgerError(f'unknown event kind {kind}')

    def _freeze_state(self, tank, reason):
        tank['frozen'] = True
        tank['freeze_reasons'].append(reason)

    def _payloads(self):
        return [entry['payload'] for entry in self.events]

    def _require_active(self, tank_id):
        tank = self.tanks[tank_id]
        if tank['frozen']:
            raise TankFrozen(f"{tank_id} 已冻结：{tank['freeze_reasons'][-1]}")
        return tank

    # ------------------------------------------------------------- sampling
    def register_calibration(self, calibration):
        event = dict(calibration)
        event['kind'] = 'calibration-registered'
        return self.append(**event)

    def revoke_calibration(self, calibration_id, at, reason):
        """Record a late-arriving calibration withdrawal as a new event."""
        known = any(c['calibration_id'] == calibration_id for c in self.calibrations.values())
        if not known:
            raise LedgerError(f'未知校准记录 {calibration_id}')
        return self.append('calibration-revoked', event_id=f'EV-CALREV-{calibration_id}',
                           calibration_id=calibration_id, at=at, reason=reason)

    def record_sample(self, tank_id, sample_id, ph, ph_error,
                      alkalinity_mmol_per_litre, calibration_id, sampled_at):
        """Attach a sample to its own tank; calibration is checked separately."""
        tank = self._require_active(tank_id)
        calibration = next((c for c in self.calibrations.values()
                            if c['calibration_id'] == calibration_id), None)
        if calibration is None or calibration.get('tank_id') != tank_id:
            raise LedgerError('校准记录与采样槽不匹配')
        sample_input = {
            'id': tank_id, 'litres': tank['litres'], 'ph': ph, 'ph_error': ph_error,
            'alkalinity_mmol_per_litre': alkalinity_mmol_per_litre,
            'sampled_at': sampled_at,
        }
        checked = verify_tank_sample(sample_input, calibration,
                                     self.outfall['minimum_ph'], self.outfall['maximum_ph'])
        return self.append(
            'sample-recorded',
            event_id=f'EV-SAMPLE-{tank_id}-{self.seq + 1:04d}',
            tank_id=tank_id,
            sample_id=sample_id,
            ph=float(ph),
            ph_error=float(ph_error),
            alkalinity_mmol_per_litre=float(alkalinity_mmol_per_litre),
            calibration_id=calibration_id,
            calibration_ok=checked['calibration_ok'],
            sampled_at=sampled_at,
            needs_neutralization=checked['verdict'] in ('须中和或外运', '须回调或外运'),
        )

    # ------------------------------------------------------- neutralization
    def neutralize(self, tank_id, event_id, at, dosed_acid_mmol,
                   endpoint_ph, endpoint_ph_error, endpoint_sample_id, operator):
        """Run one neutralization batch with acid deduction and re-test."""
        tank = self._require_active(tank_id)
        if tank['sample'] is None:
            raise LedgerError('中和前必须先有本槽独立采样与碱度滴定')
        litres = float(tank['litres'])
        if litres > float(self.unit['capacity_litres']):
            raise NeutralizerCapacity(
                f"中和机单次容量 {float(self.unit['capacity_litres']):g}L，"
                f'本批 {litres:g}L 超限')
        if not self.unit.get('available', True):
            raise NeutralizerCapacity('中和机不可用')
        demand = float(tank['sample']['alkalinity_mmol_per_litre']) * litres
        dosed = float(dosed_acid_mmol)
        if dosed < demand:
            raise AcidStockError(
                f'投酸 {dosed:g}mmol 低于按碱度计算的需求 {demand:g}mmol，无法到达终点')
        if dosed > self.acid_stock:
            raise AcidStockError(
                f'酸库存仅剩 {self.acid_stock:g}mmol，不能对 {tank_id} 再投 {dosed:g}mmol；'
                '同一库存不得重复占用给两个槽')
        stock_after = self.acid_stock - dosed
        self.append('acid-adjusted', event_id=f'EV-ACID-{self.seq + 1:04d}',
                    tank_id=tank_id, dosed_mmol=dosed, alkalinity_demand_mmol=demand,
                    stock_before_mmol=self.acid_stock, stock_after_mmol=stock_after, at=at)
        endpoint_calibration = next(
            (c for c in self.calibrations.values()
             if c['calibration_id'] == tank['sample']['calibration_id']), None)
        endpoint_sample = {
            'sample_id': endpoint_sample_id,
            'ph': float(endpoint_ph),
            'ph_error': float(endpoint_ph_error),
            'alkalinity_mmol_per_litre': 0.0,
            'calibration_id': tank['sample']['calibration_id'],
            'calibration_ok': bool(endpoint_calibration and not endpoint_calibration.get('revoked')),
            'sampled_at': at,
        }
        passed = (self.outfall['minimum_ph'] <= endpoint_ph <= self.outfall['maximum_ph']
                  and endpoint_ph + endpoint_ph_error <= self.outfall['maximum_ph']
                  and endpoint_ph - endpoint_ph_error >= self.outfall['minimum_ph']
                  and endpoint_sample['calibration_ok'])
        self.append(
            'neutralization-completed',
            event_id=event_id,
            tank_id=tank_id,
            litres=litres,
            dosed_acid_mmol=dosed,
            alkalinity_demand_mmol=demand,
            endpoint_sample=endpoint_sample,
            endpoint_passed=passed,
            operator=operator,
            at=at,
        )
        if not passed:
            raise EndpointFailure(f'{tank_id} 中和终点复测 pH {endpoint_ph:g} 不合格，批次冻结')
        return self.tanks[tank_id]

    def seal_on_site(self, tank_id, event_id, at, sealed_by):
        """Seal a neutralized, re-tested batch for later-day discharge."""
        tank = self._require_active(tank_id)
        if not tank['neutralized'] or not tank['endpoint_sample']:
            raise LedgerError('只有中和完成且终点复测合格的批次才能站内封存')
        return self.append('tank-sealed-on-site', event_id=event_id, tank_id=tank_id,
                           litres=tank['litres'], endpoint_sample_id=tank['endpoint_sample']['sample_id'],
                           sealed_by=sealed_by, at=at)

    # ------------------------------------------------------------ outfall
    def open_valve(self, tank_id, valve_id, at, opened_by, litres=None):
        tank = self._require_active(tank_id)
        day = at[:10]
        requested = float(tank['litres'] if litres is None else litres)
        gate = authorize_discharge(
            tank, requested, self._payloads(), day, self.outfall['daily_limit_litres'],
            self.outfall['minimum_ph'], self.outfall['maximum_ph'])
        if not gate['allowed']:
            raise QuotaExceeded(gate['reason'])
        if valve_id in self.open_valves:
            raise LedgerError(f'阀门 {valve_id} 已处于开启状态')
        return self.append(
            'valve-opened',
            event_id=f'EV-OPEN-{valve_id}-{self.seq + 1:04d}',
            valve_id=valve_id,
            tank_id=tank_id,
            day=day,
            requested_litres=requested,
            meter_start_reading_litres=self.meter_reading,
            opened_by=opened_by,
            at=at,
        )

    def close_valve(self, valve_id, at, closed_by, meter_end_reading_litres, signed_sample_id):
        opened = self.open_valves.get(valve_id)
        if opened is None:
            raise LedgerError(f'阀门 {valve_id} 未开启，不能关闭')
        delta = float(meter_end_reading_litres) - float(opened['meter_start_reading_litres'])
        if delta <= 0:
            raise LedgerError('在线流量计读数必须为正增量')
        day = opened['day']
        limit = float(self.outfall['daily_limit_litres'])
        already = sum(
            p['effective_litres'] for p in self._payloads()
            if p.get('kind') == 'valve-closed' and p.get('day') == day)
        if already + delta > limit + 1e-9:
            raise QuotaExceeded(
                f'计量 {delta:g}L 将使当日累计 {already + delta:g}L 超过日限额 {limit:g}L')
        tank = self.tanks[opened['tank_id']]
        if delta > float(tank['litres']) + 1e-9:
            raise LedgerError('流量计增量超过槽内余水')
        self.meter_reading = float(meter_end_reading_litres)
        return self.append(
            'valve-closed',
            event_id=f'EV-CLOSE-{valve_id}-{self.seq + 1:04d}',
            valve_id=valve_id,
            tank_id=opened['tank_id'],
            day=day,
            effective_litres=delta,
            meter_start_reading_litres=opened['meter_start_reading_litres'],
            meter_end_reading_litres=self.meter_reading,
            tank_remaining_litres=float(tank['litres']) - delta,
            signed_sample_id=signed_sample_id,
            opened_at=opened['at'],
            closed_by=closed_by,
            at=at,
        )

    # ------------------------------------------------------- hazardous carrier
    def create_manifest(self, tank_id, manifest_id, at):
        tank = self._require_active(tank_id)
        gate = carrier_authorized(self.carrier, tank['litres'], tank['source'], at[:10])
        if not gate['allowed']:
            raise CustodyError(gate['reason'])
        return self.append('carrier-manifest-created', event_id=f'EV-MANIFEST-{manifest_id}',
                           manifest_id=manifest_id, tank_id=tank_id,
                           litres=tank['litres'], media=tank['source'],
                           carrier_id=self.carrier['id'], at=at)

    def handover(self, manifest_id, at, station_signature, carrier_signature):
        if not station_signature or not carrier_signature:
            raise CustodyError('外运交接必须现场与承运人双签')
        event = self._find_manifest(manifest_id)
        tank = self.tanks[event['tank_id']]
        if tank['frozen']:
            raise TankFrozen('冻结槽在调查结论前不得外运')
        gate = carrier_authorized(self.carrier, event['litres'], event['media'], at[:10])
        if not gate['allowed']:
            raise CustodyError(gate['reason'])
        return self.append('custody-handed-over', event_id=f'EV-HANDOVER-{manifest_id}',
                           manifest_id=manifest_id, tank_id=event['tank_id'],
                           litres=event['litres'], media=event['media'],
                           station_signature=station_signature,
                           carrier_signature=carrier_signature, at=at)

    def receive_at_terminal(self, manifest_id, at, terminal_receipt_id, receiver_signature):
        """Custody only closes when the terminal actually accepts the load."""
        event = self._find_manifest(manifest_id)
        if self.tanks[event['tank_id']]['frozen']:
            raise TankFrozen('冻结批次在超标调查结论前不得办理终端接收闭环')
        if not terminal_receipt_id or not receiver_signature:
            raise CustodyError('必须有终端接收回执编号与接收人签署')
        return self.append('terminal-receipt-recorded', event_id=f'EV-RECEIPT-{manifest_id}',
                           manifest_id=manifest_id, tank_id=event['tank_id'],
                           terminal_receipt_id=terminal_receipt_id,
                           receiver_signature=receiver_signature, at=at)

    def _find_manifest(self, manifest_id):
        for entry in self.events:
            if entry['kind'] == 'carrier-manifest-created' and entry['payload']['manifest_id'] == manifest_id:
                return entry['payload']
        raise CustodyError(f'联单 {manifest_id} 不存在')

    def revoke_carrier_permit(self, at, reason):
        if self.carrier.get('permit_revoked_at'):
            raise CustodyError('许可撤销不可重复登记')
        return self.append('carrier-permit-revoked', at=at, reason=reason,
                           carrier_id=self.carrier['id'])

    # --------------------------------------------- late invalidating evidence
    def invalidate_sample(self, tank_id, event_id, at, reason, late_calibration_id=None,
                          late_sample=None, notify_authority=None):
        """A late calibration/retest invalidates evidence.

        Physical discharge facts are preserved.  Remaining on-site water is
        frozen and a nonconformance investigation plus notifications are
        raised; discharged litres are never moved back into stock.
        """
        tank = self.tanks[tank_id]
        payload = {'event_id': event_id, 'tank_id': tank_id, 'at': at, 'reason': reason}
        if late_calibration_id:
            payload['late_calibration_id'] = late_calibration_id
        if late_sample is not None:
            payload['late_sample'] = dict(late_sample)
        self.append('nonconformance-opened', **payload)
        discharged = tank['discharged_litres']
        self.append(
            'notification-sent',
            event_id=f'EV-NOTICE-{self.seq + 1:04d}',
            tank_id=tank_id,
            at=at,
            reason=reason,
            discharged_litres_preserved=discharged,
            authority=notify_authority or '生态环境主管部门',
        )
        for other_id, other in self.tanks.items():
            if other['frozen']:
                continue
            if other['location'] in ('on-site-tank', 'on-site-sealed'):
                self.append('tank-frozen', event_id=f'EV-FREEZE-{other_id}-{self.seq + 1:04d}',
                            tank_id=other_id, at=at,
                            reason=f'迟到证据否决 {tank_id}，全站剩余槽冻结待查')
            elif other_id == tank_id:
                # Water already out: the discharge fact stands, but any further
                # operation on this tank is frozen while the case is open.
                self.append('tank-frozen', event_id=f'EV-FREEZE-{other_id}-{self.seq + 1:04d}',
                            tank_id=other_id, at=at,
                            reason='迟到证据否决已排批次，槽位冻结配合超标调查')
        return self.nonconformance(tank_id)

    def nonconformance(self, tank_id):
        notices = [e['payload'] for e in self.events
                   if e['kind'] == 'notification-sent' and e['payload']['tank_id'] == tank_id]
        cases = [e['payload'] for e in self.events
                 if e['kind'] == 'nonconformance-opened' and e['payload']['tank_id'] == tank_id]
        return {'tank_id': tank_id, 'cases': cases, 'notifications': notices,
                'discharged_litres_preserved': self.tanks[tank_id]['discharged_litres'],
                'frozen': self.tanks[tank_id]['frozen']}

    # -------------------------------------------------------------- queries
    def tank(self, tank_id):
        return dict(self.tanks[tank_id])

    def trace(self, tank_id):
        """Minute-level replay of every physical event touching one tank."""
        rows = []
        for entry in self.events:
            if entry['payload'].get('tank_id') == tank_id:
                row = dict(seq=entry['seq'], **entry['payload'])
                rows.append(row)
        return rows

    def discharged_total(self):
        return sum(p['effective_litres'] for p in self._payloads()
                   if p.get('kind') == 'valve-closed')

    def discharged_on(self, day):
        return sum(p['effective_litres'] for p in self._payloads()
                   if p.get('kind') == 'valve-closed' and p.get('day') == day)

    def summary(self):
        return {
            'tanks': {tid: dict(state) for tid, state in sorted(self.tanks.items())},
            'acid_stock_mmol': self.acid_stock,
            'acid_initial_mmol': self.initial_acid_stock,
            'acid_consumed_mmol': self.initial_acid_stock - self.acid_stock,
            'meter_reading_litres': self.meter_reading,
            'discharged_total_litres': self.discharged_total(),
            'carrier': dict(self.carrier),
            'event_count': len(self.events),
            'chain_intact': self.verify_chain(),
        }
