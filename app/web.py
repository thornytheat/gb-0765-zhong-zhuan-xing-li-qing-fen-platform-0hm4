from flask import Blueprint, jsonify, render_template, request
from .errors import DeskError
from .input import sample, submission
from .modules.archive.routes import store
from .modules.evidence.service import index
from .modules.handovers.service import board as disposition_board
from .pipeline import compute
from .reasons import REASONS

DESK = {
    'scans': '扫描原件', 'legs': '航段候选', 'replay': '扫件回放', 'exceptions': '错载与漏扫',
    'ledger': '去向对账', 'archive': '批次留档', 'evidence': '联程证据',
}
bp = Blueprint('desk', __name__)


def snapshot():
    run = request.args.get('run')
    name = request.args.get('sample', 'dirty')
    if run:
        files, result = store().get(run)
        return files, result['stages'], result['manifest'], name
    files = sample(name)
    return files, compute(files), None, name


@bp.get('/api/stage/<key>')
def stage_api(key):
    if key not in tuple(DESK)[:5]:
        raise DeskError('NO_STAGE', '业务入口不存在', 404)
    run = request.args.get('run')
    if run:
        return jsonify(store().get(run)[1]['stages'][key])
    return jsonify(compute(sample(request.args.get('sample', 'dirty')), until=key))


@bp.post('/api/stage/<key>')
def inspect_stage(key):
    if key not in tuple(DESK)[:5]:
        raise DeskError('NO_STAGE', '业务入口不存在', 404)
    return jsonify(compute(submission(request.get_json(silent=True)), until=key))


@bp.get('/', defaults={'key': 'ledger'})
@bp.get('/desk/<key>')
def desk(key):
    if key not in DESK:
        raise DeskError('NO_STAGE', '业务入口不存在', 404)
    files, stages, manifest, name = snapshot()
    dispositions = disposition_board(store_handovers(), manifest['id']) if manifest else None
    # 只复制计数后再合并，绝不改动归档结果原文（证据页要核对结果哈希）
    ledger_counts = dict(stages['ledger']['counts'])
    if dispositions:
        ledger_counts.update(dispositions['tally'])
    context = dict(stages=stages, ledger=ledger_counts, names=DESK, key=key, reasons=REASONS,
                   selected=name, manifest=manifest, archive=store().list() if key == 'archive' else [],
                   dispositions=dispositions,
                   report=index(files, dict(stages=stages, manifest=manifest)) if key == 'evidence' and manifest else None)
    return render_template('desk.html', **context)


def store_handovers():
    from flask import current_app
    return current_app.extensions['transfer_handovers']
