# -*- coding: utf-8 -*-
"""核心逻辑测试: 配置/话题管线/手动触发/双模式/日报/控制台API"""
import json
import os
import sys
import time

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, ROOT)

import config as cfg_mod
import db as db_mod
import llm as llm_mod
import summarizer as sum_mod

# ---------------- 重定向数据文件到测试目录 ----------------
T = int(time.time())
cfg_mod.CONFIG_FILE = os.path.join(TEST_DIR, "test_config.json")
db_mod.DB_FILE = os.path.join(TEST_DIR, "test_archive.db")
sum_mod.MD_DIR = os.path.join(TEST_DIR, "test_md")
os.makedirs(sum_mod.MD_DIR, exist_ok=True)
for f in (cfg_mod.CONFIG_FILE, db_mod.DB_FILE):
    if os.path.exists(f):
        os.remove(f)

passed = []


def ok(name):
    passed.append(name)
    print(f"  ✓ {name}")


# ---------------- 1. 配置: 默认/迁移/密码 ----------------
print("=== 1. 配置管理 ===")
c = cfg_mod.load()
assert c["llm"]["mode"] == "multimodal" and c["web"]["port"] == 8787
assert cfg_mod.verify_password("x", c) is False
cfg_mod.set_password(c, "test1234")
c2 = cfg_mod.save(c)
assert cfg_mod.verify_password("test1234", c2) is True
assert cfg_mod.verify_password("wrong", c2) is False
ok("默认配置+密码哈希")

# 旧版迁移
old = {"ws_url": "ws://1.2.3.4:3001", "ws_token": "tk", "topic_idle_seconds": 900,
       "min_topic_msgs": 5, "llm_api_key": "sk-old", "llm_base_url": "https://x/v1",
       "llm_model": "m1", "llm_vision": True, "only_groups": [111],
       "summary_group_id": 222, "archive_group_id": 333}
with open(cfg_mod.CONFIG_FILE, "w", encoding="utf-8") as f:
    json.dump(old, f, ensure_ascii=False)
m = cfg_mod.load()
assert m["bot"]["ws_url"] == "ws://1.2.3.4:3001" and m["llm"]["api_key"] == "sk-old"
assert m["llm"]["mode"] == "multimodal" and m["groups"][0]["group_id"] == 111
assert m["groups"][0]["summary_to"] == 222 and m["groups"][0]["archive_to"] == 333
ok("旧版 config.json 迁移")

# 新格式密码经 load 保留
with open(cfg_mod.CONFIG_FILE, "w", encoding="utf-8") as f:
    json.dump({"llm": {"api_key": "k"}, "web": {"password": cfg_mod.hash_password("test1234"), "port": 8787}},
              f, ensure_ascii=False)
m = cfg_mod.load()
assert cfg_mod.verify_password("test1234", m) is True
ok("新格式配置密码保留")

# 写入测试用新配置
c = cfg_mod.load()
c["groups"] = [{"group_id": 999, "enabled": True, "summary_to": 999, "archive_to": 888, "style": "standard"}]
c["trigger"] = {"command": "总结一下", "permission": "all", "admins": [555], "cooldown": 60}
c["report"] = {"enabled": False, "time": "23:00", "to_group": 0}
cfg_mod.save(c)

db_mod.init()

# ---------------- 2. 话题管线 (mock LLM) ----------------
print("=== 2. 话题管线 ===")
sent_text, sent_files = [], []
sum_mod.bind_sender(lambda gid, txt: sent_text.append((gid, txt)),
                    lambda gid, p: sent_files.append((gid, p)))
assert isinstance(llm_mod.get_client(), llm_mod.MockLLM)  # 无 key = mock

ev = lambda uid, nick, text, ts: sum_mod.on_message(999, uid, nick, "m%d" % ts, "text", text, [], text, ts)
for i, (uid, nick, txt) in enumerate([
        (111, "张三", "最近房地产政策又松了"), (222, "李四", "二线城市都取消限购了"),
        (111, "张三", "房贷利率也降了"), (333, "王五", "https://example.com/news?a=1")]):
    ev(uid, nick, txt, T + i)

sum_mod.check_idle(now=T + 3600)          # 1小时后空闲 -> 触发关闭+总结
rows = db_mod.list_topics(group_id=999)
assert rows[0]["status"] == "closed" and rows[0]["msg_count"] == 4
assert len(sent_text) == 1 and sent_text[0][0] == 999           # 总结发回本群
assert "话题总结" in sent_text[0][1]
assert len(sent_files) == 1 and sent_files[0][0] == 888         # md 发到存档群
assert os.path.exists(sent_files[0][1])
ok("话题留档->空闲关闭->总结发群->md存档发群")

# 不足门槛丢弃
ev(111, "张三", "嗯", T + 4000)
sum_mod.check_idle(now=T + 4000 + 1801)
rows = db_mod.list_topics(group_id=999)
assert rows[0]["status"] == "discarded"
ok("不足min_msgs的话题被丢弃")

# ---------------- 3. 手动触发权限/冷却 ----------------
print("=== 3. 手动触发 ===")
c = cfg_mod.load()
c["trigger"]["permission"] = "admin"
cfg_mod.save(c)
r = sum_mod.manual_trigger(999, 111, "张三")             # 非管理员
assert "权限" in r
ok("仅管理员模式: 普通人被拒")

c = cfg_mod.load(); c["trigger"]["permission"] = "all"; c["trigger"]["cooldown"] = 0
cfg_mod.save(c)
ev(111, "张三", "新话题A", T + 6000)
ev(222, "李四", "继续聊A", T + 6001)
ev(111, "张三", "再说A", T + 6002)
r = sum_mod.manual_trigger(999, 111, "张三")
assert "已总结" in r or "话题" in r
ok("手动触发总结(所有人模式)")

# ---------------- 4. dual 模式消息构造 ----------------
print("=== 4. 双模型(dual)构造 ===")
captured = {}


def fake_post(url, headers=None, json=None, timeout=None):
    captured.setdefault("calls", []).append(url)
    captured["last_payload"] = json
    if json["model"] == "vis-m":                     # 视觉辅助调用
        captured["vision_call"] = True
        content = "图片是一张房价数据表"
    else:
        content = '{"title":"t","points":["p"],"summary":"s"}'

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    return R()


llm_mod.requests = __import__("types")
client = llm_mod.OpenAICompatLLM({
    "mode": "dual", "api_key": "k", "base_url": "https://x/v1", "model": "text-m",
    "vision_model": "vis-m", "vision_api_key": "k2", "vision_base_url": "https://y/v1",
    "timeout": 5})
client._requests = type("M", (), {"post": staticmethod(fake_post)})()
msgs = [{"nickname": "张三", "text": "看这张图", "ts": T, "image_urls": ["http://img/1.jpg"]},
        {"nickname": "李四", "text": "数据真高", "ts": T + 1, "image_urls": []}]
captured["vision_call"] = False
result = client.summarize(msgs, "standard")
assert captured.get("vision_call") is True, "dual 模式应先调视觉模型"
assert "[图片内容: 图片是一张房价数据表]" in captured["last_payload"]["messages"][0]["content"]
assert result["title"] == "t"
ok("dual: 视觉提取->并入文本->主模型总结")

# multimodal 模式: 主模型 content 数组带图
client.mode = "multimodal"
captured["vision_call"] = False
client.summarize(msgs, "standard")
p = captured["last_payload"]["messages"][0]["content"]
assert isinstance(p, list) and any(b["type"] == "image_url" for b in p)
ok("multimodal: 单模型 content 数组直接带图")

# text 模式: 纯字符串
client.mode = "text"
client.summarize(msgs, "standard")
assert isinstance(captured["last_payload"]["messages"][0]["content"], str)
ok("text: 纯文本字符串(图片降级)")

# ---------------- 5. 日报 ----------------
print("=== 5. 每日日报 ===")
c = cfg_mod.load()
c["report"] = {"enabled": True, "time": "00:01", "to_group": 888}
cfg_mod.save(c)
sum_mod._report_done = ""
sent_text.clear()
# 造一个今天已总结的话题
tid = db_mod.create_topic(999, T - 100)
db_mod.update_topic_open(tid, T - 50, 5, {"a", "b"})
db_mod.close_topic(tid, "closed")
db_mod.save_summary(tid, "房价讨论", ["p1"], "s1", "standard")


class FakeReportLLM:
    name = "fake"

    def summarize(self, messages, style="standard"):
        return {"title": "t", "points": [], "summary": "s"}

    def daily_report(self, rows):
        captured["report_rows"] = rows
        return {"title": "日报", "points": ["a"], "summary": "b"}


orig_get_client = llm_mod.get_client
llm_mod.get_client = lambda: FakeReportLLM()
sum_mod.check_report()
llm_mod.get_client = orig_get_client
assert len(captured["report_rows"]) >= 1
assert any(g == 888 and "日报" in t for g, t in sent_text)
ok("日报生成并发送到指定群")

# ---------------- 6. 控制台 API ----------------
print("=== 6. 控制台 API ===")
import webapp
os.remove(cfg_mod.CONFIG_FILE)            # 重置: 测试首次设密码流程
webapp._PAGE = None
webapp.app.config["TESTING"] = True
client_http = webapp.app.test_client()

r = client_http.get("/api/state").get_json()
assert r["has_password"] is False and r["wizard_done"] is False
r = client_http.post("/api/login", json={"password": "abcd1234"}).get_json()
assert r.get("ok") and r.get("first")
ok("首次登录即设置密码")

r = client_http.get("/api/config")
assert r.status_code == 200
data = r.get_json()
assert data["llm"]["mode"] == "multimodal"
ok("读取配置(key掩码)")

# 哨兵保存: key 传掩码不覆盖
c = cfg_mod.load()
c["llm"]["api_key"] = "sk-real-key-123456"
cfg_mod.save(c)
data = client_http.get("/api/config").get_json()
masked = data["llm"]["api_key"]
assert "•" in masked
r = client_http.post("/api/config", json={"llm": {"api_key": masked, "base_url": "https://new/v1",
                                                  "model": "m9", "mode": "dual"}})
assert r.get_json()["ok"]
c = cfg_mod.load()
assert c["llm"]["api_key"] == "sk-real-key-123456"    # 未被掩码覆盖
assert c["llm"]["base_url"] == "https://new/v1" and c["llm"]["mode"] == "dual"
ok("配置保存+key哨兵保护")

# 未登录 401
r = webapp.app.test_client().get("/api/config")
assert r.status_code == 401
ok("未登录访问被拒(401)")

# 话题/仪表盘
r = client_http.get("/api/topics?group_id=999").get_json()
assert len(r["topics"]) >= 1
r = client_http.get("/api/dashboard?days=1").get_json()
assert r["messages"] >= 5 and r["llm_total"]["calls"] >= 1
ok("话题列表+仪表盘统计")

print()
print(f">>> 全部通过: {len(passed)} 项")
for p in passed:
    print("   -", p)
