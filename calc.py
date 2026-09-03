# -*- coding: utf-8 -*-
"""Математика: только обычные формулы. Нейросеть сюда не допускается."""
import json
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


import time as _time

_TTL = {}


def ttl_cache(ttl_seconds):
    """Простой кэш результатов по аргументам с временем жизни."""
    def deco(fn):
        def wrapper(*args, **kwargs):
            key = (fn.__name__, args, tuple(sorted(kwargs.items())))
            hit = _TTL.get(key)
            now = _time.time()
            if hit and now - hit[0] < ttl_seconds:
                return hit[1]
            val = fn(*args, **kwargs)
            _TTL[key] = (now, val)
            return val
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def invalidate_caches():
    _TTL.clear()


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
    statuses = {}   # компактно: статус -> {count, days, sample}
    for (ch, d, m), (v, st) in snap.items():
        if m in agg and v is not None:
            agg[m] += v
        if st != "OK":
            e = statuses.setdefault(st, {"count": 0, "days": set(), "sample": []})
            e["count"] += 1
            e["days"].add(str(d))
            if len(e["sample"]) < 5:
                e["sample"].append(f"{m} за {d}")
    statuses = {k: {"count": v["count"], "days": sorted(v["days"]), "sample": v["sample"]}
                for k, v in statuses.items()}
    # подписчики: два пакетных запроса на все каналы сразу (было 2 запроса на канал)
    q = Channel.query.filter_by(is_active=True, is_competitor=False)
    if channel_id:
        q = q.filter(Channel.id == channel_id)
    ch_ids = [c.id for c in q.all()]

    def followers_batch(on_date):
        """Последнее известное значение followers на/до даты для каждого канала."""
        rows = (db.session.query(MetricSnapshot.channel_id,
                                 MetricSnapshot.date, MetricSnapshot.value,
                                 db.func.max(MetricSnapshot.fetched_at))
                .filter(MetricSnapshot.channel_id.in_(ch_ids),
                        MetricSnapshot.metric == "followers",
                        MetricSnapshot.date <= on_date,
                        MetricSnapshot.value.isnot(None))
                .group_by(MetricSnapshot.channel_id, MetricSnapshot.date,
                          MetricSnapshot.value)
                .order_by(MetricSnapshot.channel_id, MetricSnapshot.date.desc()).all())
        best = {}
        for r in rows:
            if r[0] not in best:
                best[r[0]] = r[2]   # первая (=самая поздняя) дата на канал
        return best

    fe = followers_batch(end) if ch_ids else {}
    fs = followers_batch(start - timedelta(days=1)) if ch_ids else {}
    fol_end = sum(v for v in fe.values() if v)
    fol_start = sum(v for v in fs.values() if v)
    agg["followers_end"] = fol_end if fe else None
    agg["followers_start"] = fol_start if fol_start else None
    return agg, statuses


def registrations_total(start: date, end: date, platform=None):
    q = db.session.query(func.sum(Registration.count)).filter(
        Registration.date >= start, Registration.date <= end, Registration.status == "OK",
        ~Registration.utm_source.like("demo_%"))
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


@ttl_cache(120)
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


def compare_best_worst(items, key="ERR", n=10):
    """Сравнение содержания ТОП-n и худших-n материалов: длительность, рубрики,
    CTA, форматы. Выводы строятся только из рассчитанных чисел."""
    best, flop = top_flop(items, key, n)
    if not best or not flop:
        return "", best, flop

    def stats(group):
        durs = [i["item"].duration_sec for i in group if i["item"].duration_sec]
        rubrics = {}
        cta = formats = 0
        for i in group:
            tags = json.loads(i["item"].ai_tags or "{}")
            r = tags.get("рубрика")
            if r:
                rubrics[r] = rubrics.get(r, 0) + 1
            if tags.get("есть_регистрационный_CTA"):
                cta += 1
            if i["item"].format:
                formats += 0  # считаем ниже по форматам
        fmts = {}
        for i in group:
            f = i["item"].format
            if f:
                fmts[f] = fmts.get(f, 0) + 1
        reach = sum(i.get("reach") or 0 for i in group) / len(group)
        return {"dur": (sum(durs) / len(durs)) if durs else None,
                "rubrics": rubrics, "cta_share": cta / len(group) * 100,
                "formats": fmts, "avg_reach": reach}

    sb, sf = stats(best), stats(flop)
    L = []
    if sb["dur"] and sf["dur"]:
        rel = (sb["dur"] - sf["dur"]) / sf["dur"] * 100 if sf["dur"] else None
        word = "длиннее" if rel and rel > 0 else "короче"
        L.append(f"Лучшие материалы в среднем {abs(rel):.0f}% {word} по длительности "
                 f"({sb['dur']:.0f}с против {sf['dur']:.0f}с).")
    if sb["cta_share"] != sf["cta_share"]:
        L.append(f"Регистрационный CTA стоит в {sb['cta_share']:.0f}% лучших против "
                 f"{sf['cta_share']:.0f}% худших.")
    top_r = max(sb["rubrics"].items(), key=lambda x: x[1]) if sb["rubrics"] else None
    if top_r:
        L.append(f"Среди лучших чаще всего рубрика «{top_r[0]}» ({top_r[1]} из {len(best)}).")
    if sb["avg_reach"] and sf["avg_reach"]:
        L.append(f"Средний охват лучшего материала — {sb['avg_reach']:,.0f} против "
                 f"{sf['avg_reach']:,.0f} у худшего "
                 f"(x{sb['avg_reach']/sf['avg_reach']:.1f}).".replace(",", " "))
    return "\n".join("- " + x for x in L), best, flop


@ttl_cache(300)
def weekly_series(weeks=8, channel_id=None):
    """Тренды по последним N неделям: ERR, CV, регистрации, охват — для графика."""
    from datetime import timedelta
    end = week_bounds(date.today())[1]
    out = []
    for i in range(weeks - 1, -1, -1):
        e = end - timedelta(days=7 * i)
        s_, e_ = e - timedelta(days=6), e
        p = period_report(s_, e_, channel_id)
        out.append({"label": s_.strftime("%d.%m"), "end": e_.isoformat(),
                    "ERR": p["ind"].get("ERR"),
                    "CV": p["ind"].get("CV_reach"),
                    "regs": p["registrations"],
                    "reach": p["agg"].get("reach") or 0})
    return out


@ttl_cache(300)
def growth_points(start, end):
    """Точки роста: только из рассчитанных чисел. Возвращает [{kind, text}],
    kind: scale (масштабировать) | fix (чинить просадку) | insight (инсайт)."""
    out = []
    try:
        series = weekly_series(6)
        # сравниваем по последней ЗАВЕРШЁННОЙ неделе (текущая может быть неполной)
        if series and series[-1].get("end", "9999") >= date.today().isoformat():
            series = series[:-1]
        if len(series) >= 3:
            cur, prev = series[-1], series[:-1][-4:]
            def avg(key):
                vals = [p.get(key) for p in prev if p.get(key) is not None]
                return sum(vals) / len(vals) if vals else None
            for key, label in (("ERR", "ERR (вовлечённость)"), ("CV", "CV в регистрацию"),
                               ("regs", "регистрации")):
                a = avg(key)
                c = cur.get(key)
                # сравниваем CV/регистрации только если данные есть во всех неделях
                # (иначе в истории нули из-за отсутствия источника, а не из-за результата)
                if key in ("CV", "regs"):
                    if any((p.get("regs") or 0) == 0 for p in prev):
                        continue
                if a and c is not None:
                    pct = (c - a) / a * 100
                    if pct < -10:
                        out.append({"kind": "fix",
                                    "text": f"{label}: {c:.2f} против средней {a:.2f} за 4 недели ({pct:+.0f}%) — найти причину просадки."})
                    elif pct > 15:
                        out.append({"kind": "scale",
                                    "text": f"{label}: {c:.2f} против {a:.2f} ({pct:+.0f}%) — работает, усиливать."})
    except Exception:
        pass
    try:
        import utm as utm_mod
        br = utm_mod.breakdown(start, end)
        mediums = [(m, v) for m, v in br["by_medium"].items() if v["regs"] > 0 and m != "прочее"]
        if mediums:
            best_m, best_v = max(mediums, key=lambda x: x[1]["regs"])
            out.append({"kind": "scale",
                        "text": f"Лучшее размещение по регистрациям — {best_m}: {int(best_v['regs'])} рег., {int(best_v['orders'])} заказов. Масштабировать."})
        funnels = [(f, v) for f, v in br["by_funnel"].items()
                   if f != "Прочее" and v["regs"] > 0]
        funnels = [(f, v) for f, v in funnels if v["regs"] >= 5 and 0 < v["orders"] < v["regs"]]
        if funnels:
            worst_f, worst_v = min(funnels, key=lambda x: x[1]["orders"] / x[1]["regs"])
            out.append({"kind": "fix",
                        "text": f"Воронка «{worst_f}»: {int(worst_v['regs'])} рег. → {int(worst_v['orders'])} заказов "
                                f"(CR {worst_v['orders'] / worst_v['regs'] * 100:.1f}%) — самая слабая связка, чинить CTA/страницу."})
    except Exception:
        pass
    return out[:6]


def month_forecast():
    """Предварительный итог месяца (по ТЗ): линейная экстраполяция по прошедшим дням.
    Статус ESTIMATED — это оценка, не факт. Возвращает None для неполных данных."""
    from datetime import timedelta as _td
    s_, e_ = month_bounds(date.today())
    if date.today() <= s_:
        return None
    import calendar as _cal
    elapsed = (date.today() - s_).days or 1
    total_days = _cal.monthrange(s_.year, s_.month)[1]
    agg, _ = aggregate(s_, date.today())
    regs = registrations_total(s_, date.today())
    from db import GcPayment
    pays = db.session.query(func.coalesce(func.sum(GcPayment.amount), 0)).filter(
        GcPayment.date >= s_, GcPayment.date <= date.today(),
        GcPayment.status == "accepted").scalar()
    k = total_days / elapsed
    out = {"month_start": s_.isoformat(), "month_end": e_.isoformat(),
           "elapsed_days": elapsed, "total_days": total_days, "status": "ESTIMATED"}
    for key, src in (("reach", agg.get("reach")), ("views", agg.get("views")),
                     ("regs", regs), ("payments", pays)):
        if src is not None:
            out[key] = src
            out[key + "_forecast"] = round(src * k)
    return out
