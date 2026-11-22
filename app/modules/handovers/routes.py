from flask import Blueprint, current_app, jsonify, request

from . import service

bp = Blueprint('handovers', __name__)


def handovers():
    return current_app.extensions['transfer_handovers']


def archive():
    return current_app.extensions['transfer_archive']


@bp.post('/api/runs/<run_id>/dispositions')
def seed(run_id):
    return jsonify(service.seed_run(handovers(), archive(), run_id))


@bp.get('/api/runs/<run_id>/dispositions')
def board(run_id):
    return jsonify(service.board(handovers(), run_id))


@bp.post('/api/dispositions/<order_id>/handovers')
def handoff(order_id):
    return jsonify(service.handoff(handovers(), order_id, request.get_json(silent=True)))


@bp.post('/api/dispositions/<order_id>/close')
def close(order_id):
    body = request.get_json(silent=True) or {}
    return jsonify(service.close(handovers(), archive(), order_id, body.get('confirm_run')))


@bp.get('/api/dispositions/<order_id>/attribution')
def attribution(order_id):
    return jsonify(service.attribution(handovers(), order_id))
