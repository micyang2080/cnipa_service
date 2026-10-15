# -*- coding: utf-8 -*-
"""HTTP 入队层（对齐 ip_externalLinker）；Worker 在同进程串行执行（对齐 external）。"""
from __future__ import annotations

import logging
import os

from flask import Flask, jsonify, request

from service import auth
from service import config
from service import task_service
from service.worker import start_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("cnipa.api")

app = Flask(__name__)


@app.before_request
def _auth():
    if request.method == "OPTIONS":
        return None
    path = request.path or ""
    if path in ("/health", "/cnipa/health"):
        return None
    if not path.startswith("/cnipa/"):
        return None
    tid = auth.extract_token(request)
    ok, msg, _uid, _name = auth.verify_token(tid)
    if not ok:
        return jsonify({"code": 4010, "msg": msg}), 401
    return None


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, tokenID, X-Token-ID"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/health")
@app.route("/cnipa/health")
def health():
    return jsonify({"ok": True})


@app.route("/cnipa/v1/env", methods=["GET", "OPTIONS"])
def env_status():
    if request.method == "OPTIONS":
        return ("", 204)
    from service import runner_invoke

    ok, data, err = runner_invoke.check_status(timeout_sec=120)
    if data:
        return jsonify({"ok": True, "env": data}), 200
    return jsonify({"ok": False, "error_code": "ENV_CHECK_FAIL", "error_message": err}), 500


@app.route("/cnipa/v1/tasks/<int:task_id>/cancel", methods=["POST", "OPTIONS"])
def cancel_task(task_id: int):
    if request.method == "OPTIONS":
        return ("", 204)
    from service import db
    from service import runner_invoke

    with db.conn() as c:
        ok = db.cancel_task(c.cursor(), task_id)
    runner_invoke.kill_current_uia()
    runner_invoke.kill_all_uia_runners()
    if not ok:
        data, code = task_service.get_task(task_id)
        if code == 404:
            return jsonify(data), 404
        return jsonify({**data, "ok": True, "message": "任务已结束，无法取消"}), 200
    data, code = task_service.get_task(task_id)
    return jsonify(data), code


@app.route("/cnipa/v1/tasks", methods=["POST", "OPTIONS"])
def create_task():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(silent=True) or {}
    data, code = task_service.create_task(body)
    return jsonify(data), code


@app.route("/cnipa/v1/tasks/<int:task_id>", methods=["GET", "OPTIONS"])
def get_task(task_id: int):
    if request.method == "OPTIONS":
        return ("", 204)
    data, code = task_service.get_task(task_id)
    return jsonify(data), code


@app.route("/cnipa/v1/tasks/lookup", methods=["POST", "OPTIONS"])
def lookup_task():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(silent=True) or {}
    data, code = task_service.lookup_task(body)
    return jsonify(data), code


def create_app():
    return app


if os.environ.get("CNIPA_DISABLE_WORKER", "").strip().lower() not in ("1", "true", "yes"):
    start_worker()
    log.info("cnipa_service ready; %s:%s", config.listen_host(), config.listen_port())
else:
    log.info("worker disabled (CNIPA_DISABLE_WORKER)")
