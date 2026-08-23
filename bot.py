# -*- coding: utf-8 -*-
"""OneBot 11 WS 客户端: 收群消息 -> 留档/指令; 定时器 -> 空闲切分/日报"""
import json
import re
import threading
import time

import config
import summarizer

_ws = None
_api_lock = threading.Lock()


def log(msg):
    summarizer.log(msg)


# ------------------------- API 调用(fire-and-forget) -------------------------
def _send_api(action, params):
    """发送即忘: 不等待响应, 避免阻塞 WS 回调线程"""
    global _ws
    if _ws is None:
        return
    payload = {"action": action, "params": params,
               "echo": f"qqcs_{time.time_ns()}"}
    try:
        with _api_lock:
            _ws.send(json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        log(f"[API发送失败] {action}: {e}")


def send_group_text(group_id, text):
    _send_api("send_group_msg", {"group_id": int(group_id), "message": text})


def send_group_file(group_id, file_path):
    _send_api("send_group_msg", {
        "group_id": int(group_id),
        "message": [{"type": "file", "data": {"file": file_path}}]})


# ------------------------- 消息解析 -------------------------
def _extract(ev):
    """返回 (text, image_urls, raw_type)"""
    raw = ev.get("raw_message") or ""
    text_parts, image_urls = [], []
    msg = ev.get("message")
    if isinstance(msg, list):                       # 数组格式
        for seg in msg:
            t = seg.get("type")
            d = seg.get("data") or {}
            if t == "text":
                text_parts.append(d.get("text", ""))
            elif t == "image":
                u = d.get("url") or d.get("file") or ""
                if u.startswith("http"):
                    image_urls.append(u)
            elif t == "json":
                text_parts.append(_parse_json_card(str(d.get("data", ""))))
        raw_type = "image" if image_urls and not any(p.strip() for p in text_parts) else "text"
        return "".join(text_parts).strip(), image_urls, raw_type
    # 字符串格式(CQ码)
    image_urls = [u for u in re.findall(r"\[CQ:image,[^\]]*url=([^\],]+)", raw)]
    text = re.sub(r"\[CQ:[^\]]*\]", "", raw).strip()
    raw_type = "image" if image_urls and not text else "text"
    return text, image_urls, raw_type


def _parse_json_card(data_str):
    """QQ小程序/分享卡片: 提取标题与跳转链接"""
    try:
        s = (data_str.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]")
             .replace("&#58;", ":").replace("\\/", "/"))
        d = json.loads(s)
        title = d.get("title") or d.get("desc") or ""
        url = d.get("qqdocurl") or d.get("jumpUrl") or ""
        if title or url:
            return (title + " " + url).strip()
        return s[:100]
    except Exception:
        return data_str[:100]


def handle_group_event(ev):
    cfg = config.get()
    group_id = ev.get("group_id")
    user_id = ev.get("user_id")
    if not group_id:
        return
    sender = ev.get("sender") or {}
    nickname = sender.get("nickname") or sender.get("card") or str(user_id)
    text, image_urls, raw_type = _extract(ev)
    ts = ev.get("time") or int(time.time())
    msg_id = str(ev.get("message_id", ""))
    sub = ev.get("sub_type")
    if sub == "bot":                                # 不处理机器人自身消息
        return

    # 指令触发
    cmd = (cfg["trigger"].get("command") or "总结一下").strip()
    if text and text.strip() == cmd:
        reply = summarizer.manual_trigger(group_id, user_id, nickname)
        send_group_text(group_id, reply)
        return

    if not text and not image_urls:
        return
    summarizer.on_message(group_id, user_id, nickname, msg_id, raw_type,
                          text, image_urls, ev.get("raw_message") or "", ts)


# ------------------------- 连接循环 -------------------------
def _on_open(ws):
    global _ws
    _ws = ws
    log("WebSocket 已连接")


def _on_message(ws, message):
    try:
        ev = json.loads(message)
    except Exception:
        return
    if ev.get("post_type") == "message" and ev.get("message_type") == "group":
        try:
            handle_group_event(ev)
        except Exception as e:
            log(f"[消息处理异常] {e}")


def _on_close(ws, code, msg):
    global _ws
    _ws = None
    log(f"WebSocket 断开(code={code}), 5秒后重连")


def _on_error(ws, error):
    pass


def _timer_loop():
    """每 20 秒: 空闲话题切分 + 日报检查"""
    while True:
        time.sleep(20)
        try:
            summarizer.check_idle()
            summarizer.check_report()
        except Exception as e:
            log(f"[定时器异常] {e}")


def test_ws(ws_url=None, token=None):
    """向导/控制台: 测试 NapCat 连接, 返回 (ok, info)"""
    import websocket
    cfg = config.get()["bot"]
    url = ws_url or cfg["ws_url"]
    tk = token if token is not None else cfg["ws_token"]
    try:
        ws = websocket.create_connection(
            url, timeout=8,
            header={"Authorization": f"Bearer {tk}"} if tk else {})
        ws.send(json.dumps({"action": "get_login_info", "params": {}, "echo": "t1"}))
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                d = json.loads(ws.recv())
            except Exception:
                break
            if d.get("echo") == "t1":
                ws.close()
                info = d.get("data") or {}
                if d.get("retcode") == 0:
                    return True, f"已连接, 机器人账号 {info.get('user_id')} ({info.get('nickname')})"
                return False, f"鉴权失败: {d.get('wording') or d.get('msg')}"
        ws.close()
        return False, "未收到 NapCat 响应"
    except Exception as e:
        return False, f"连接失败: {e}"


def run_forever():
    """阻塞式连接循环(带重连), 在主线程调用"""
    import websocket
    threading.Thread(target=_timer_loop, daemon=True).start()
    summarizer.bind_sender(send_group_text, send_group_file)
    while True:
        cfg = config.get()["bot"]
        try:
            ws = websocket.WebSocketApp(
                cfg["ws_url"],
                header={"Authorization": f"Bearer {cfg['ws_token']}"} if cfg.get("ws_token") else {},
                on_open=_on_open, on_message=_on_message,
                on_close=_on_close, on_error=_on_error)
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            log(f"[WS异常] {e}")
        _ws = None
        time.sleep(5)
