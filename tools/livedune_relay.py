# -*- coding: utf-8 -*-
"""Релей LiveDune -> приложение (запускается на GitHub Actions, где доступен
api.livedune.com). Забирает дневную статистику всех аккаунтов за N дней и
отправляет пакет на POST /api/ingest (ключ "ld")."""
import json, os, sys
from datetime import date, timedelta
import requests

API = "https://api.livedune.com"
TOKEN = os.environ.get("LIVEDUNE_TOKEN")
PA_URL = os.environ.get("PA_URL", "").rstrip("/")
INGEST = os.environ.get("PA_INGEST_TOKEN")
DAYS = int(os.environ.get("DAYS", "3"))


def get(path, params):
    r = requests.get(f"{API}/{path}", params={"access_token": TOKEN, **params}, timeout=90)
    r.raise_for_status()
    return r.json()


def main():
    if not (TOKEN and PA_URL and INGEST):
        sys.exit("Нужны LIVEDUNE_TOKEN, PA_URL, PA_INGEST_TOKEN")
    accounts, after = [], None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        d = get("accounts", params)
        accounts.extend(d.get("response", []))
        after = d.get("after")
        if not after or not d.get("response"):
            break
    print("аккаунтов LiveDune:", len(accounts))
    # карта соответствия ld_id -> channel_id приходит из env LD_MAP (JSON)
    ld_map = json.loads(os.environ.get("LD_MAP", "{}"))
    start = (date.today() - timedelta(days=DAYS)).isoformat()
    packet = []
    for a in accounts:
        ch = ld_map.get(str(a["id"]))
        if not ch:
            continue
        rows, cursor = [], None
        while True:
            params = {"limit": 100}
            if cursor:
                params["after"] = cursor
            h = get(f"accounts/{a['id']}/history", params)
            recs = h.get("response", [])
            rows.extend(r for r in recs if (r.get("created") or "") >= start)
            cursor = h.get("after")
            if not cursor or not recs or (recs[-1].get("created") or "") < start:
                break
        for rec in rows:
            metrics = {}
            posts = rec.get("posts") or 0
            def total(avg):
                return round((avg or 0) * posts)
            if rec.get("followers") is not None:
                metrics["followers"] = rec["followers"]
            if rec.get("gained") is not None:
                metrics["subscribed"] = rec["gained"]
            if rec.get("lost") is not None:
                metrics["unsubscribed"] = rec["lost"]
            if (rec.get("reach") or {}).get("total") is not None:
                metrics["reach"] = rec["reach"]["total"]
            views = (rec.get("posts_views") or 0) + (rec.get("video_views") or 0)
            if views:
                metrics["views"] = views
            if rec.get("impressions") is not None:
                metrics["impressions"] = rec["impressions"]
            if posts:
                metrics["likes"] = total(rec.get("avg_likes"))
                metrics["comments"] = total(rec.get("avg_comments"))
                metrics["shares"] = total(rec.get("avg_reposts"))
            if rec.get("stories_views"):
                metrics["opens"] = rec["stories_views"]
            if metrics:
                packet.append({"channel_id": int(ch), "date": rec["created"], "metrics": metrics})
    print("строк дневной статистики:", len(packet))
    r = requests.post(f"{PA_URL}/api/ingest",
                      json={"ld": packet},
                      headers={"X-Ingest-Token": INGEST}, timeout=300)
    print("ingest:", r.status_code, r.text[:200])
    r.raise_for_status()


if __name__ == "__main__":
    main()
