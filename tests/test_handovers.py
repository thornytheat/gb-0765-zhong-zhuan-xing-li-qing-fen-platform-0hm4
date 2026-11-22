from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import DeskError
from app.input import sample
from app.manifest import fingerprint
from app.modules.archive.service import archive
from app.modules.archive.store import Archive
from app.modules.handovers.policy import offload_confirmed, pending_tally
from app.modules.handovers.service import order_key
from app.modules.handovers.store import HandoverStore
from app.pipeline import compute


def confirmed_files():
    """在脏样例上为两个错装航段补撤下扫件，使回放末态变为 OFFLOADED。"""
    files = sample('dirty')
    extra = [
        ('E30', '000105', 'NX518', '', 'OFFLOAD', '2026-11-21T00:05:00+08:00', '2026-11-21T00:06:00+08:00'),
        ('E31', '000108', 'NX518', 'NX-02', 'OFFLOAD', '2026-11-21T00:50:00+08:00', '2026-11-21T00:51:00+08:00'),
    ]
    text = files['scans.csv'] + ''.join(','.join(row) + '\n' for row in extra)
    return dict(files, **{'scans.csv': text})


@pytest.fixture
def run(desk):
    saved = desk.post('/api/archive', json={'sample': 'dirty'}).json
    seeded = desk.post(f"/api/runs/{saved['id']}/dispositions").json
    return saved['id'], seeded


@pytest.fixture
def first_order_id(run):
    return run[1]['orders'][0]['id']


@pytest.fixture
def dirty_seeded(desk):
    saved = desk.post('/api/archive', json={'sample': 'dirty'}).json
    seeded = desk.post(f"/api/runs/{saved['id']}/dispositions").json
    return saved['id'], seeded


@pytest.fixture
def confirmed_run_id(desk):
    return desk.post('/api/archive', json={'files': confirmed_files()}).json['id']


def test_seed_orders_from_wrong_bags(run):
    run_id, seeded = run
    assert [(row['tag'], row['leg']) for row in seeded['orders']] == [
        ('000105', 'NX-01'), ('000108', 'NX-02')]
    assert {row['owner'] for row in seeded['orders']} == {'原班'}
    assert seeded['tally'] == dict(disposition_open=2, disposition_closed=0, disposition_total=2)
    assert seeded['orders'][0]['id'] == order_key(run_id, '000105', 'NX-01') == fingerprint(
        [run_id, '000105', 'NX-01'])


def test_seed_is_idempotent(desk, run):
    run_id, _ = run
    again = desk.post(f'/api/runs/{run_id}/dispositions').json
    assert len(again['orders']) == 2
    for order in again['orders']:
        events = desk.get(f"/api/dispositions/{order['id']}/attribution").json['events']
        assert [event['kind'] for event in events] == ['OPEN']


def test_handoff_changes_owner_and_records_events(desk, first_order_id):
    result = desk.post(f'/api/dispositions/{first_order_id}/handovers',
                       json=dict(idempotency_key='k1', from_crew='原班', to_crew='新班'))
    assert result.status_code == 200 and result.json['order']['owner'] == '新班'
    attribution = desk.get(f'/api/dispositions/{first_order_id}/attribution').json
    assert attribution['order']['owner'] == '新班'
    assert [event['kind'] for event in attribution['events']] == ['OPEN', 'HANDOVER']
    assert attribution['events'][-1]['from_crew'] == '原班'
    assert attribution['events'][-1]['to_crew'] == '新班'


def test_duplicate_request_replays_without_second_handoff(desk, first_order_id):
    body = dict(idempotency_key='k-dup', from_crew='原班', to_crew='新班')
    first = desk.post(f'/api/dispositions/{first_order_id}/handovers', json=body).json
    second = desk.post(f'/api/dispositions/{first_order_id}/handovers', json=body).json
    assert first['order']['owner'] == second['order']['owner'] == '新班'
    assert second['replayed'] is True
    events = desk.get(f'/api/dispositions/{first_order_id}/attribution').json['events']
    assert [event['kind'] for event in events].count('HANDOVER') == 1


def test_retry_succeeds_with_new_key_after_owner_changes(desk, first_order_id):
    desk.post(f'/api/dispositions/{first_order_id}/handovers',
              json=dict(idempotency_key='k1', from_crew='原班', to_crew='新班'))
    again = desk.post(f'/api/dispositions/{first_order_id}/handovers',
                      json=dict(idempotency_key='k2', from_crew='新班', to_crew='丙班'))
    assert again.status_code == 200 and again.json['order']['owner'] == '丙班'


def test_non_owner_submission_is_rejected(desk, first_order_id):
    result = desk.post(f'/api/dispositions/{first_order_id}/handovers',
                       json=dict(idempotency_key='k-bad', from_crew='隔壁班', to_crew='新班'))
    assert (result.status_code, result.json['code']) == (409, 'OWNER_MISMATCH')
    assert desk.get(f'/api/dispositions/{first_order_id}/attribution').json['order']['owner'] == '原班'


def test_original_and_new_crew_submit_concurrently_only_one_wins(tmp_path):
    archive_store, handover_store = Archive(tmp_path / 'a.sqlite3'), HandoverStore(tmp_path / 'a.sqlite3')
    saved = archive(archive_store, sample('dirty'), compute)
    from app.modules.handovers import service
    seeded = service.seed_run(handover_store, archive_store, saved['id'])
    order = seeded['orders'][0]

    def submit(attempt):
        key, crew = attempt
        try:
            return service.handoff(handover_store, order['id'],
                                   dict(idempotency_key=key, from_crew=crew, to_crew='新班')), None
        except DeskError as exc:
            return None, exc.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(submit, [('k1', '原班'), ('k2', '原班'), ('k3', '原班'), ('k4', '原班')]))
    winners = [item for item, code in outcomes if code is None]
    losers = [code for item, code in outcomes if code]
    assert len(winners) == 1 and winners[0]['order']['owner'] == '新班'
    assert losers == ['OWNER_MISMATCH'] * 3
    assert handover_store.get_order(order['id'])['owner'] == '新班'
    events = handover_store.events(order['id'])
    assert [event['kind'] for event in events].count('HANDOVER') == 1
    # 失败请求也已留痕，同键重放仍是同一个拒绝结局（抛同样的错误码）
    with pytest.raises(DeskError) as exc:
        service.handoff(handover_store, order['id'],
                        dict(idempotency_key='k2', from_crew='原班', to_crew='新班'))
    assert exc.value.code == 'OWNER_MISMATCH'


def test_two_crews_grab_same_order_ownership_chain_holds(tmp_path):
    """原班与新班同时抢同一张单：责任链必须连续，任一时刻只有一组负责。

    新班（from_crew=新班）在原班交出之前不可能成功；若它成功，
    必然排在原班交接之后，且交出方等于上一任接班方。
    """
    archive_store = Archive(tmp_path / 'c.sqlite3')
    handover_store = HandoverStore(tmp_path / 'c.sqlite3')
    saved = archive(archive_store, sample('dirty'), compute)
    from app.modules.handovers import service
    order = service.seed_run(handover_store, archive_store, saved['id'])['orders'][0]

    def submit(attempt):
        key, frm, to = attempt
        try:
            service.handoff(handover_store, order['id'],
                            dict(idempotency_key=key, from_crew=frm, to_crew=to))
            return 'ok'
        except DeskError as exc:
            return exc.code

    attempts = [(f'orig-{n}', '原班', '新班') for n in range(4)] + \
               [(f'newbie-{n}', '新班', '丙班') for n in range(4)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, attempts))
    # 原班的四个独立请求只有一个能成功，其余被 CAS 拒绝
    assert results.count('ok') >= 1
    chain = [event for event in handover_store.events(order['id']) if event['kind'] == 'HANDOVER']
    # 责任链：首环交出方必为原班；每环交出方等于上环接班方，绝不出现两组同时负责
    assert chain[0]['from_crew'] == '原班'
    for previous, current in zip(chain, chain[1:]):
        assert current['from_crew'] == previous['to_crew']
    newbie_event = next((event for event in chain if event['from_crew'] == '新班'), None)
    if newbie_event is not None:
        assert chain.index(newbie_event) > 0  # 新班只能在原班交出之后接手
    assert handover_store.get_order(order['id'])['owner'] == chain[-1]['to_crew']


def test_same_key_in_parallel_never_double_handoff(tmp_path):
    archive_store = Archive(tmp_path / 'b.sqlite3')
    handover_store = HandoverStore(tmp_path / 'b.sqlite3')
    saved = archive(archive_store, sample('dirty'), compute)
    from app.modules.handovers import service
    order = service.seed_run(handover_store, archive_store, saved['id'])['orders'][0]
    body = dict(idempotency_key='same-key', from_crew='原班', to_crew='新班')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.handoff(handover_store, order['id'], body), range(4)))
    # 全部返回同一成功结局，但只发生过一次真实交接
    assert len(results) == 4 and all(item['ok'] for item in results)
    assert handover_store.get_order(order['id'])['owner'] == '新班'
    assert [event['kind'] for event in handover_store.events(order['id'])].count('HANDOVER') == 1
    request_rows = handover_store.connect().execute(
        'SELECT COUNT(*) FROM handover_requests').fetchone()[0]
    assert request_rows == 1


def test_same_key_with_different_payload_conflicts(desk, first_order_id):
    desk.post(f'/api/dispositions/{first_order_id}/handovers',
              json=dict(idempotency_key='k1', from_crew='原班', to_crew='新班'))
    result = desk.post(f'/api/dispositions/{first_order_id}/handovers',
                       json=dict(idempotency_key='k1', from_crew='原班', to_crew='丙班'))
    assert (result.status_code, result.json['code']) == (409, 'REQUEST_CONFLICT')


def test_close_blocked_until_replay_confirms_offload(desk, first_order_id):
    result = desk.post(f'/api/dispositions/{first_order_id}/close', json={})
    assert (result.status_code, result.json['code']) == (422, 'REPLAY_NOT_CONFIRMED')
    assert desk.get(f'/api/dispositions/{first_order_id}/attribution').json['order']['status'] == 'open'


def test_close_after_offload_confirmed_updates_tally(desk, dirty_seeded, confirmed_run_id):
    run_id, seeded = dirty_seeded
    target = next(row for row in seeded['orders'] if row['tag'] == '000105')
    response = desk.post(f"/api/dispositions/{target['id']}/close",
                         json={'confirm_run': confirmed_run_id})
    assert response.status_code == 200
    board = desk.get(f'/api/runs/{run_id}/dispositions').json
    assert board['tally'] == dict(disposition_open=1, disposition_closed=1, disposition_total=2)
    closed = desk.get(f"/api/dispositions/{target['id']}/attribution").json
    assert closed['order']['status'] == 'closed'
    assert closed['order']['confirmed_run'] == confirmed_run_id
    assert closed['events'][-1]['kind'] == 'CLOSE'


def test_close_is_idempotent(desk, dirty_seeded, confirmed_run_id):
    _, seeded = dirty_seeded
    target = seeded['orders'][0]
    first = desk.post(f"/api/dispositions/{target['id']}/close",
                      json={'confirm_run': confirmed_run_id}).json
    second = desk.post(f"/api/dispositions/{target['id']}/close",
                       json={'confirm_run': confirmed_run_id}).json
    assert first['ok'] and second['replayed']
    events = desk.get(f"/api/dispositions/{target['id']}/attribution").json['events']
    assert [event['kind'] for event in events].count('CLOSE') == 1


def test_handoff_rejected_after_close(desk, dirty_seeded, confirmed_run_id):
    _, seeded = dirty_seeded
    target = seeded['orders'][0]
    desk.post(f"/api/dispositions/{target['id']}/close",
              json={'confirm_run': confirmed_run_id})
    result = desk.post(f"/api/dispositions/{target['id']}/handovers",
                       json=dict(idempotency_key='late', from_crew='原班', to_crew='新班'))
    assert (result.status_code, result.json['code']) == (409, 'ORDER_CLOSED')


def test_failed_request_is_replayed_with_same_outcome(desk, first_order_id):
    body = dict(idempotency_key='fail-key', from_crew='隔壁班', to_crew='新班')
    first = desk.post(f'/api/dispositions/{first_order_id}/handovers', json=body)
    second = desk.post(f'/api/dispositions/{first_order_id}/handovers', json=body)
    assert (first.status_code, first.json['code']) == (409, 'OWNER_MISMATCH')
    assert (second.status_code, second.json['code']) == (409, 'OWNER_MISMATCH')
    assert [event['kind'] for event in
            desk.get(f'/api/dispositions/{first_order_id}/attribution').json['events']] == ['OPEN']


def test_unknown_order_and_bad_input(desk):
    assert desk.post('/api/dispositions/nope/handovers',
                     json=dict(idempotency_key='x', from_crew='a', to_crew='b')).status_code == 404
    missing = desk.post(f'/api/dispositions/{first_order_id}/handovers', json=dict(from_crew='原班'))
    assert (missing.status_code, missing.json['code']) == (422, 'BAD_INPUT')
    same_crew = desk.post(f'/api/dispositions/{first_order_id}/handovers',
                          json=dict(idempotency_key='z', from_crew='原班', to_crew='原班'))
    assert (same_crew.status_code, same_crew.json['code']) == (422, 'BAD_INPUT')


def test_board_views_render_with_owner_and_history(desk, run, saved):
    run_id, _ = run
    page = desk.get(f'/desk/exceptions?run={run_id}')
    text = page.get_data(as_text=True)
    assert page.status_code == 200 and '错装处置单与班组交接' in text and '原班' in text
    ledger = desk.get(f'/desk/ledger?run={run_id}').get_data(as_text=True)
    assert '错装待处置件' in ledger


def test_tally_and_policy_helpers():
    orders = [dict(status='open', tag='a'), dict(status='open', tag='b'),
              dict(status='closed', tag='c')]
    assert pending_tally(orders) == dict(disposition_open=2, disposition_closed=1, disposition_total=3)
    stages = compute(confirmed_files())
    assert offload_confirmed(stages, '000105', 'NX-01')
    assert not offload_confirmed(compute(sample('dirty')), '000105', 'NX-01')
