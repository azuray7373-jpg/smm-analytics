# -*- coding: utf-8 -*-
"""Релей уведомлений: забирает очередь из приложения и доставляет в Telegram.
Запускается на GitHub Actions (api.telegram.org доступен). Если TG-секретов нет —
мягко завершается, не ломая остальные шаги workflow."""
import os, sys
import requests

PA_URL = os.environ.get("PA_URL", "").rstrip("/")
INGEST = os.environ.get("PA_INGEST_TOKEN")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")


def main():
    if not (PA_URL and INGEST):
        sys.exit("Нужны PA_URL, PA_INGEST_TOKEN")
    r = requests.get(f"{PA_URL}/api/notifications/pending",
                     params={"token": INGEST}, timeout=60)
    r.raise_for_status()
    items = r.json().get("items", [])
    print(f"недоставленных уведомлений: {len(items)}")
    if not items:
        return
    if not (TG_TOKEN and TG_CHAT):
        print("TG_BOT_TOKEN/TG_CHAT_ID не заданы — уведомления остаются в очереди (экран 🔔).")
        return
    icons = {"error": "❌", "warn": "⚠️", "info": "ℹ️"}
    sent_ids = []
    for it in items:
        text = f"{icons.get(it['level'], '•')} {it['message'][:3500]}"
        try:
            resp = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                                 json={"chat_id": TG_CHAT, "text": text}, timeout=30)
            if resp.status_code == 200:
                sent_ids.append(it["id"])
            else:
                print("tg error:", resp.text[:150])
        except Exception as e:
            print("tg exception:", e)
    if sent_ids:
        r2 = requests.post(f"{PA_URL}/api/notifications/delivered",
                           json={"ids": sent_ids}, headers={"X-Ingest-Token": INGEST}, timeout=60)
        print("помечено доставленными:", len(sent_ids), r2.status_code)


if __name__ == "__main__":
    main()
