# -*- coding: utf-8 -*-
"""LLM 客户端: 三种模式(纯文本/文本+视觉辅助/单一多模态), OpenAI 兼容接口"""
import json
import re
import time

import config
import db


# ------------------------- 工具 -------------------------
def _parse_llm_json(content: str, msg_count: int) -> dict:
    content = (content or "").strip()
    m = re.search(r"\{[\s\S]*\}", content)
    if m:
        try:
            d = json.loads(m.group(0))
            return {"title": str(d.get("title", ""))[:40] or "群聊话题",
                    "points": [str(p)[:60] for p in (d.get("points") or [])][:8],
                    "summary": str(d.get("summary", ""))[:400]}
        except Exception:
            pass
    return {"title": f"群聊话题（{msg_count}条消息）",
            "points": [content[:60]] if content else [],
            "summary": content[:200] or "（总结生成失败）"}


def _estimate_tokens(text: str) -> int:
    """粗估: 中文约1字=1token, 英文约4字符=1token"""
    cn = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    other = len(text or "") - cn
    return int(cn + other / 4)


class MockLLM:
    """无 key 时的占位客户端(不调 API)"""
    name = "mock"

    def summarize(self, messages, style="standard"):
        return {"title": f"群聊话题（{len(messages)}条消息）",
                "points": ["（未配置 LLM，此为占位总结）"],
                "summary": "当前未配置 API key，总结为占位内容。请在控制台完成 LLM 配置。"}

    def describe_images(self, urls):
        return {u: "（图片内容未识别）" for u in urls}

    def score_topic(self, title, points, summary, keywords):
        """兴趣打分: 关键词命中数 -> 0-100 分(无关键词=None)"""
        if not keywords:
            return None
        kws = [k for k in keywords if k]
        text = (title or "") + " " + (summary or "") + " " + " ".join(points or [])
        hit = sum(1 for k in kws if k in text)
        if not hit:
            return {"score": 0, "reason": "与兴趣关键词无直接关联"}
        score = min(100, 40 + hit * 15)
        hit_kws = "、".join(k for k in kws if k in text)
        return {"score": score, "reason": f"命中关键词: {hit_kws}"}


class OpenAICompatLLM:
    def __init__(self, llm_cfg: dict):
        import requests
        self._requests = requests
        self.cfg = llm_cfg
        self.mode = llm_cfg.get("mode", "text")
        self.api_key = llm_cfg.get("api_key", "")
        self.base_url = (llm_cfg.get("base_url") or "").rstrip("/")
        self.model = llm_cfg.get("model", "")
        self.vision_model = llm_cfg.get("vision_model", "")
        self.vision_key = llm_cfg.get("vision_api_key") or self.api_key
        self.vision_base_url = (llm_cfg.get("vision_base_url") or self.base_url).rstrip("/")
        self.timeout = int(llm_cfg.get("timeout", 60))
        self.name = self.model

    # ------------------------- 底层调用 -------------------------
    def _chat(self, messages, model=None, key=None, base_url=None, max_tokens=800,
              kind="summary", msgs=0, record=True):
        r = self._requests.post(
            f"{base_url or self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key or self.api_key}",
                     "Content-Type": "application/json"},
            json={"model": model or self.model, "messages": messages,
                  "temperature": 0.4, "max_tokens": max_tokens},
            timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        if record:
            db.add_stat(kind, model=model or self.model, msgs=msgs,
                        tokens_in=usage.get("prompt_tokens") or 0,
                        tokens_out=usage.get("completion_tokens") or 0)
        return content

    # ------------------------- 视觉辅助(dual 模式) -------------------------
    def describe_images(self, urls):
        """把图片转成文字描述, 供纯文本主模型使用"""
        out = {}
        for u in urls:
            try:
                content = self._chat(
                    [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": u}},
                        {"type": "text", "text": "用一段不超过60字的话描述这张图片的关键信息(文字/数据/场景)。"}]}],
                    model=self.vision_model, key=self.vision_key, base_url=self.vision_base_url,
                    max_tokens=150, kind="vision")
                out[u] = content.strip()[:80]
            except Exception:
                out[u] = ""            # 失败不阻断总结
        return out

    # ------------------------- 总结 -------------------------
    def summarize(self, messages, style="standard"):
        st = config.STYLES.get(style, config.STYLES["standard"])
        lines, image_urls = [], []
        for m in messages:
            ts = time.strftime("%m-%d %H:%M", time.localtime(m["ts"]))
            text = m["text"] or ""
            imgs = m.get("image_urls") or []
            if imgs:
                image_urls.extend(imgs)
                if not text:
                    text = "（图片消息）"
            lines.append(f"{ts} {m['nickname']}: {text[:200]}")
        chat = "\n".join(lines[-int(config.get()["topic"]["max_msgs"]):])

        prompt = (
            "你是群聊总结助手。下面是某QQ群一段话题的聊天记录(格式: 时间 昵称: 内容)。\n"
            f"请：1) 用不超过20字概括主题标题; 2) 提炼{st['points']}, "
            "如果涉及政策/时事/新闻，明确指出具体政策或事件; 3) 写{st['summary']}。\n"
            '只输出JSON: {"title":"...","points":["..."],"summary":"..."}\n'
            "聊天记录:\n" + chat)

        total_in = _estimate_tokens(prompt)
        if self.mode == "dual" and image_urls:
            # 双模型: 视觉辅助先提取图片信息, 并入文本
            descs = self.describe_images(image_urls)
            usable = [f"[图片内容: {d}]" for d in descs.values() if d]
            if usable:
                prompt += "\n本话题中的图片信息:\n" + "\n".join(usable)
            content = self._chat([{"role": "user", "content": prompt}],
                                 max_tokens=900, kind="summary", msgs=len(messages))
        elif self.mode == "multimodal" and image_urls:
            # 单一多模态模型: 直接带图
            content_blocks = [{"type": "text", "text": prompt}]
            seen = set()
            for u in image_urls:
                if u not in seen:
                    seen.add(u)
                    content_blocks.append({"type": "image_url", "image_url": {"url": u}})
            if len(content_blocks) > 1:
                content_blocks.append({"type": "text",
                                       "text": "以上图片是本话题中群友发送的，请把图片里的关键信息(如截图文字、图表数据)也纳入总结。"})
            content = self._chat([{"role": "user", "content": content_blocks}],
                                 max_tokens=900, kind="summary", msgs=len(messages))
        else:
            if image_urls:
                prompt = prompt.replace("聊天记录:", "聊天记录(图片消息以[图片消息]标注):")
            content = self._chat([{"role": "user", "content": prompt}],
                                 kind="summary", msgs=len(messages))
        result = _parse_llm_json(content, len(messages))
        result["_tokens_in_est"] = total_in
        return result

    # ------------------------- 兴趣度打分 -------------------------
    def score_topic(self, title, points, summary, keywords):
        """基于兴趣关键词对话题打分(0-100)并给出理由。无关键词返回 None。"""
        if not keywords:
            return None
        kws = [str(k).strip() for k in keywords if str(k).strip()]
        if not kws:
            return None
        pts = "；".join(points or [])
        prompt = (
            "你是兴趣匹配评估器。用户对以下关键词感兴趣: " + "、".join(kws) + "。\n"
            "下面是群聊的一个话题总结，请评估该话题对用户的兴趣匹配度:\n"
            f"标题: {title}\n要点: {pts}\n总结: {(summary or '')[:300]}\n"
            "打分标准: 90-100=高度契合用户兴趣(核心主题就是关键词所指), "
            "70-89=明显相关(大量讨论关键词相关内容), 40-69=部分相关(提及但非主题), "
            "0-39=基本无关。\n"
            '只输出JSON: {"score": 0-100整数, "reason": "不超过30字的中文理由"}')
        try:
            content = self._chat([{"role": "user", "content": prompt}],
                                 max_tokens=200, kind="interest")
            m = re.search(r"\{[\s\S]*\}", content or "")
            if not m:
                return None
            d = json.loads(m.group(0))
            score = max(0, min(100, int(float(d.get("score", 0)))))
            return {"score": score, "reason": str(d.get("reason", ""))[:60]}
        except Exception:
            return None            # 打分失败不阻断总结

    # ------------------------- 日报 -------------------------
    def daily_report(self, topic_rows):
        """topic_rows: [{title, points, summary, group_id, msg_count}]"""
        items = []
        for t in topic_rows:
            pts = "；".join(json.loads(t["points"] or "[]")) if isinstance(t["points"], str) else ""
            items.append(f"- {t['title']}（{t['msg_count']}条）：{t['summary'][:120]}" +
                         (f" 要点: {pts[:100]}" if pts else ""))
        prompt = (
            "你是群聊日报生成助手。以下是某QQ群今天各话题的总结，请生成一份每日群聊日报：\n"
            "1) 20字内的日报标题; 2) 3-8条要点(每条不超过40字, 覆盖今天所有话题); "
            "3) 100字内的今日总评(讨论氛围/关注焦点)。\n"
            '只输出JSON: {"title":"...","points":["..."],"summary":"..."}\n'
            "今日话题:\n" + "\n".join(items))
        content = self._chat([{"role": "user", "content": prompt}],
                             kind="report", msgs=len(topic_rows))
        return _parse_llm_json(content, len(topic_rows))

    # ------------------------- 测试(向导/控制台用) -------------------------
    def test(self):
        r = self._requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json={"model": self.model, "messages": [{"role": "user", "content": "回复: OK"}],
                  "max_tokens": 10},
            timeout=self.timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def get_client():
    """根据当前配置返回 LLM 客户端(每次新建, 实现热切换)"""
    cfg = config.get()["llm"]
    if cfg.get("api_key") and cfg.get("base_url") and cfg.get("model"):
        return OpenAICompatLLM(cfg)
    return MockLLM()
