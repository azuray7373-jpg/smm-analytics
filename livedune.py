# -*- coding: utf-8 -*-
"""Коннектор LiveDune API (https://api.livedune.com).

Аккаунты Лайфдюн автоматически сопоставляются нашим каналам:
сначала по сохранённой карте livedune_map (Settings, JSON {ld_id: channel_id}),
при первом запуске — по эвристикам имени/типа. Дневная статистика пишется в
metric_snapshots (источник livedune), подпись gained/lost -> subscribed/unsubscribed.

Прямой режим (sync_livedune) работает там, где api.livedune.com доступен;
на PythonAnywhere free доступ закрыт прокси — данные доставляет релей
GitHub Actions через /api/ingest (ключ "ld").
"""
import json
from datetime import date, timedelta
import requests
from db import db, get_setting, set_setting, Channel, RunLog, Notification

API = "https://api.livedune.com"

# эвристики сопоставления по имени/типу (порядок важен)
NAME_RULES = [
    ("venera", "instagram", "ВЕНЕРА"),
    ("венера", "instagram", "ВЕНЕРА"),
    ("рецепт", "instagram", "Алексей новый"),
    ("школа сыроделия алексея", "instagram", "Дина"),
    ("сыроделие", "instagram", "Алексей старый"),   # главный аккаунт «СЫРОДЕЛИЕ | ШКОЛА №1»
]
TYPE_TO_NAME = {"telegram": "Telegram", "vk_group": "VK", "youtube": "YouTube",
                "tiktok": "TikTok", "max": "MAX основной", "dzen": "Дзен"}


def _token():
    return get_setting("livedune_token")


def configured():
    return bool(_token())


def _get(path, params, timeout=60):
    r = requests.get(f"{API}/{path}", params={"access_token": _token(), **params}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def list_accounts():
    accs, after = [], None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        d = _get("accounts", params)
        accs.extend(d.get("response", []))
        after = d.get("after")
        if not after or not d.get("response"):
            break
    return accs


# проверенная карта наших аккаунтов LiveDune (id ЛД -> наш канал)
DEFAULT_LD_MAP = {1482798: 1, 2079848: 2, 3411323: 3, 3126029: 4, 2667568: 5,
                  3323948: 6, 2381086: 8, 2824731: 9, 2383823: 10, 3333744: 11}


def channel_map():
    """ld_id -> channel_id. Сначала явные привязки каналов (ld_account_id,
    включая конкурентов), затем автосопоставление по именам."""
    m = dict(DEFAULT_LD_MAP)
    for c in Channel.query.filter(Channel.ld_account_id.isnot(None)):
        m[c.ld_account_id] = c.id
    raw = get_setting("livedune_map", "")
    if raw:
        try:
            for k, v in json.loads(raw).items():
                m.setdefault(int(k), int(v))
            return m
        except Exception:
            pass
    channels = {c.name: c.id for c in Channel.query.all()}
    m = {}
    for a in list_accounts():
        name = (a.get("name") or a.get("short_name") or "")
        low = name.lower()
        target = None
        for rule, atype, cname in NAME_RULES:
            if (a["type"] == atype or a["type"].startswith(atype)) and rule in low:
                target = cname
                break
        if not target:
            target = TYPE_TO_NAME.get(a["type"])
        if target and target in channels:
            m[a["id"]] = channels[target]
    set_setting("livedune_map", json.dumps(m))
    db.session.commit()
    return m


def fetch_history(ld_id, days):
    """Дневные записи за последние N дней (пагинация по created)."""
    start = (date.today() - timedelta(days=days)).isoformat()
    out, after = [], None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        d = _get(f"accounts/{ld_id}/history", params)
        rows = d.get("response", [])
        out.extend(r for r in rows if (r.get("created") or "") >= start)
        after = d.get("after")
        if not after or not rows or (rows[-1].get("created") or "") < start:
            break
    return out


def day_metrics(rec):
    """LD-запись дня -> {метрика: значение} в наших терминах."""
    out = {}
    posts = rec.get("posts") or 0
    def total(avg):
        return round((avg or 0) * posts) if avg is not None and posts else (avg or 0) * posts
    if rec.get("followers") is not None:
        out["followers"] = rec["followers"]
    if rec.get("gained") is not None:
        out["subscribed"] = rec["gained"]
    if rec.get("lost") is not None:
        out["unsubscribed"] = rec["lost"]
    reach = rec.get("reach") or {}
    if reach.get("total") is not None:
        out["reach"] = reach["total"]
    views = (rec.get("posts_views") or 0) + (rec.get("video_views") or 0)
    if views:
        out["views"] = views
    if rec.get("impressions") is not None:
        out["impressions"] = rec["impressions"]
    likes, comments, shares = total(rec.get("avg_likes")), total(rec.get("avg_comments")), total(rec.get("avg_reposts"))
    if likes:
        out["likes"] = likes
    if comments:
        out["comments"] = comments
    if shares:
        out["shares"] = shares
    if rec.get("stories_views"):
        out["opens"] = rec["stories_views"]
    return out


def sync_livedune(days=7, threaded=False):
    """Прямая синхронизация (для хостов с доступом к api.livedune.com).
    Возвращает пакет для релея: [{channel_id, date, metrics:{...}}]."""
    import threading
    from connectors import save_metric
    def _run():
        with __import__("app").app.app_context():
            try:
                r = RunLog(kind="livedune_sync", status="OK")
                db.session.add(r)
                db.session.commit()
                packet = collect_packet(days)
                n = 0
                for row in packet:
                    for m, v in row["metrics"].items():
                        save_metric(r.id, row["channel_id"], row["date"], m, v, "livedune")
                        n += 1
                r.details = f"accounts={len({row['channel_id'] for row in packet})} values={n}"
                db.session.commit()
            except Exception as e:
                db.session.add(Notification(level="error", message=f"Ошибка LiveDune: {e}"))
                db.session.commit()
                raise
    if threaded:
        threading.Thread(target=_run, daemon=True).start()
        return "запущена в фоне"
    _run()
    return "завершена"


def collect_packet(days=7):
    """Собрать дневные метрики всех аккаунтов (без записи в БД) — для релея."""
    cmap = channel_map()
    packet = []
    for ld_id, ch_id in cmap.items():
        try:
            for rec in fetch_history(ld_id, days):
                metrics = day_metrics(rec)
                if metrics and rec.get("created"):
                    packet.append({"channel_id": ch_id, "date": rec["created"], "metrics": metrics})
        except Exception:
            continue
    return packet


def ingest_packet(packet):
    """Приём пакета релея на стороне приложения: [{channel_id, date, metrics}]."""
    from connectors import save_metric
    r = RunLog(kind="livedune_sync", status="OK")
    db.session.add(r)
    db.session.commit()
    n = 0
    for row in packet:
        for m, v in (row.get("metrics") or {}).items():
            try:
                d = date.fromisoformat(str(row["date"])[:10])
            except ValueError:
                continue
            save_metric(r.id, int(row["channel_id"]), d, m, float(v), "livedune")
            n += 1
    r.details = f"values={n}"
    db.session.commit()
    return n


def can_reach(timeout=8):
    """Доступен ли api.livedune.com с этого хоста (для free-прокси PA — нет)."""
    try:
        requests.get(f"{API}/accounts", params={"access_token": _token()}, timeout=timeout)
        return True
    except Exception:
        return False
