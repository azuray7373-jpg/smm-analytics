# -*- coding: utf-8 -*-
"""Smart Insights: авто-рекомендации на основе всех данных.
Комбинирует тренды, heatmap, комментарии, конкурентов, ROI в конкретные действия."""
import json
from datetime import date, timedelta
from collections import defaultdict
from db import db, Channel, MetricSnapshot, Registration, GcOrder, GcPayment, ContentItem, ContentStat, Spend
from sqlalchemy import func
import calc
import utm as utm_mod


def generate_insights():
    """Генерирует 5-7 умных рекомендаций на основе всех данных приложения."""
    today = date.today()
    week_start = today - timedelta(days=6)
    prev_start = week_start - timedelta(days=7)
    insights = []

    # 1. Лучший день для публикаций
    try:
        items = calc.content_stats_for_period(today - timedelta(days=30), today)
        day_stats = defaultdict(lambda: {"reach": 0, "n": 0})
        for i in items:
            if i["item"].published_at:
                wd = i["item"].published_at.weekday()
                day_stats[wd]["reach"] += i.get("reach") or 0
                day_stats[wd]["n"] += 1
        days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        best_day = max(day_stats.items(), key=lambda x: x[1]["reach"] / max(x[1]["n"], 1))
        if best_day[1]["n"] >= 2:
            avg = best_day[1]["reach"] / best_day[1]["n"]
            insights.append({
                "type": "schedule", "priority": "high",
                "title": f"Лучший день: {days_ru[best_day[0]]}",
                "text": f"Средний охват {avg:,.0f} — планируйте важные публикации на этот день".replace(",", " "),
            })
    except Exception:
        pass

    # 2. Растущая рубрика
    try:
        import intel
        trends = intel.trend_radar(8)
        growing = [t for t in trends if "растёт" in t["status"]]
        if growing:
            g = growing[0]
            insights.append({
                "type": "content", "priority": "high",
                "title": f"Растёт: «{g['rubric']}»",
                "text": f"Средний охват {g['recent_avg']:,.0f}, тренд {g.get('trend_pct', '?')}% — сделайте ещё 2-3 материала в этой рубрике".replace(",", " "),
            })
        burning = [t for t in trends if "выгора" in t["status"]]
        if burning:
            b = burning[0]
            insights.append({
                "type": "content", "priority": "medium",
                "title": f"Выгорает: «{b['rubric']}»",
                "text": f"Тренд {b.get('trend_pct', '?')}% — смените подачу или пауза".replace(",", " "),
            })
    except Exception:
        pass

    # 3. Канал-лидер и аутсайдер
    try:
        channels = []
        for ch in Channel.query.filter_by(is_active=True, is_competitor=False).all():
            p = calc.period_report(week_start, today, ch.id)
            channels.append({
                "name": ch.name,
                "reach": p["agg"].get("reach") or 0,
                "err": p["ind"].get("ERR") or 0,
                "regs": p["registrations"],
            })
        if channels:
            best = max(channels, key=lambda c: c["reach"])
            worst = min([c for c in channels if c["reach"] > 1000], key=lambda c: c["err"])
            insights.append({
                "type": "channel", "priority": "high",
                "title": f"Лидер: {best['name']}",
                "text": f"Охват {best['reach']:,.0f}, ERR {best['err']:.2f}% — увеличьте частоту публикаций на 20-30%".replace(",", " "),
            })
            insights.append({
                "type": "channel", "priority": "medium",
                "title": f"Требует внимания: {worst['name']}",
                "text": f"ERR {worst['err']:.2f}% — пересмотрите контент-стратегию или формат",
            })
    except Exception:
        pass

    # 4. UTM инсайт
    try:
        br = utm_mod.breakdown(week_start, today)
        best_medium = max(
            ((m, v) for m, v in br.get("by_medium", {}).items() if v["regs"] > 0),
            key=lambda x: x[1]["regs"], default=None
        )
        if best_medium:
            m, v = best_medium
            insights.append({
                "type": "utm", "priority": "medium",
                "title": f"Лучшее размещение: {m}",
                "text": f"{v['regs']:.0f} регистраций, {v.get('orders', 0):.0f} заказов — используйте это размещение чаще",
            })
    except Exception:
        pass

    # 5. Комментарии — боли
    try:
        import comments as cm
        dig = cm.digest(week_start, today)
        if dig["pains"]:
            top_pain = dig["pains"][0]
            insights.append({
                "type": "audience", "priority": "high",
                "title": "Закройте боль аудитории",
                "text": f"«{top_pain.text[:80]}» — сделайте контент, отвечающий на этот вопрос",
            })
        if dig["ideas"]:
            idea = dig["ideas"][0]
            insights.append({
                "type": "audience", "priority": "medium",
                "title": "Идея от подписчиков",
                "text": f"«{idea.text[:80]}» — аудитория сама просит этот контент",
            })
    except Exception:
        pass

    # 6. ROI предупреждение
    try:
        pays = utm_mod.payments_by_platform(week_start, today)
        spend_rows = db.session.query(Channel.platform, func.coalesce(func.sum(Spend.amount), 0)).join(
            Spend, Spend.channel_id == Channel.id
        ).filter(Spend.date >= week_start, Spend.date <= today).group_by(Channel.platform).all()
        for plat, spent in spend_rows:
            if spent > 0:
                earned = pays.get(plat, 0)
                roi = (earned - spent) / spent * 100
                if roi < 0:
                    insights.append({
                        "type": "money", "priority": "high",
                        "title": f"ROI отрицательный: {plat}",
                        "text": f"Потрачено {spent:,.0f} ₽, получено {earned:,.0f} ₽ — пересмотрите стратегию".replace(",", " "),
                    })
    except Exception:
        pass

    # Сортировка по приоритету
    priority_order = {"high": 0, "medium": 1, "low": 2}
    insights.sort(key=lambda x: priority_order.get(x["priority"], 3))

    return insights[:7]
