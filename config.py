# -*- coding: utf-8 -*-
"""配置管理: 读写 config.json, 线程安全, 支持热重载与旧版(qq-archive)迁移"""
import hashlib
import json
import os
import secrets
import sys
import threading

if getattr(sys, "frozen", False):          # PyInstaller exe 模式: 数据落 exe 同目录
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

_lock = threading.Lock()
_cache = None

STYLES = {
    "brief":    {"name": "简洁", "points": "2-3个要点(每条不超过25字)", "summary": "一段80字内的总结"},
    "standard": {"name": "标准", "points": "3-6个要点(每条不超过30字)", "summary": "一段150字内的总结"},
    "detailed": {"name": "详细", "points": "5-8个要点(每条不超过35字), 如有观点分歧分别列出", "summary": "一段300字内的总结, 需覆盖讨论中的不同立场"},
}


def default_config() -> dict:
    return {
        "wizard_done": False,
        "web": {"password": "", "port": 8787},
        "llm": {
            "mode": "multimodal",        # text=纯文本 | dual=文本+视觉辅助 | multimodal=单一多模态
            "api_key": "",
            "base_url": "",
            "model": "",
            "vision_model": "",          # dual 模式的视觉辅助模型
            "vision_api_key": "",        # 空 = 复用 api_key
            "vision_base_url": "",       # 空 = 复用 base_url
            "timeout": 60,
            "price_in": 0.0,             # 元/百万输入token, 用于费用估算, 0=不估算
            "price_out": 0.0,
        },
        "bot": {"ws_url": "ws://127.0.0.1:3001", "ws_token": "123456"},
        "topic": {"idle_seconds": 1800, "min_msgs": 3, "max_msgs": 80},
        "trigger": {
            "command": "总结一下",
            "permission": "all",         # all=所有人 | admin=仅管理员
            "admins": [],
            "cooldown": 60,              # 秒, 防刷
        },
        "report": {"enabled": False, "time": "23:00", "to_group": 0},
        "interest": {
            "enabled": False,          # 兴趣度打分开关
            "keywords": [],            # 兴趣关键词(如 ["政策","房价","AI"])
            "push_threshold": 0,       # 打分>=此值推送高兴趣话题到群, 0=不推送
        },
        "groups": [],                    # [{group_id, enabled, summary_to, archive_to, style}]
    }


def _migrate_old(raw: dict) -> dict:
    """兼容旧版 qq-archive 的 config.json"""
    cfg = default_config()
    if raw.get("ws_url"):
        cfg["bot"]["ws_url"] = raw["ws_url"]
    if raw.get("ws_token"):
        cfg["bot"]["ws_token"] = raw["ws_token"]
    if raw.get("topic_idle_seconds"):
        cfg["topic"]["idle_seconds"] = int(raw["topic_idle_seconds"])
    if raw.get("min_topic_msgs"):
        cfg["topic"]["min_msgs"] = int(raw["min_topic_msgs"])
    if raw.get("llm_api_key"):
        cfg["llm"]["api_key"] = raw["llm_api_key"]
        cfg["llm"]["base_url"] = raw.get("llm_base_url", "")
        cfg["llm"]["model"] = raw.get("llm_model", "")
        cfg["llm"]["mode"] = "multimodal" if raw.get("llm_vision") else "text"
        cfg["wizard_done"] = True
    gid = raw.get("only_groups") or []
    if raw.get("summary_group_id") or raw.get("archive_group_id"):
        for g in gid:
            cfg["groups"].append({
                "group_id": int(g),
                "enabled": True,
                "summary_to": int(raw.get("summary_group_id") or g),
                "archive_to": int(raw.get("archive_group_id") or 0),
                "style": "standard",
            })
    return cfg


def _normalize(raw: dict) -> dict:
    """补齐缺失键, 保证结构完整"""
    cfg = default_config()
    for k, v in cfg.items():
        if k not in raw:
            continue
        if isinstance(v, dict) and isinstance(raw[k], dict):
            v.update(raw[k])
            cfg[k] = v
        else:
            cfg[k] = raw[k]
    cfg["groups"] = [g for g in (raw.get("groups") or []) if g.get("group_id")]
    return cfg


def load() -> dict:
    global _cache
    with _lock:
        if not os.path.exists(CONFIG_FILE):
            return default_config()
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return default_config()
        if "llm" not in raw:                       # 旧版结构
            cfg = _migrate_old(raw)
        else:
            cfg = _normalize(raw)
        _cache = cfg
        return json.loads(json.dumps(cfg))          # 深拷贝返回


def save(cfg: dict) -> dict:
    global _cache
    with _lock:
        cfg = _normalize(cfg)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
        _cache = cfg
        return json.loads(json.dumps(cfg))


def get() -> dict:
    """热重载入口: 每次从磁盘读取最新配置"""
    global _cache
    with _lock:
        if _cache is not None and os.path.exists(CONFIG_FILE):
            try:
                mtime = os.path.getmtime(CONFIG_FILE)
                if getattr(_cache, "_mtime", None) == mtime:
                    return json.loads(json.dumps(_cache))
            except Exception:
                pass
    return load()


def set_password(cfg: dict, plain: str):
    cfg["web"]["password"] = hash_password(plain)


def hash_password(plain: str) -> str:
    return hashlib.sha256(("qqcs:" + plain).encode("utf-8")).hexdigest()


def verify_password(plain: str, cfg: dict = None) -> bool:
    cfg = cfg or get()
    stored = cfg.get("web", {}).get("password") or ""
    if not stored:                                  # 未设密码 = 首次, 允许设置
        return False
    return secrets.compare_digest(hash_password(plain), stored)


def gen_secret() -> str:
    return secrets.token_hex(16)
