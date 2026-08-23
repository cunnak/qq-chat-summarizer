# -*- coding: utf-8 -*-
"""网页控制台: Flask 单页应用(登录/向导/仪表盘/设置/话题/日志/运维)"""
import functools
import hmac
import json
import os
import time

from flask import Flask, request, jsonify, Response

import config
import db
import llm
import summarizer

app = Flask(__name__)
_restart_flag = {"on": False}
_PAGE = None


# ------------------------- 鉴权 -------------------------
def _secret():
    cfg = config.get()
    s = cfg["web"].get("secret")
    if not s:
        cfg["web"]["secret"] = config.gen_secret()
        config.save(cfg)
        s = cfg["web"]["secret"]
    return s


def _token():
    return hmac.new(_secret().encode(), b"qqcs-auth", "sha256").hexdigest()


def _authed():
    return request.cookies.get("auth") == _token()


def login_required(fn):
    @functools.wraps(fn)
    def w(*a, **kw):
        if not _authed():
            return jsonify({"error": "unauthorized"}), 401
        return fn(*a, **kw)
    return w


def _mask(key: str) -> str:
    if not key:
        return ""
    return (key[:6] + "••••" + key[-4:]) if len(key) > 12 else "••••"


# ------------------------- 登录/状态 -------------------------
@app.post("/api/login")
def api_login():
    pw = (request.json or {}).get("password", "")
    cfg = config.get()
    if not cfg["web"].get("password"):                 # 首次: 设置密码
        if len(pw) < 4:
            return jsonify({"error": "密码至少 4 位"}), 400
        config.set_password(cfg, pw)
        config.save(cfg)
        resp = jsonify({"ok": True, "first": True})
        resp.set_cookie("auth", _token(), max_age=86400 * 30, samesite="Lax")
        return resp
    if config.verify_password(pw, cfg):
        resp = jsonify({"ok": True})
        resp.set_cookie("auth", _token(), max_age=86400 * 30, samesite="Lax")
        return resp
    return jsonify({"error": "密码错误"}), 401


@app.get("/api/state")
def api_state():
    cfg = config.get()
    import bot
    return jsonify({
        "has_password": bool(cfg["web"].get("password")),
        "wizard_done": bool(cfg.get("wizard_done")),
        "bot_connected": bot._ws is not None,
        "style_options": {k: v["name"] for k, v in config.STYLES.items()},
    })


# ------------------------- 配置读写 -------------------------
def _masked_config():
    cfg = config.get()
    c = json.loads(json.dumps(cfg))
    c["llm"]["api_key"] = _mask(cfg["llm"].get("api_key", ""))
    c["llm"]["vision_api_key"] = _mask(cfg["llm"].get("vision_api_key", ""))
    return c


@app.get("/api/config")
@login_required
def api_get_config():
    return jsonify(_masked_config())


@app.post("/api/config")
@login_required
def api_set_config():
    new = request.json or {}
    cfg = config.get()
    llm_new = new.get("llm") or {}
    # 哨兵处理: 掩码值 = 不修改原 key
    for field in ("api_key", "vision_api_key"):
        v = llm_new.get(field)
        if v is not None and "•" in str(v):
            llm_new[field] = cfg["llm"].get(field, "")
    for k in ("web", "llm", "bot", "topic", "trigger", "report"):
        if new.get(k) is not None:
            if k == "web":                              # 密码/端口/secret 不经此接口
                continue
            cfg[k] = new[k]
    if isinstance(new.get("groups"), list):
        cfg["groups"] = new["groups"]
    if "wizard_done" in new:
        cfg["wizard_done"] = bool(new["wizard_done"])
    cfg = config.save(cfg)
    summarizer.log("[控制台] 配置已保存并生效")
    return jsonify({"ok": True, "restart_hint": bool(new.get("bot"))})


# ------------------------- 连接测试 -------------------------
@app.post("/api/test-ws")
@login_required
def api_test_ws():
    body = request.json or {}
    import bot
    ok, info = bot.test_ws(body.get("ws_url"), body.get("ws_token"))
    return jsonify({"ok": ok, "info": info})


@app.post("/api/test-llm")
@login_required
def api_test_llm():
    body = request.json or {}
    cfg = config.get()
    key = body.get("api_key") or cfg["llm"]["api_key"]
    if "•" in str(key):
        key = cfg["llm"]["api_key"]
    base = body.get("base_url") or cfg["llm"]["base_url"]
    model = body.get("model") or cfg["llm"]["model"]
    if not (key and base and model):
        return jsonify({"ok": False, "info": "请填写 key / 接口地址 / 模型名"})
    try:
        client = llm.OpenAICompatLLM({"api_key": key, "base_url": base,
                                      "model": model, "timeout": 30})
        out = client.test()
        return jsonify({"ok": True, "info": f"模型响应正常: {out[:30]}"})
    except Exception as e:
        return jsonify({"ok": False, "info": f"调用失败: {str(e)[:120]}"})


# ------------------------- 仪表盘 -------------------------
@app.get("/api/dashboard")
@login_required
def api_dashboard():
    days = int(request.args.get("days", 1))
    since = time.time() - days * 86400
    stats = db.dashboard_stats(since)
    cfg = config.get()
    price_in = float(cfg["llm"].get("price_in") or 0)
    price_out = float(cfg["llm"].get("price_out") or 0)
    ti = sum(x["tokens_in"] for x in stats["llm"])
    to = sum(x["tokens_out"] for x in stats["llm"])
    calls = sum(x["calls"] for x in stats["llm"])
    cost = (ti / 1e6 * price_in + to / 1e6 * price_out) if (price_in or price_out) else None
    stats["llm_total"] = {"calls": calls, "tokens_in": ti, "tokens_out": to,
                          "cost": round(cost, 4) if cost is not None else None}
    stats["open_topics"] = len(db.find_open_topics())
    return jsonify(stats)


# ------------------------- 话题 -------------------------
@app.get("/api/topics")
@login_required
def api_topics():
    gid = request.args.get("group_id", type=int)
    status = request.args.get("status") or None
    page = max(1, request.args.get("page", 1, type=int))
    rows = db.list_topics(group_id=gid, status=status, limit=30, offset=(page - 1) * 30)
    for r in rows:
        r["participants"] = json.loads(r["participants"] or "[]")
        r["points"] = json.loads(r["points"] or "[]")
    return jsonify({"topics": rows, "page": page})


@app.get("/api/topic/<int:tid>")
@login_required
def api_topic(tid):
    t = db.get_topic(tid)
    if not t:
        return jsonify({"error": "not found"}), 404
    t["participants"] = json.loads(t["participants"] or "[]")
    t["points"] = json.loads(t["points"] or "[]")
    msgs = db.query_messages(t["group_id"], t["start_ts"], t["last_ts"])
    return jsonify({"topic": t, "messages": msgs})


@app.post("/api/topic/<int:tid>/regenerate")
@login_required
def api_topic_regen(tid):
    t = db.get_topic(tid)
    if not t:
        return jsonify({"error": "not found"}), 404
    result = summarizer.summarize_topic(t["group_id"], tid)
    if result:
        return jsonify({"ok": True, "result": result})
    return jsonify({"error": "生成失败"}), 500


# ------------------------- 日志/运维 -------------------------
@app.get("/api/logs")
@login_required
def api_logs():
    lines_num = min(int(request.args.get("lines", 200)), 1000)
    path = os.path.join(config.BASE_DIR, "run.log")
    if not os.path.exists(path):
        return jsonify({"logs": []})
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    return jsonify({"logs": [l.rstrip() for l in lines[-lines_num:]]})


@app.post("/api/export")
@login_required
def api_export():
    days = int((request.json or {}).get("days", 7))
    fmt = (request.json or {}).get("format", "md")
    since = time.time() - days * 86400
    rows = db.list_topics(status="closed", since=since, limit=500)
    if fmt == "html":
        parts = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>群聊话题导出</title></head><body>"]
        for t in rows:
            pts = "".join(f"<li>{p}</li>" for p in json.loads(t["points"] or "[]"))
            parts.append(f"<h2>{t['title']}</h2><p>群{t['group_id']} | "
                         f"{time.strftime('%m-%d %H:%M', time.localtime(t['start_ts']))} ~ "
                         f"{time.strftime('%H:%M', time.localtime(t['last_ts']))} | "
                         f"{t['msg_count']}条</p><ul>{pts}</ul><p>{t['summary']}</p><hr>")
        parts.append("</body></html>")
        content, mime, ext = "\n".join(parts), "text/html; charset=utf-8", "html"
    else:
        parts = [f"# 群聊话题导出（近{days}天）", ""]
        for t in rows:
            pts = "\n".join(f"- {p}" for p in json.loads(t["points"] or "[]"))
            parts.append(f"## {t['title']}\n\n群{t['group_id']} | "
                         f"{time.strftime('%m-%d %H:%M', time.localtime(t['start_ts']))}~"
                         f"{time.strftime('%H:%M', time.localtime(t['last_ts']))} | "
                         f"{t['msg_count']}条\n\n{pts}\n\n{t['summary']}\n\n---\n")
        content, mime, ext = "\n".join(parts), "text/markdown; charset=utf-8", "md"
    return Response(content, mimetype=mime, headers={
        "Content-Disposition": f"attachment; filename=topics_export.{ext}"})


@app.post("/api/backup")
@login_required
def api_backup():
    dest = os.path.join(config.BASE_DIR, f"backup_{time.strftime('%Y%m%d_%H%M%S')}.db")
    db.backup(dest)
    return jsonify({"ok": True, "path": dest})


@app.post("/api/clear-data")
@login_required
def api_clear():
    gid = (request.json or {}).get("group_id")
    db.clear_data(int(gid) if gid else None)
    summarizer.log(f"[控制台] 清空数据 group={gid or '全部'}")
    return jsonify({"ok": True})


@app.post("/api/restart")
@login_required
def api_restart():
    _restart_flag["on"] = True
    return jsonify({"ok": True})


def should_restart():
    return _restart_flag["on"]


# ------------------------- 页面 -------------------------
@app.get("/")
def index():
    return Response(_render_page(), mimetype="text/html; charset=utf-8")


def _render_page():
    global _PAGE
    if _PAGE is None:
        import sys
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base, "webui.html"), "r", encoding="utf-8") as f:
            _PAGE = f.read()
    return _PAGE


def run(port=None):
    cfg = config.get()
    port = port or int(cfg["web"].get("port") or 8787)
    app.run(host="0.0.0.0", port=port, threaded=True)
