# -*- coding: utf-8 -*-
"""Математика: только обычные формулы. Нейросеть сюда не допускается."""
from datetime import date, timedelta
from sqlalchemy import func
from db import db, MetricSnapshot, ContentItem, ContentStat, Registration, Channel

# Показатели-суммы за период (не подписчики!)
SUM_METRICS = ["views", "impressions", "reach", "opens", "reads", "likes", "comments",
               "saves", "shares", "reactions", "subscribed", "unsubscribed"]
INTERACTIONS = ["likes", "comments", "saves", "shares", "reactions"]

# Соответствие utm_source -> platform (для атрибуции регистраций)
UTM_TO_PLATFORM = {
    "instagram": "instagram", "ig": "instagram", "syrover_school": "instagram",
    "youtube": "youtube", "yt": "youtube",
    "max": "max", "telegram": "telegram", "tg": "telegram",
    "tiktok": "tiktok", "vk": "vk", "dzen": "dzen",
}


def week_bounds(d: date):
    """Неделя: фиксированный интервал понедельник–воскресенье."""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def month_bounds(d: date):
    """Месяц: всегда календарный 1 число .. последнее число."""
    start = d.replace(day=1)
    if d.month == 12:
        nxt = d.replace(year=d.year + 1, month=1)
    else:
        nxt = d.replace(month=d.month + 1)
    return start, nxt - timedelta(days=1)


def previous_week(d: date):
    s, e = week_bounds(d)
    return s - timedelta(days=7), e - timedelta(days=7)


def previous_month(d: date):
    s, e = month_bounds(d)
    return (s - timedelta(days=1)).replace(day=1), s - timedelta(days=1)


def prev_period(start: date, end: date):
    """Предыдущий период той же длины."""
    n = (end - start).days
    return start - timedelta(days=n + 1), start - timedelta(days=1)


def latest_snapshots(start: date, end: date, channel_id=None):
    """Актуальное значение каждой метрики на каждый день периода.
    Возвращает {(channel_id, date, metric): (value, status, source)}."""
    q = (db.session.query(MetricSnapshot.channel_id, MetricSnapshot.date, MetricSnapshot.metric,
                          MetricSnapshot.value, MetricSnapshot.status,
                          func.max(MetricSnapshot.fetched_at).label("ts"))
         .filter(MetricSnapshot.date >= start, MetricSnapshot.date <= end)
         .group_by(MetricSnapshot.channel_id, MetricSnapshot.date, MetricSnapshot.metric))
    if channel_id:
        q = q.filter(MetricSnapshot.channel_id == channel_id)
    out = {}
    for r in q.all():
        out[(r.channel_id, r.date, r.metric)] = (r.value, r.status)
    return out


def aggregate(start: date, end: date, channel_id=None):
    """Суммы метрик за период + подписчики на начало/конец + статусы данных."""
    snap = latest_snapshots(start, end, channel_id)
    agg = {m: 0.0 for m in SUM_METRICS}
    statuses = {}
    for (ch, d, m), (v, st) in snap.items():
        if m in agg and v is not None:
            agg[m] += v
        if st != "OK":
            statuses.setdefault(st, []).append(f"метрика {m} за {d}")
    # подписчики: последнее известное значение на/до конца и на/до начала
    def followers_on(ch, on_date):
        row = (MetricSnapshot.query.filter_by(channel_id=ch, metric="followers")
               .filter(MetricSnapshot.date <= on_date)
               .order_by(MetricSnapshot.date.desc(), MetricSnapshot.fetched_at.desc()).first())
        row_after = (MetricSnapshot.query.filter_by(channel_id=ch, metric="followers")
                     .filter(MetricSnapshot.date >= on_date)
                     .order_by(MetricSnapshot.date.asc(), MetricSnapshot.fetched_at.desc()).first())
        vals = [r.value for r in (row, row_after) if r and r.value is not None]
        return vals[0] if vals else None

    channels = Channel.query.filter_by(is_active=True)
    if channel_id:
        channels = channels.filter(Channel.id == channel_id)
    fol_end = fol_start = 0
    known = 0
    for ch in channels.all():
        a, b = followers_on(ch.id, end), followers_on(ch.id, start - timedelta(days=1))
        if a is not None:
            fol_end += a
            known += 1
        if b is not None:
            fol_start += b
    agg["followers_end"] = fol_end if known else None
    agg["followers_start"] = fol_start if fol_start else None
    return agg, statuses


def registrations_total(start: date, end: date, platform=None):
    q = db.session.query(func.sum(Registration.count)).filter(
        Registration.date >= start, Registration.date <= end, Registration.status == "OK")
    if platform:
        sources = [k for k, v in UTM_TO_PLATFORM.items() if v == platform]
        q = q.filter(Registration.utm_source.in_(sources))
    return q.scalar() or 0.0


def indicators(agg: dict, regs: float) -> dict:
    """Все производные показатели. Месячные считаются заново из месячных абсолютов,
    никогда как среднее недельных процентов."""
    out = {}
    inter = sum(agg.get(m) or 0 for m in INTERACTIONS)
    out["interactions"] = inter
    out["net_growth"] = (agg.get("subscribed") or 0) - (agg.get("unsubscribed") or 0)
    if agg.get("followers_start"):
        out["audience_growth_pct"] = ((agg["followers_end"] or 0) - agg["followers_start"]) / agg["followers_start"] * 100
    if agg.get("reach"):
        out["ERR"] = inter / agg["reach"] * 100
        out["CV_reach"] = (regs / agg["reach"] * 100) if regs is not None else None
    if agg.get("followers_end"):
        avg_fol = ((agg["followers_start"] or agg["followers_end"]) + agg["followers_end"]) / 2
        out["ER"] = inter / avg_fol * 100
    if agg.get("impressions"):
        out["open_CTR"] = (agg.get("opens") or 0) / agg["impressions"] * 100
    if agg.get("opens"):
        out["read_through"] = (agg.get("reads") or 0) / agg["opens"] * 100
    return out


def period_report(start: date, end: date, channel_id=None):
    """Полный срез по периоду: агрегаты + показатели + сравнение с предыдущим периодом."""
    agg, statuses = aggregate(start, end, channel_id)
    ch = Channel.query.get(channel_id) if channel_id else None
    regs = registrations_total(start, end, ch.platform if ch else None)
    ind = indicators(agg, regs)
    p_start, p_end = prev_period(start, end)
    pagg, _ = aggregate(p_start, p_end, channel_id)
    pregs = registrations_total(p_start, p_end, ch.platform if ch else None)
    pind = indicators(pagg, pregs)

    def delta(key, unit="pct"):
        """Абсолютная и относительная динамика. unit: pct | pp | abs."""
        cur, prev = agg.get(key), pagg.get(key)
        if key == "ERR":
            cur, prev = ind.get("ERR"), pind.get("ERR")
        if cur is None or prev in (None, 0):
            return None
        if unit == "pp":
            return {"cur": cur, "prev": prev, "d": cur - prev}
        if unit == "abs":
            return {"cur": cur, "prev": prev, "d": cur - prev}
        return {"cur": cur, "prev": prev, "d": (cur - prev) / prev * 100}

    deltas = {
        "reach": delta("reach"), "views": delta("views"), "registrations":
        {"cur": regs, "prev": pregs, "d": ((regs - pregs) / pregs * 100) if pregs else None},
        "ERR": delta("ERR", "pp"), "CV_reach":
        {"cur": ind.get("CV_reach"), "prev": pind.get("CV_reach"),
         "d": (ind["CV_reach"] - pind["CV_reach"]) if (ind.get("CV_reach") is not None and pind.get("CV_reach") is not None) else None},
        "net_growth": delta("net_growth", "abs"),
        "followers_end": delta("followers_end", "abs"),
    }
    # GetCourse: заказы и оплаты за период (по календарным датам, как всё остальное)
    gc = {}
    try:
        from db import GcOrder, GcPayment
        gc["orders"] = GcOrder.query.filter(GcOrder.date >= start, GcOrder.date <= end).count()
        gc["payments"] = GcPayment.query.filter(GcPayment.date >= start, GcPayment.date <= end).count()
        gc["payments_sum"] = db.session.query(func.coalesce(func.sum(GcPayment.amount), 0)).filter(
            GcPayment.date >= start, GcPayment.date <= end, GcPayment.status == "accepted").scalar()
    except Exception:
        gc = {"orders": 0, "payments": 0, "payments_sum": 0}
    return {"start": start.isoformat(), "end": end.isoformat(),
            "agg": {k: v for k, v in agg.items()}, "ind": ind, "deltas": deltas,
            "registrations": regs, "data_statuses": statuses, "gc": gc}


def content_stats_for_period(start: date, end: date, channel_id=None):
    """Лучшие/худшие материалы: суммарная статистика каждой единицы контента за период."""
    q = (db.session.query(ContentItem, ContentStat)
         .join(ContentStat, ContentStat.content_id == ContentItem.id)
         .filter(ContentStat.date >= start, ContentStat.date <= end))
    if channel_id:
        q = q.filter(ContentItem.channel_id == channel_id)
    items = {}
    for ci, cs in q.all():
        it = items.setdefault(ci.id, {"item": ci, "views": 0, "reach": 0, "likes": 0, "comments": 0,
                                      "saves": 0, "shares": 0, "reactions": 0, "subs": 0, "registrations": 0})
        for k in ("views", "reach", "likes", "comments", "saves", "shares", "reactions", "subs", "registrations"):
            it[k] += (getattr(cs, k) or 0)
    for it in items.values():
        inter = it["likes"] + it["comments"] + it["saves"] + it["shares"] + it["reactions"]
        it["interactions"] = inter
        it["ERR"] = inter / it["reach"] * 100 if it["reach"] else None
        it["CV"] = it["registrations"] / it["reach"] * 100 if it["reach"] else None
    return list(items.values())


def top_flop(items, key, n=10):
    valid = [i for i in items if i.get(key) is not None]
    s = sorted(valid, key=lambda x: x[key], reverse=True)
    return s[:n], list(reversed(s[-n:])) if s else []
