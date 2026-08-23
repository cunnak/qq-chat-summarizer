# -*- coding: utf-8 -*-
"""群聊总结助手 入口: 启动网页控制台 + OneBot 连接循环"""
import argparse
import os
import subprocess
import sys
import threading
import time
import webbrowser

import config
import db
import summarizer
import webapp


def open_browser(url):
    for _ in range(20):                     # 等控制台就绪
        time.sleep(0.5)
        try:
            import requests
            requests.get(url, timeout=2)
            break
        except Exception:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def restart_monitor():
    """监听控制台的重启请求"""
    while True:
        time.sleep(2)
        if webapp.should_restart():
            summarizer.log("[重启] 收到控制台重启请求")
            time.sleep(1)
            if os.environ.get("QQCS_SYSTEMD") == "1":
                os._exit(0)                  # systemd 自动拉起
            if getattr(sys, "frozen", False):
                subprocess.Popen([sys.executable])
            else:
                subprocess.Popen([sys.executable] + sys.argv)
            os._exit(0)


def main():
    parser = argparse.ArgumentParser(description="QQ 群聊总结助手")
    parser.add_argument("--port", type=int, default=None, help="控制台端口(默认取配置)")
    args = parser.parse_args()

    cfg = config.load()
    db.init()
    port = args.port or int(cfg["web"].get("port") or 8787)
    url = f"http://127.0.0.1:{port}"

    # 控制台线程
    threading.Thread(target=webapp.run, args=(port,), daemon=True).start()
    # 重启监视线程
    threading.Thread(target=restart_monitor, daemon=True).start()
    # 首次运行自动打开浏览器(向导)
    if not cfg.get("wizard_done"):
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()

    summarizer.log("=" * 50)
    summarizer.log(f"群聊总结助手启动 | 控制台: {url} | 数据目录: {config.BASE_DIR}")
    summarizer.log(f"WS: {cfg['bot']['ws_url']} | LLM: {cfg['llm'].get('model') or '未配置'}"
                   f"({cfg['llm'].get('mode')})")
    summarizer.log("=" * 50)

    import bot
    bot.run_forever()                        # 阻塞


if __name__ == "__main__":
    main()
