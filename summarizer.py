# -*- coding: utf-8 -*-
"""话题管理: 留档入库、空闲切分、总结流水线、手动触发、每日日报"""
import json
import os
import re
import threading
import time

import config
import db
import llm

MD_DIR = os.path.join(config.BASE_DIR, "archive_md")
os.makedirs(MD_DIR, exist_ok=True)

_state = {}          # group_id -> {"topic_id", "last_ts", "msg_count", "participants", "start_ts"}
_lock = threading.Lock()
_last_trigger = {}   # group_id -> ts (手动触发冷却)
_report_done = ""    # 日报去重日期标记


def log(msg: str):
    line = time.strftime("[%H:%M:%S] ") + msg
    try:
        with open(os.path.join(config.BASE_DIR, "run.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
    except Exception:
        pass
    print(line, flush=True)


# ------------------------- 群配置 -------------------------
def group_cfg(group_id):
    """返回某群的生效配置(未配置的群用默认值, 默认启用)"""
    for g in config.get()["groups"]:
        if int(g["group_id"]) == int(group_id):
            return g
    return {"group_id": group_id, "enabled": True,
            "summary_to": group_id, "archive_to": 0, "style": "standard"}


# ------------------------- 消息处理 -------------------------
def on_message(group_id, user_id, nickname, msg_id, raw_type, text, image_urls, raw, ts):
    gc = group_cfg(group_id)
    if not gc.get("enabled", True):
        return
    db.insert_message(group_id, user_id, nickname, msg_id, raw_type,
                      text, image_urls, raw, ts)
    with _lock:
        st = _state.get(group_id)
        if st is None or ts - st["last_ts"] > config.get()["topic"]["idle_seconds"]:
            # 开新话题(残留 open 的话题先按 discarded 关闭, 除非消息数达标)
            if st:
                _finalize(st, group_id)
            topic_id = db.create_topic(group_id, ts)
            st = {"topic_id": topic_id, "last_ts": ts, "msg_count": 0,
                  "participants": set(), "start_ts": ts}
            _state[group_id] = st
            log(f"[话题开启] 群{group_id} 话题#{topic_id}")
        st["last_ts"] = ts
        st["msg_count"] += 1
        st["participants"].add(str(nickname or user_id))
        db.update_topic_open(st["topic_id"], ts, st["msg_count"], st["participants"])
        log(f"[留档] 群{group_id} {nickname}: {(text or '[' + raw_type + ']')[:40]}")


def _finalize(st, group_id):
    """话题收尾: 不足门槛 discarded, 否则 closed 并触发总结"""
    if st["msg_count"] < config.get()["topic"]["min_msgs"]:
        db.close_topic(st["topic_id"], "discarded")
        log(f"[话题丢弃] 群{group_id} 话题#{st['topic_id']} 仅{st['msg_count']}条")
    else:
        db.close_topic(st["topic_id"], "closed")
        summarize_topic(group_id, st["topic_id"], st)


def check_idle(now=None):
    """由 bot 定时器调用: 关闭空闲超时的话题"""
    now = now or time.time()
    idle = config.get()["topic"]["idle_seconds"]
    with _lock:
        for gid, st in list(_state.items()):
            if now - st["last_ts"] > idle and st["msg_count"] > 0:
                _finalize(st, gid)
                del _state[gid]


# ------------------------- 总结 -------------------------
def summarize_topic(group_id, topic_id, st=None):
    gc = group_cfg(group_id)
    style = gc.get("style", "standard")
    try:
        t = db.get_topic(topic_id) or {}
        messages = db.query_messages(group_id, t["start_ts"], t["last_ts"])
        if not messages:
            log(f"[总结跳过] 话题#{topic_id} 无消息")
            return None
        client = llm.get_client()
        result = client.summarize(messages, style)
        db.save_summary(topic_id, result["title"], result["points"], result["summary"], style)
        log(f"[总结完成] 群{group_id} 话题#{topic_id}: {result['title']}")
        # 兴趣度打分(配置开启时)
        score_info = score_topic(topic_id, result)
        # 发回讨论群
        summary_to = int(gc.get("summary_to") or group_id)
        if summary_to:
            text = _summary_text(result, t, group_id, score_info)
            _send_group(summary_to, text)
        # 高兴趣话题推送(打分>=阈值 且 不是发回群本身)
        if score_info and summary_to:
            threshold = int(config.get()["interest"].get("push_threshold") or 0)
            if threshold > 0 and score_info["score"] >= threshold:
                push_text = (f"🔥 高兴趣话题提醒：{result['title']}\n"
                             f"兴趣度 {score_info['score']}/100 · {score_info['reason']}\n"
                             f"完整总结见上方话题总结")
                _send_group(summary_to, push_text)
                log(f"[兴趣推送] 话题#{topic_id} 分数{score_info['score']}>=阈值{threshold}")
        # md 存档
        md = _build_md(result, t, messages, group_id)
        archive_to = int(gc.get("archive_to") or 0)
        if archive_to:
            path = _save_md(group_id, result["title"], md)
            _send_file(archive_to, path)
        return result
    except Exception as e:
        log(f"[总结失败] 话题#{topic_id}: {e}")
        return None


def score_topic(topic_id, result=None):
    """兴趣度打分并入库; 返回 {"score", "reason"} 或 None(未启用/无关键词/失败)"""
    try:
        interest = config.get()["interest"]
        if not interest.get("enabled"):
            return None
        kws = interest.get("keywords") or []
        if not kws:
            return None
        if result is None:
            t = db.get_topic(topic_id) or {}
            result = {"title": t.get("title", ""),
                      "points": json.loads(t.get("points") or "[]"),
                      "summary": t.get("summary", "")}
        client = llm.get_client()
        info = client.score_topic(result["title"], result["points"], result["summary"], kws)
        if info:
            db.save_interest(topic_id, info["score"], info["reason"])
            log(f"[兴趣打分] 话题#{topic_id}: {info['score']}分 ({info['reason']})")
        return info
    except Exception as e:
        log(f"[兴趣打分失败] 话题#{topic_id}: {e}")
        return None


def _summary_text(result, t, group_id, score_info=None):
    pts = "\n".join(f"• {p}" for p in result["points"])
    start = time.strftime("%H:%M", time.localtime(t["start_ts"]))
    end = time.strftime("%H:%M", time.localtime(t["last_ts"]))
    names = json.loads(t["participants"] or "[]")
    head = (f"📋 话题总结：{result['title']}\n"
            f"⏱ {start}~{end} | {t['msg_count']}条 | {len(names)}人参与\n")
    if score_info:
        head += f"🎯 兴趣度 {score_info['score']}/100（{score_info['reason']}）\n"
    body = (f"\n{pts}\n" if pts else "") + f"\n{result['summary']}"
    return head + body


def _build_md(result, t, messages, group_id):
    start = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["start_ts"]))
    end = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["last_ts"]))
    names = json.loads(t["participants"] or "[]")
    lines = [f"# 话题总结：{result['title']}", "",
             f"- 群号：{group_id}", f"- 时间：{start} ~ {end}",
             f"- 消息数：{t['msg_count']} 条 | 参与 {len(names)} 人：{'、'.join(names)}", "",
             "## 要点", ""]
    lines += [f"- {p}" for p in result["points"]]
    lines += ["", "## 总结", "", result["summary"], "", "## 原始消息摘录", ""]
    for m in messages:
        ts = time.strftime("%H:%M", time.localtime(m["ts"]))
        img = f" [附{len(m['image_urls'])}张图]" if m.get("image_urls") else ""
        lines.append(f"> {ts} **{m['nickname']}**: {m['text'][:150]}{img}")
    return "\n".join(lines)


def _save_md(group_id, title, content):
    day = time.strftime("%Y%m%d")
    safe = re.sub(r"[\\/:*?\"<>|#\s]", "_", title)[:30] or "topic"
    path = os.path.join(MD_DIR, f"{day}_{safe}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# ------------------------- 手动触发 -------------------------
def manual_trigger(group_id, user_id, nickname):
    """群指令触发: 总结当前进行中的话题. 返回回复文本"""
    cfg = config.get()["trigger"]
    # 权限
    if cfg.get("permission") == "admin":
        admins = [int(a) for a in (cfg.get("admins") or [])]
        if int(user_id) not in admins:
            return "⛔ 你没有触发总结的权限"
    # 冷却
    now = time.time()
    last = _last_trigger.get(group_id, 0)
    if now - last < int(cfg.get("cooldown", 60)):
        wait = int(cfg.get("cooldown", 60) - (now - last))
        return f"⏱ 操作太频繁，请 {wait} 秒后再试"
    _last_trigger[group_id] = now

    with _lock:
        st = _state.get(group_id)
        if st is None or st["msg_count"] < 1:
            return "当前没有进行中的话题可总结"
        _state.pop(group_id, None)
        db.close_topic(st["topic_id"], "closed")
    result = summarize_topic(group_id, st["topic_id"], st)
    if result:
        return f"✅ 已总结话题「{result['title']}」"
    return "总结生成失败，请稍后再试"


# ------------------------- 每日日报 -------------------------
def check_report():
    """由 bot 定时器调用: 到点生成日报"""
    global _report_done
    cfg = config.get()["report"]
    if not cfg.get("enabled") or not cfg.get("to_group"):
        return
    hhmm = time.strftime("%H:%M")
    today = time.strftime("%Y-%m-%d")
    if hhmm < cfg.get("time", "23:00") or _report_done == today:
        return
    _report_done = today
    try:
        day_start = time.mktime(time.strptime(today, "%Y-%m-%d"))
        rows = db.list_topics(status="closed", since=day_start)
        if not rows:
            log("[日报] 今日无已总结话题, 跳过")
            return
        client = llm.get_client()
        result = client.daily_report(rows)
        pts = "\n".join(f"• {p}" for p in result["points"])
        text = (f"📰 群聊日报 {today}\n\n{pts}\n\n{result['summary']}\n"
                f"（今日 {len(rows)} 个话题）")
        _send_group(int(cfg["to_group"]), text)
        # 同步 md 存档
        md = f"# 群聊日报 {today}\n\n## 要点\n\n" + \
             "\n".join(f"- {p}" for p in result["points"]) + \
             f"\n\n## 总评\n\n{result['summary']}\n\n## 今日话题\n\n" + \
             "\n".join(f"- {t['title']}（{t['msg_count']}条）" for t in rows)
        path = _save_md(0, f"日报{today}", md)
        log(f"[日报] 已发送至群{cfg['to_group']}, 存档 {path}")
    except Exception as e:
        log(f"[日报失败] {e}")


# ------------------------- 发送(由 bot 注入实现, 解耦) -------------------------
_send_group = None
_send_file = None


def bind_sender(send_group_fn, send_file_fn):
    global _send_group, _send_file
    _send_group = send_group_fn
    _send_file = send_file_fn
