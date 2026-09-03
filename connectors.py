# -*- coding: utf-8 -*-
"""Сбор данных. Схема fallback по ТЗ:
официальный API -> официальный XLSX/CSV -> автоматизированный браузер -> ручной ввод.
Здесь реализованы: YouTube API (если задан ключ), CSV-импорт, ручной ввод.
Остальные источники (Instagram Graph API, Лайфдюн, GetCourse) подключаются
через настройки токенов тем же интерфейсом коллектора."""
import csv, io, requests
from datetime import date, datetime
from db import db, MetricSnapshot, RunLog, Notification, get_setting, Channel

DAILY_METRICS = ["followers", "views", "impressions", "reach", "opens", "reads",
                 "likes", "comments", "saves", "shares", "reactions",
                 "subscribed", "unsubscribed"]


def start_run(kind):
    r = RunLog(kind=kind, status="OK")
    db.session.add(r)
    db.session.commit()
    return r.id


def finish_run(run_id, details="", status="OK"):
    r = RunLog.query.get(run_id)
    r.status = status
    r.details = details
    db.session.commit()


def save_metric(run_id, channel_id, d, metric, value, source, status="OK"):
    db.session.add(MetricSnapshot(channel_id=channel_id, date=d, metric=metric,
                                  value=value, status=status, source=source, run_id=run_id))


def collect_youtube(run_id):
    """YouTube Channels/Analytics API: подписчики и просмотры публичного канала по API-ключу."""
    key = get_setting("youtube_api_key")
    ch = Channel.query.filter(Channel.platform == "youtube", Channel.is_active == True).first()  # noqa
    if not key or not ch:
        return "youtube: ключ API не задан или канал не найден — пропуск"
    try:
        r = requests.get("https://www.googleapis.com/youtube/v3/channels",
                         params={"part": "statistics", "id": get_setting("youtube_channel_id", ""),
                                 "key": key}, timeout=20)
        st = r.json().get("items", [{}])[0].get("statistics", {})
        today = date.today()
        save_metric(run_id, ch.id, today, "followers", int(st.get("subscriberCount", 0)), "youtube_api")
        save_metric(run_id, ch.id, today, "views", int(st.get("viewCount", 0)), "youtube_api")
        db.session.commit()
        return "youtube: OK"
    except Exception as e:
        db.session.add(Notification(level="error", message=f"Ошибка YouTube API: {e}"))
        db.session.commit()
        return f"youtube: ERROR {e}"


def mark_missing(run_id, d=None):
    """Метим метрики всех каналов за дату как MISSING, если данных нет —
    чтобы отличать '0' от 'не получено'. Итог — сводное уведомление по ТЗ."""
    d = d or date.today()
    missing_by_channel = {}
    for ch in Channel.query.filter_by(is_active=True).all():
        have = {m.metric for m in MetricSnapshot.query.filter_by(channel_id=ch.id, date=d).all()}
        miss = [m for m in DAILY_METRICS if m not in have]
        for m in miss:
            save_metric(run_id, ch.id, d, m, None, "collector", status="MISSING")
        if miss:
            missing_by_channel[ch.name] = miss
    db.session.commit()
    if missing_by_channel:
        exists = Notification.query.filter(
            Notification.message.like(f"%не получены данные за {d}%")).first()
        if not exists:
            channels_list = ", ".join(sorted(missing_by_channel))
            db.session.add(Notification(level="error", message=(
                f"❌ Не получены данные за {d} по каналам: {channels_list} "
                f"(всего {sum(len(v) for v in missing_by_channel.values())} метрик MISSING — "
                "импорт CSV или ручной ввод на соответствующих экранах).")))
            db.session.commit()
    return f"MISSING: {sum(len(v) for v in missing_by_channel.values())} метрик по {len(missing_by_channel)} каналам"


def run_daily_collection():
    run_id = start_run("daily_collect")
    results = [collect_youtube(run_id), mark_missing(run_id)]
    finish_run(run_id, "; ".join(results))
    return results


ALLOWED_METRICS = set(DAILY_METRICS)


def import_csv_channel(fileobj):
    """CSV-импорт дневной статистики. Формат header:
    date,channel_id,metric,value[,status]
    date,registrations,utm_source,utm_medium,utm_campaign,landing,count  — для регистраций
    """
    run_id = start_run("csv_import")
    text = fileobj.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    n = 0
    for row in reader:
        row = {k.strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
        if not row.get("date"):
            continue
        d = datetime.strptime(row["date"][:10], "%Y-%m-%d").date()
        if row.get("metric"):
            if row["metric"] not in ALLOWED_METRICS:
                continue
            value = float(row["value"]) if row.get("value") not in (None, "",) else None
            status = row.get("status", "MANUAL" if value is not None else "MISSING").upper()
            save_metric(run_id, int(row["channel_id"]), d, row["metric"], value, "csv", status)
            n += 1
        elif row.get("registrations") or row.get("count"):
            from db import Registration
            cnt = float(row.get("count") or row.get("registrations") or 0)
            db.session.add(Registration(date=d, utm_source=row.get("utm_source", ""),
                                        utm_medium=row.get("utm_medium", ""),
                                        utm_campaign=row.get("utm_campaign", ""),
                                        landing=row.get("landing", ""), count=cnt, status="OK"))
            n += 1
    db.session.commit()
    finish_run(run_id, f"импортировано {n} строк")
    return n
