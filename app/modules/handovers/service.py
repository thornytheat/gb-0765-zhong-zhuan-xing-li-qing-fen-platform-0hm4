from datetime import datetime, timezone

from app.errors import DeskError
from app.manifest import canonical, fingerprint
from .policy import offload_confirmed, pending_tally, wrong_bags

FAILURE_STATUS = {'NO_ORDER': 404, 'ORDER_CLOSED': 409, 'OWNER_MISMATCH': 409}
FAILURE_MESSAGE = {
    'NO_ORDER': '处置单不存在',
    'ORDER_CLOSED': '处置单已关单，不能再交接',
    'OWNER_MISMATCH': '提交班组不是当前责任班组，交接被拒',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def order_key(run_id, tag, leg):
    return fingerprint([run_id, tag, leg])


def _crew(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 32:
        raise DeskError('BAD_INPUT', f'{field}为必填字符串且不超过 32 字', 422)
    return value.strip()


def _handoff_payload(body):
    if not isinstance(body, dict):
        raise DeskError('BAD_INPUT', '请提交交接请求体', 422)
    key = body.get('idempotency_key')
    if not isinstance(key, str) or not key.strip() or len(key) > 64:
        raise DeskError('BAD_INPUT', 'idempotency_key 为必填字符串且不超过 64 字', 422)
    expected, next_crew = _crew(body.get('from_crew'), 'from_crew'), _crew(body.get('to_crew'), 'to_crew')
    if expected == next_crew:
        raise DeskError('BAD_INPUT', '接班班组不能与当前责任班组相同', 422)
    return key.strip(), dict(idempotency_key=key.strip(), from_crew=expected, to_crew=next_crew)


def seed_run(handovers, archive, run_id):
    """按归档中的错装记录幂等生成处置单，初始责任班组为原班。"""
    _, result = archive.get(run_id)
    orders = [dict(id=order_key(run_id, bag['tag'], bag['leg']), run_id=run_id,
                   tag=bag['tag'], leg=bag['leg'], planned=bag['planned'],
                   owner='原班', status='open', created_at=now(), closed_at=None, confirmed_run=None)
              for bag in wrong_bags(result['stages'])]
    rows = handovers.seed_orders(orders)
    return dict(orders=rows, tally=pending_tally(rows))


def board(handovers, run_id):
    """归因页视图：每张单的当前责任班组、状态与交接经过。"""
    orders = handovers.list_orders(run_id)
    for order in orders:
        order['events'] = handovers.events(order['id'])
    return dict(orders=orders, tally=pending_tally(orders))


def handoff(handovers, order_id, body):
    """交接处置单；同 idempotency_key 重放首次结局，绝不重复交接。"""
    key, payload = _handoff_payload(body)
    result, error = handovers.request_handoff(
        order_id, key, payload, payload['from_crew'], payload['to_crew'], now())
    if error == 'NO_ORDER':
        raise DeskError('NO_ORDER', FAILURE_MESSAGE['NO_ORDER'], 404)
    if result['replayed'] and canonical(result['payload']) != canonical(payload):
        raise DeskError('REQUEST_CONFLICT', '同幂等键的请求内容与首次提交不一致', 409)
    outcome = result['outcome']
    if not outcome['ok']:
        raise DeskError(outcome['code'], FAILURE_MESSAGE[outcome['code']],
                        FAILURE_STATUS[outcome['code']])
    if result['replayed']:
        outcome = dict(outcome, replayed=True)
    return outcome


def close(handovers, archive, order_id, confirm_run=None):
    """撤下扫件经回放确认后关单；重复关单幂等返回。"""
    order = handovers.get_order(order_id)
    if order is None:
        raise DeskError('NO_ORDER', FAILURE_MESSAGE['NO_ORDER'], 404)
    if order['status'] == 'closed':
        return dict(ok=True, replayed=True, order=order, events=handovers.events(order_id))
    run_id = confirm_run or order['run_id']
    try:
        _, result = archive.get(run_id)
    except DeskError:
        raise DeskError('NO_RUN', '回放确认所用归档不存在', 404)
    if not offload_confirmed(result['stages'], order['tag'], order['leg']):
        raise DeskError('REPLAY_NOT_CONFIRMED', '回放未确认该件在错装航段撤下，不能关单', 422)
    fresh, error = handovers.close_order(order_id, run_id, now())
    if error:
        raise DeskError(error, FAILURE_MESSAGE[error], FAILURE_STATUS[error])
    return dict(ok=True, replayed=False, order=fresh, events=handovers.events(order_id))


def attribution(handovers, order_id):
    order = handovers.get_order(order_id)
    if order is None:
        raise DeskError('NO_ORDER', FAILURE_MESSAGE['NO_ORDER'], 404)
    return dict(order=order, events=handovers.events(order_id),
                tally=pending_tally(handovers.list_orders(order['run_id'])))
