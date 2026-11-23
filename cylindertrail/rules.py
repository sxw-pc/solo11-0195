"""Bottle inspection, filling, and delivery decisions share evidence, not state shortcuts."""
from collections import defaultdict
from .decisions import make_decision


def latest(events, kind):
    groups = defaultdict(list)
    for event in events:
        if event['kind'] == kind:
            groups[event['bottle_id']].append(event)
    return {key: sorted(items, key=lambda x: (x['version'], x['effective_at'], x['id']))[-1]
            for key, items in groups.items()}


def decision(result, reason, evidence):
    return make_decision(result, reason, evidence)


def calculate(events):
    bottles = latest(events, 'bottle')
    fills = latest(events, 'fill')
    dispatches = latest(events, 'dispatch')
    # A recall applies to the shared gas lot, even when the notice arrived in
    # another bottle's fill evidence. Recall is monotonic until physical return.
    recall_events=defaultdict(list)
    for event in events:
        if event['kind']=='fill' and event['batch'].get('recalled'):
            recall_events[event['batch']['id']].append(event['id'])
    all_bottles = defaultdict(list)
    for e in events:
        if e['kind'] == 'bottle':
            all_bottles[e['bottle_id']].append(e)
    output, states = {}, []
    for bottle_id in sorted(set(bottles) | set(fills) | set(dispatches)):
        bottle, fill, shipment = bottles.get(bottle_id), fills.get(bottle_id), dispatches.get(bottle_id)
        evidence = [e['id'] for e in (bottle, fill, shipment) if e]
        if fill:evidence += recall_events[fill['batch']['id']]
        history = all_bottles[bottle_id]
        identity = bool(bottle and len({(e['volume'], e['medium']) for e in history}) == 1)
        returned = bool(shipment and shipment.get('return') and shipment['return'].get('station_signature'))
        contaminated = bool(returned and (shipment['return']['residual_pressure'] >
                             shipment['return']['max_residual_pressure'] or shipment['return'].get('contaminated')))
        certificate = bool(bottle and fill and bottle['certificate']['valid_until'] >= fill['filled_at']
                           and not bottle['certificate'].get('revoked')
                           and bottle['certificate']['inspector'] != fill['operator'] and not contaminated)
        medium = bool(bottle and fill and bottle['medium'] == fill['medium'])
        recalled = bool(fill and recall_events[fill['batch']['id']])
        purity = bool(fill and fill['batch']['purity'] >= fill['batch']['minimum_purity']
                      and fill['batch']['expires'] >= fill['filled_at'] and not recalled)
        # Reference temperature/pressure conversion is deliberately observable in the result.
        equivalent = round(fill['quantity'] * fill['pressure'] / fill['reference_pressure']
                           * fill['reference_temperature'] / fill['temperature'], 3) if fill else 0
        within_limit = bool(bottle and fill and equivalent <= bottle['volume'])
        seal = bool(fill and fill['seal']['bottle_id'] == bottle_id
                    and fill['seal']['batch_id'] == fill['batch']['id']
                    and fill['seal']['receipt_id'] == fill['receipt_id'])
        fill_ok = identity and certificate and medium and purity and within_limit and seal
        handed = bool(shipment and shipment['outbound'].get('station_signature')
                      and shipment['outbound'].get('customer_signature'))
        notification = bool(shipment and shipment.get('recall_notice', {}).get('customer_signature'))
        prefix = bottle_id + '/'
        output[prefix+'瓶号冲突是否冻结身份'] = decision('冻结' if not identity else '确认', '历次瓶体容积和介质必须一致', evidence)
        output[prefix+'定检证书是否覆盖充装时刻'] = decision('有效' if certificate else '无效', '独立检验及充装时刻', evidence)
        output[prefix+'瓶体许可介质是否匹配'] = decision('匹配' if medium else '错配', '瓶体许可与批次介质', evidence)
        output[prefix+'气体批次纯度是否允许充装'] = decision('合格' if purity else '隔离', '纯度、到期与召回', evidence)
        output[prefix+'温压换算是否超瓶体额定量'] = decision(f'{equivalent}:合格' if within_limit else f'{equivalent}:超限', '换算到参考温压的当量和瓶容', evidence)
        output[prefix+'充装封签是否与批次同源'] = decision('同源' if seal else '失配', '瓶号、批次和回执三方绑定', evidence)
        output[prefix+'批次召回是否追踪在外瓶'] = decision('在外召回' if recalled and handed and not returned else '已收回' if recalled and returned else '无召回', '封签与出入库去向', evidence)
        output[prefix+'出库双签是否转移保管'] = decision('用气单位' if handed and not returned else '充装站', '双签和实际退瓶；安全资格单独裁定', evidence)
        output[prefix+'退瓶残压是否要求隔离'] = decision('隔离' if contaminated else '可复检' if returned else '未退瓶', '残压及污染采样', evidence)
        output[prefix+'召回送达是否解除用气风险'] = decision('已解除' if recalled and returned and not contaminated else '仅通知' if recalled and notification else '风险未结' if recalled else '无召回', '通知不等于实物收回', evidence)
        if bottle:
            states.append(('瓶体检验资格', bottle_id, '可充装' if identity and not bottle['certificate'].get('revoked') and not contaminated else '冻结', bottle['version']))
        if fill:
            states.append(('充装批次裁定', bottle_id, '可封签' if fill_ok else '隔离', fill['version']))
        if shipment:
            states.append(('出入库交接召回', bottle_id, '客户保管' if handed and not returned else '本站保管', shipment['version']))
    return output, states


def business_records(e):
    ident = e['id']
    if e['kind'] == 'bottle':
        return [('瓶体检验资格', '瓶体档案', ident+':bottle', {'bottle_id': e['bottle_id'], 'volume': e['volume'], 'medium': e['medium']}),
                ('瓶体检验资格', '定检证书', ident+':certificate', e['certificate'])]
    if e['kind'] == 'fill':
        return [('充装批次裁定', '气体批次', ident+':batch', e['batch']),
                ('充装批次裁定', '充装回执', ident+':receipt', {'receipt_id': e['receipt_id'], 'quantity': e['quantity'], 'seal': e['seal']})]
    return [('出入库交接召回', '交接单', ident+':outbound', e['outbound']),
            ('出入库交接召回', '退瓶回执', ident+':return', e.get('return', {'status': '未退瓶'}))]
