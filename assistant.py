# -*- coding: utf-8 -*-
"""AI SMM-специалист: чат-ассистент с доступом ко всем данным приложения.
Отвечает на вопросы SMMщика на основе реальных цифр — ничего не выдумывает."""
import json
from datetime import date, timedelta
from db import db, get_setting, Channel, MetricSnapshot, Registration, GcOrder, GcPayment
import calc
import ai_analyst
import utm as utm_mod


SUGGESTIONS = [
    "Какой канал работает хуже всех?",
    "Что публиковать завтра?",
    "Почему упали регистрации?",
    "Какая рубрика даёт больше всего регистраций?",
    "Что усилить на следующей неделе?",
    "Покажи топ-3 материала за месяц",
    "Какой формат контента самый эффективный?",
    "Как обстоят дела у конкурентов?",
    "Что думает аудитория в комментариях?",
    "Какая воронка конвертирует лучше?",
]


def _build_context():
    """Собирает сжатый контекст данных для ассистента."""
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    last_7 = (today - timedelta(days=7), today)
    last_30 = (today - timedelta(days=30), today)

    ctx = {}

    # KPI за 7 и 30 дней
    p7 = calc.period_report(*last_7)
    p30 = calc.period_report(*last_30)
    ctx["week"] = {k: v for k, v in p7.items() if not k.startswith("_") and k != "data_statuses"}
    ctx["month"] = {k: v for k, v in p30.items() if not k.startswith("_") and k != "data_statuses"}

    # По каналам (7 дней)
    channels = []
    for ch in Channel.query.filter_by(is_active=True, is_competitor=False).all():
        p = calc.period_report(*last_7, ch.id)
        channels.append({
            "name": ch.name, "platform": ch.platform,
            "reach": p["agg"].get("reach"),
            "err": p["ind"].get("ERR"),
            "regs": p["registrations"],
            "followers": p["agg"].get("followers_end"),
        })
    ctx["channels"] = channels

    # Конкуренты
    competitors = []
    for ch in Channel.query.filter_by(is_competitor=True, is_active=True).all():
        p = calc.period_report(*last_7, ch.id)
        competitors.append({
            "name": ch.name, "reach": p["agg"].get("reach"), "err": p["ind"].get("ERR"),
        })
    if competitors:
        ctx["competitors"] = competitors

    # Контент — топ и худшие
    items = calc.content_stats_for_period(*last_30)
    scored = []
    for i in items:
        tags = json.loads(i["item"].ai_tags or "{}")
        scored.append({
            "title": (i["item"].title or "")[:60],
            "format": i["item"].format,
            "rubric": tags.get("рубрика", ""),
            "reach": i.get("reach"),
            "err": round(i.get("ERR") or 0, 2),
            "regs": i.get("registrations") or 0,
        })
    scored.sort(key=lambda x: -(x["reach"] or 0))
    ctx["top_content"] = scored[:5]
    ctx["worst_content"] = scored[-3:] if len(scored) >= 5 else []

    # UTM / воронки
    ctx["utm_breakdown"] = {k: v for k, v in utm_mod.breakdown(*last_7).items() if k != "missing"}

    # Комментарии
    try:
        import comments as cm
        dig = cm.digest(*last_7)
        ctx["comments_summary"] = {
            "total": dig["total"],
            "top_pains": [c.text[:80] for c in dig["pains"][:3]],
            "top_questions": [c.text[:80] for c in dig["questions"][:3]],
        }
    except Exception:
        pass

    # Тренды рубрик
    try:
        import intel
        ctx["trends"] = intel.trend_radar(8)[:5]
    except Exception:
        pass

    return ctx


def _build_system_prompt():
    return (
        "Ты — опытный AI SMM-специалист. Тебе даны РЕАЛЬНЫЕ данные из аналитической системы. "
        "Правила:\n"
        "1. Используй ТОЛЬКО числа из предоставленных данных.\n"
        "2. Если данных нет — скажи «недостаточно данных».\n"
        "3. Давай КОНКРЕТНЫЕ рекомендации: что сделать, где, когда.\n"
        "4. Формат ответа: 2-5 предложений, по делу, без воды.\n"
        "5. Отвечай по-русски.\n"
        "6. Если вопрос про «что делать» — дай 3 конкретных действия (усилить/изменить/убрать)."
    )


def ask(question):
    """Ответ ассистента на вопрос пользователя."""
    if not question.strip():
        return {"answer": "Задайте вопрос."}

    ctx = _build_context()
    prompt = (
        f"Данные аналитики (за последние 7 и 30 дней):\n"
        f"{json.dumps(ctx, ensure_ascii=False, default=str)}\n\n"
        f"Вопрос SMM-специалиста: {question}"
    )

    answer = ai_analyst._call_llm(_build_system_prompt(), prompt)
    if not answer:
        # фолбэк — простые эвристики без LLM
        answer = _heuristic_answer(question, ctx)

    return {"answer": answer}


def _heuristic_answer(question, ctx):
    """Простой ответ без LLM — если API недоступен."""
    q = question.lower()
    chs = ctx.get("channels", [])

    if "хуже" in q or "слаб" in q or "плох" in q:
        if chs:
            worst = min(chs, key=lambda c: c.get("err") or 0)
            return (f"Хуже всех работает «{worst['name']}»: ERR {worst.get('err', 0):.2f}%. "
                    f"Рекомендую пересмотреть контент-стратегию этого канала.")

    if "лучш" in q or "топ" in q or "лучший" in q:
        if chs:
            best = max(chs, key=lambda c: c.get("reach") or 0)
            return (f"Лучший канал — «{best['name']}»: охват {best.get('reach', 0):,.0f}, "
                    f"ERR {best.get('err', 0):.2f}%. Усилить его — приоритет №1.")

    if "регистрац" in q or "рег" in q:
        regs7 = ctx.get("week", {}).get("registrations", 0)
        return f"За 7 дней: {regs7} регистраций. Подробный разрез — на экране «Регистрации»."

    if "что публиковать" in q or "завтра" in q or "контент" in q:
        trends = ctx.get("trends", [])
        growing = [t for t in trends if "растёт" in t.get("status", "")]
        if growing:
            return (f"Растущие рубрики: {', '.join(t['rubric'] for t in growing[:3])}. "
                    f"Рекомендую сфокусироваться на них. Полный план — на экране AI-аналитики.")

    return ("Не смог обработать запрос без AI. Вот ключевые цифры за неделю: "
            f"охват {ctx.get('week', {}).get('agg', {}).get('reach', 0):,.0f}, "
            f"регистрации {ctx.get('week', {}).get('registrations', 0)}. "
            f"Попробуйте переформулировать вопрос.")
