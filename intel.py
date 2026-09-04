# -*- coding: utf-8 -*-
"""Интеллектуальный слой: Performance Score, Predictor, Smart Digest, Trend Radar,
Weekly Team Brief. Все числа — только из рассчитанных данных (calc.py)."""
import json
from datetime import date, timedelta
from collections import defaultdict
from sqlalchemy import func
from db import db, ContentItem, ContentStat, Channel, MetricSnapshot, Registration, GcPayment
import calc


# ═════════════════════════════════════════════════
# 1. CONTENT PERFORMANCE SCORE (0–100)
# ═════════════════════════════════════════════════

def performance_score(item_stats):
    """Балл 0–100 для одного материала на основе его статистики.
    Взвешенная сумма нормированных метрик против медианы канала."""
    if not item_stats or not item_stats.get("reach"):
        return 0
    reach = item_stats["reach"]
    err = item_stats.get("ERR") or 0
    regs = item_stats.get("registrations") or 0
    saves = item_stats.get("saves") or 0
    shares = item_stats.get("shares") or 0
    # нормируем каждую метрику (0–25 каждая)
    s = min(reach / 10000 * 10, 25)        # охват: 10k = максимум
    s += min(err * 3, 25)                   # ERR: 8%+ = максимум
    s += min(regs / 50 * 20, 20)            # регистрации: 50 = максимум
    s += min(saves / 100 * 15, 15)          # сохранения: 100 = максимум
    s += min(shares / 50 * 15, 15)          # репосты: 50 = максимум
    return round(min(s, 100))


def score_all_content(days=30):
    """Пересчитать баллы для всех материалов за период."""
    items = calc.content_stats_for_period(date.today() - timedelta(days=days), date.today())
    scored = []
    for i in items:
        score = performance_score(i)
        tags = json.loads(i["item"].ai_tags or "{}")
        scored.append({
            "item": i["item"],
            "score": score,
            "reach": i.get("reach") or 0,
            "err": i.get("ERR"),
            "regs": i.get("registrations") or 0,
            "saves": i.get("saves") or 0,
            "rubric": tags.get("рубрика", "—"),
            "format": i["item"].format or "—",
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ═════════════════════════════════════════════════
# 2. AI PERFORMANCE PREDICTOR
# ═════════════════════════════════════════════════

def predict_performance(title, text, format_, duration=None):
    """Предсказание ожидаемого охвата и ERR на основе исторических данных
    похожих материалов. Возвращает словарь с прогнозом и уверенностью."""
    from db import Channel
    # собираем статистику по форматам за 60 дней
    items = calc.content_stats_for_period(date.today() - timedelta(days=60), date.today())
    # группируем по формату
    by_format = defaultdict(lambda: {"reach": [], "err": [], "n": 0})
    for i in items:
        fmt = i["item"].format or "other"
        by_format[fmt]["reach"].append(i.get("reach") or 0)
        by_format[fmt]["err"].append(i.get("ERR") or 0)
        by_format[fmt]["n"] += 1

    fmt_stats = by_format.get(format_, by_format.get("other", {"reach": [0], "err": [0], "n": 0}))
    if fmt_stats["n"] < 3:
        return {"confidence": "low", "reason": "мало данных по формату",
                "predicted_reach": None, "predicted_err": None}

    # медиана и разброс
    reach_sorted = sorted(fmt_stats["reach"])
    err_sorted = sorted([e for e in fmt_stats["err"] if e > 0])
    med_reach = reach_sorted[len(reach_sorted)//2] if reach_sorted else 0
    med_err = err_sorted[len(err_sorted)//2] if err_sorted else 0

    # корректировка по длительности (для видео)
    duration_factor = 1.0
    if duration and duration > 60:
        duration_factor = 0.85  # длинные видео — ниже охват
    elif duration and 15 <= duration <= 45:
        duration_factor = 1.15  # короткие — выше

    predicted_reach = round(med_reach * duration_factor)
    predicted_err = round(med_err, 2)
    confidence = "high" if fmt_stats["n"] >= 10 else "medium"

    return {
        "confidence": confidence,
        "predicted_reach": predicted_reach,
        "predicted_err": predicted_err,
        "sample_size": fmt_stats["n"],
        "format": format_,
        "reason": f"на основе {fmt_stats['n']} материалов формата «{format_}» за 60 дней"
    }


# ═════════════════════════════════════════════════
# 3. SMART DAILY DIGEST
# ═════════════════════════════════════════════════

def smart_digest(target_date=None):
    """«Что изменилось» обычным языком — только из чисел."""
    target_date = target_date or (date.today() - timedelta(days=1))
    prev_date = target_date - timedelta(days=1)
    insights = []

    # сравниваем с предыдущим днём и средним за 7 дней
    for ch in Channel.query.filter_by(is_active=True, is_competitor=False).all():
        cur_agg, _ = calc.aggregate(target_date, target_date)
        prev_agg, _ = calc.aggregate(prev_date, prev_date)
        week_agg, _ = calc.aggregate(target_date - timedelta(days=7), prev_date)
        # по каналу
        cur_ch = _channel_day_stats(ch.id, target_date)
        prev_ch = _channel_day_stats(ch.id, prev_date)
        if cur_ch["reach"] and prev_ch["reach"]:
            pct = (cur_ch["reach"] - prev_ch["reach"]) / prev_ch["reach"] * 100
            if abs(pct) >= 30:
                direction = "вырос" if pct > 0 else "упал"
                insights.append(f"📡 {ch.name}: охват {direction} на {abs(pct):.0f}% "
                                f"({prev_ch['reach']:,.0f} → {cur_ch['reach']:,.0f})".replace(",", " "))

    # лучший материал вчера
    items = calc.content_stats_for_period(target_date, target_date)
    if items:
        best = max(items, key=lambda x: x.get("reach") or 0)
        title = best["item"].title or best["item"].link or "материал"
        tags = json.loads(best["item"].ai_tags or "{}")
        insights.append(f"🏆 Лучший материал: «{title[:60]}» — охват "
                        f"{best['reach']:,.0f}, ERR {best.get('ERR', 0):.1f}%".replace(",", " "))

    # регистрации
    regs_today = calc.registrations_total(target_date, target_date)
    regs_prev = calc.registrations_total(prev_date, prev_date)
    if regs_prev and abs(regs_today - regs_prev) / regs_prev >= 0.2:
        pct = (regs_today - regs_prev) / regs_prev * 100
        insights.append(f"📝 Регистрации: {pct:+.0f}% ({regs_prev:.0f} → {regs_today:.0f})")

    return insights


def _channel_day_stats(channel_id, d):
    from db import MetricSnapshot
    snap = MetricSnapshot.query.filter_by(channel_id=channel_id, metric="reach", date=d) \
        .order_by(MetricSnapshot.fetched_at.desc()).first()
    return {"reach": snap.value if snap else 0}


# ═════════════════════════════════════════════════
# 4. TREND RADAR
# ═════════════════════════════════════════════════

def trend_radar(weeks=8):
    """Жизненный цикл тем: растёт / стабильно / выгорает."""
    items_by_week = {}
    for w in range(weeks):
        end = date.today() - timedelta(days=w * 7)
        start = end - timedelta(days=6)
        items = calc.content_stats_for_period(start, end)
        rubric_stats = defaultdict(lambda: {"reach": 0, "n": 0, "err": []})
        for i in items:
            tags = json.loads(i["item"].ai_tags or "{}")
            rub = tags.get("рубрика")
            if rub:
                rubric_stats[rub]["reach"] += i.get("reach") or 0
                rubric_stats[rub]["n"] += 1
                if i.get("ERR"):
                    rubric_stats[rub]["err"].append(i["ERR"])
        for rub, s in rubric_stats.items():
            if s["n"] > 0:
                items_by_week.setdefault(rub, []).append({
                    "week": start.strftime("%d.%m"),
                    "avg_reach": s["reach"] / s["n"],
                    "avg_err": sum(s["err"]) / len(s["err"]) if s["err"] else 0,
                    "n": s["n"],
                })

    trends = []
    for rub, weeks_data in items_by_week.items():
        if len(weeks_data) < 3:
            continue
        # берём последние 4 недели
        recent = weeks_data[:4]
        old = weeks_data[4:] if len(weeks_data) > 4 else []
        recent_avg = sum(w["avg_reach"] for w in recent) / len(recent)
        old_avg = sum(w["avg_reach"] for w in old) / len(old) if old else recent_avg
        if old_avg:
            pct = (recent_avg - old_avg) / old_avg * 100
            if pct > 20:
                status = "растёт 📈"
            elif pct < -20:
                status = "выгорает 📉"
            else:
                status = "стабильно ➡️"
        else:
            status = "новая ✨"
        trends.append({
            "rubric": rub,
            "status": status,
            "recent_avg": round(recent_avg),
            "trend_pct": round(pct) if old_avg else None,
            "materials": sum(w["n"] for w in recent),
        })
    trends.sort(key=lambda x: -(x["recent_avg"] or 0))
    return trends


# ═════════════════════════════════════════════════
# 5. WEEKLY TEAM BRIEF
# ═════════════════════════════════════════════════

def team_brief():
    """Одностраничник для планёрки: победы, проблемы, действия."""
    end = date.today()
    start = end - timedelta(days=6)
    p = calc.period_report(start, end)
    scored = score_all_content(7)
    trends = trend_radar(8)
    import utm as utm_mod
    br = utm_mod.breakdown(start, end)

    brief = {"period": f"{start} — {end}", "wins": [], "problems": [], "actions": []}

    # Победы
    for item in scored[:3]:
        if item["score"] >= 40:
            brief["wins"].append(
                f"«{item['item'].title or 'материал'}» — балл {item['score']}/100, "
                f"охват {item['reach']:,.0f}".replace(",", " "))
    rd = p["deltas"].get("reach")
    if rd and rd.get("d") and rd["d"] > 10:
        brief["wins"].append(f"Общий охват вырос на {rd['d']:.0f}% к прошлой неделе")
    best_medium = max(((m, v) for m, v in br["by_medium"].items() if v["regs"] > 0),
                      key=lambda x: x[1]["regs"], default=None)
    if best_medium:
        brief["wins"].append(f"Лучшее размещение: {best_medium[0]} ({best_medium[1]['regs']:.0f} рег.)")

    # Проблемы
    for t in trends:
        if "выгорает" in t["status"]:
            brief["problems"].append(f"Рубрика «{t['rubric']}» выгорает ({t['trend_pct'] or '?'}% за месяц)")
    if rd and rd.get("d") and rd["d"] < -15:
        brief["problems"].append(f"Общий охват упал на {abs(rd['d']):.0f}% — искать причину")
    for item in scored[-3:]:
        if item["score"] < 15 and item["reach"] > 0:
            brief["problems"].append(
                f"Слабый материал: «{(item['item'].title or '?')[:40]}» — балл {item['score']}/100")

    # Действия
    for t in trends[:3]:
        if "растёт" in t["status"]:
            brief["actions"].append(f"Усилить рубрику «{t['rubric']}» — растёт")
        elif "выгорает" in t["status"]:
            brief["actions"].append(f"Обновить подачу рубрики «{t['rubric']}» или заменить")
    from ai_analyst import generate_content_plan
    brief["actions"].append("Сгенерировать AI-контент-план на следующую неделю")

    return brief
