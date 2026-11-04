# -*- coding: utf-8 -*-
"""Аудит качества аудитории (по мотивам HypeAuditor AQS и боли из ОС:
«подозреваем накрутку вовлечённости»).

Скор 0–100 по 4 категориям на основе НАШИХ реальных данных:
1. Вовлечённость vs нормы платформы (ERR против рыночных бенчмарков).
2. Аномалии роста подписчиков (всплески прироста, не подтверждённые охватом).
3. «Ровность» вовлечённости (слишком одинаковый ERR день ото дня —
   классический почерк накрутки; живая аудитория даёт разброс).
4. Реальность охвата (reach/followers: накрученные базы почти не смотрят).

Точных «процентов ботов» не обещаем (это proprietary у HypeAuditor) —
даём прозрачные флаги с цифрами, которые можно проверить руками.
"""
from datetime import date, timedelta
from statistics import pstdev, mean

import calc
from db import Channel, MetricSnapshot

# Рыночные нормы ERR по платформам (медианы из открытых бенчмарков 2025-2026)
PLATFORM_ERR_NORMS = {
    "instagram": 1.8, "tiktok": 4.5, "youtube": 2.0, "telegram": 3.0,
    "vk": 2.0, "dzen": 2.5, "max": 2.0,
}
# Здоровый разброс дневного ERR (коэффициент вариации), ниже — подозрительно
CV_FLOOR = 0.10


def _day_series(channel_id, metric, days=60):
    """Значения метрики по дням (последние N дней), только не-NULL."""
    start = date.today() - timedelta(days=days)
    snap = calc.latest_snapshots(start, date.today(), channel_id)
    return [(d, v) for (cid, d, met), (v, st) in sorted(snap.items())
            if met == metric and v is not None and v > 0]


def channel_quality(ch, days=60):
    """Аудит одного канала. Возвращает скор, категории и флаги."""
    start = date.today() - timedelta(days=days)
    p = calc.period_report(start, date.today(), ch.id)
    agg, ind = p["agg"], p["ind"]
    followers = agg.get("followers_end") or 0
    reach = agg.get("reach") or 0
    err = ind.get("ERR")
    flags, cats = [], {}

    # 1. Вовлечённость vs норма платформы (0-30)
    norm = PLATFORM_ERR_NORMS.get(ch.platform, 2.0)
    if err is None or not reach:
        cats["engagement"] = {"score": 15, "label": "мало данных", "val": None}
    else:
        ratio = err / norm
        s = min(30, int(ratio * 20))  # err=норма → 20, err=1.5×нормы → 30
        if ratio < 0.35:
            flags.append(f"ERR {err:.2f}% при норме {ch.platform} ~{norm}% — "
                         f"аудитория почти не реагирует (в {1/ratio:.1f} раза ниже нормы)")
        cats["engagement"] = {"score": max(0, s), "val": err, "norm": norm,
                              "label": f"ERR {err:.2f}% vs норма {norm}%"}

    # 2. Аномалии прироста подписчиков (0-25): всплески подписок без охвата
    subs = _day_series(ch.id, "subscribed", days)
    spike_flags = []
    if len(subs) >= 7:
        vals = [v for _, v in subs]
        avg_s = mean(vals) if vals else 0
        spikes = [v for v in vals if avg_s > 0 and v > 6 * avg_s]
        day_reach = {d: v for d, v in _day_series(ch.id, "reach", days)}
        unbacked = []
        for d, v in subs:
            if avg_s > 0 and v > 6 * avg_s and day_reach.get(d, 0) < v * 20:
                unbacked.append((d, v))
        s = 25 if not spikes else max(5, 25 - 10 * len(spikes))
        if spikes:
            spike_flags.append(f"подписки скачут: {len(spikes)} дней с аномальным "
                               f"приростом (>6× среднего {avg_s:.0f})")
        if unbacked:
            spike_flags.append(f"{len(unbacked)} дней: большие подписки БЕЗ охвата "
                               f"(подписались, но не смотрят) — типичный след накрутки")
        cats["growth"] = {"score": s, "label": f"подписки/день в среднем {avg_s:.0f}"}
    else:
        cats["growth"] = {"score": 13, "label": "мало данных о приросте"}
    flags.extend(spike_flags)

    # 3. «Ровность» вовлечённости (0-20): живая аудитория = разброс
    inter_days, reach_days = {}, {}
    for met, store in (("likes", None), ("comments", None)):
        pass
    snap = calc.latest_snapshots(start, date.today(), ch.id)
    for (cid, d, met), (v, st) in snap.items():
        if v is None or v < 0:
            continue
        if met in ("likes", "comments", "saves", "shares", "reactions"):
            inter_days[d] = inter_days.get(d, 0) + v
        if met == "reach":
            reach_days[d] = v
    day_errs = [inter_days[d] / reach_days[d] * 100
                for d in inter_days if reach_days.get(d, 0) > 200]
    if len(day_errs) >= 7:
        cv = (pstdev(day_errs) / mean(day_errs)) if mean(day_errs) else 0
        s = 20 if cv >= CV_FLOOR * 2 else (12 if cv >= CV_FLOOR else 5)
        if cv < CV_FLOOR:
            flags.append(f"вовлечённость подозрительно ровная (разброс {cv*100:.0f}% < 10%) — "
                         "живая аудитория так не ведёт себя, похоже на автоматические лайки")
        cats["consistency"] = {"score": s, "val": round(cv, 3),
                               "label": f"разброс дневного ERR: {cv*100:.0f}%"}
    else:
        cats["consistency"] = {"score": 10, "label": "мало дней с вовлечённостью"}

    # 4. Реальность охвата (0-25): reach/followers за период
    if followers > 0 and reach > 0:
        rpf = reach / followers  # за 60 дней здоровый охват 1.5-10× базы
        if rpf >= 1.5:
            s = 25
        elif rpf >= 0.8:
            s = 18
        elif rpf >= 0.3:
            s = 10
        else:
            s = 3
            flags.append(f"за 60 дней охват всего {rpf:.2f}× базы ({reach:,.0f} при "
                         f"{followers:,.0f} подписчиков) — база большая, но её "
                         "почти не показывают: много «мёртвых» подписчиков".replace(",", " "))
        cats["reachability"] = {"score": s, "val": round(rpf, 2),
                                "label": f"охват/база за 60 дней: {rpf:.2f}×"}
    else:
        cats["reachability"] = {"score": 12, "label": "нет данных охвата/базы"}

    total = sum(c["score"] for c in cats.values())
    if total >= 75:
        verdict = "здоровая аудитория"
    elif total >= 50:
        verdict = "есть признаки неактивной части базы"
    elif total >= 30:
        verdict = "существенные признаки накрутки/неактивной базы"
    else:
        verdict = "сильные признаки накрутки — аудитория преимущественно неактивна"
    return {"channel": ch, "score": total, "verdict": verdict,
            "cats": cats, "flags": flags,
            "followers": followers, "reach": reach, "err": err}


def audit_all(days=60):
    """Аудит всех активных каналов (не конкурентов), сортировка по скору."""
    out = []
    for ch in Channel.query.filter_by(is_active=True, is_competitor=False).all():
        try:
            out.append(channel_quality(ch, days))
        except Exception as e:
            out.append({"channel": ch, "score": None, "verdict": f"ошибка расчёта: {e}",
                        "cats": {}, "flags": [], "followers": None, "reach": None, "err": None})
    out.sort(key=lambda x: (x["score"] if x["score"] is not None else -1))
    return out
