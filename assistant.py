# -*- coding: utf-8 -*-
"""AI SMM-специалист: быстрый ответ из кэшированных данных."""
import json
from datetime import date, timedelta
from db import get_setting
import calc

_LLM_CACHE = {}

SUGGESTIONS = [
    "Какой канал работает хуже всех?",
    "Что публиковать завтра?",
    "Сколько регистраций?",
    "Топ контента",
    "Какой формат эффективнее?",
    "Что думает аудитория?",
]


def get_cached_answer(question):
    if not _LLM_CACHE:
        raw = get_setting("ai_answer_cache", "")
        if raw:
            try:
                for item in json.loads(raw):
                    _LLM_CACHE[item["q"]] = item["a"]
            except Exception:
                pass
    q = question.lower().strip()
    for key, ans in _LLM_CACHE.items():
        if key in q or q in key:
            return ans
    return None


def ask(question):
    """Быстрый ответ — максимум 1 запрос к БД."""
    if not question.strip():
        return {"answer": "Задайте вопрос."}
    try:
        cached = get_cached_answer(question)
        if cached:
            return {"answer": cached, "source": "llm_cache"}
    except Exception:
        pass
    try:
        today = date.today()
        w_s = today - timedelta(days=6)
        p = calc.period_report(w_s, today)
        w = {
            "reach": p["agg"].get("reach"),
            "views": p["agg"].get("views"),
            "regs": p["registrations"],
            "err": p["ind"].get("ERR"),
            "cv": p["ind"].get("CV_reach"),
            "inter": p["ind"].get("interactions"),
            "followers": p["agg"].get("followers_end"),
        }
    except Exception:
        w = {}
    return {"answer": _smart_answer(question, w), "source": "smart"}


def _smart_answer(question, w):
    q = question.lower()
    L = []
    fmt = lambda n: "{:,.0f}".format(n).replace(",", " ") if n else "0"

    if any(x in q for x in ("хуже", "слаб", "плох", "worst", "канал")):
        L.append("Все каналы: экран «Каналы» — 11 аккаунтов с ERR, ER, CV.")
        if w.get("err"):
            L.append("Общий ERR: {:.2f}%".format(w["err"]))
        L.append("Лидер/аутсайдер: экран «Интеллект» → Team Brief")

    elif any(x in q for x in ("регистрац", "рег", "reg")):
        L.append("Регистрации за неделю: {}".format(fmt(w.get("regs"))))
        L.append("По UTM: экран «Регистрации» → «Наши метки»")

    elif any(x in q for x in ("публиковать", "контент", "завтра", "план")):
        L.append("AI-план: экран «AI-аналитика» → «Сгенерировать план недели»")
        L.append("Тренды: экран «Интеллект» → Trend Radar")

    elif any(x in q for x in ("охват", "reach")):
        L.append("Охват за неделю: {}".format(fmt(w.get("reach"))))

    elif any(x in q for x in ("подписчик", "followers", "аудитор")):
        L.append("Подписчиков: {}".format(fmt(w.get("followers"))))

    elif any(x in q for x in ("деньг", "продаж", "оплат", "заказ", "roi")):
        L.append("Воронка: экран «Продажи»")
        L.append("ROI: экран «ROI»")

    else:
        L.append("Охват: {} | Рег: {} | ERR: {:.2f}% | Подписчики: {}".format(
            fmt(w.get("reach")), fmt(w.get("regs")), w.get("err") or 0, fmt(w.get("followers"))))
        L.append("Спросите: «какой канал хуже», «что публиковать», «сколько регистраций»")

    return chr(10).join(L) if L else "Задайте вопрос."
