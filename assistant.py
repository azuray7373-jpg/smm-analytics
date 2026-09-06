# -*- coding: utf-8 -*-
"""AI SMM-специалист: трёхуровневый ответ — кэш LLM → прямой LLM → умный фолбэк."""
import json
from datetime import date, timedelta
from db import db, get_setting, set_setting, Channel
import calc
import ai_analyst

# ─── Кэш LLM-ответов (обновляется AI-релеем 4 раза в сутки) ───
_LLM_CACHE = {}


def get_cached_answer(question):
    if not _LLM_CACHE:
        _load_cache()
    q = question.lower().strip()
    for key, ans in _LLM_CACHE.items():
        if key in q or q in key:
            return ans
    return None


def _load_cache():
    raw = get_setting("ai_answer_cache", "")
    if raw:
        try:
            for item in json.loads(raw):
                _LLM_CACHE[item["q"]] = item["a"]
        except Exception:
            pass


SUGGESTIONS = [
    "Какой канал работает хуже всех?",
    "Что публиковать завтра?",
    "Почему упали регистрации?",
    "Какая рубрика даёт больше регистраций?",
    "Топ-3 материала за месяц",
    "Какой формат контента эффективнее?",
    "Что думает аудитория в комментариях?",
    "Какая воронка конвертирует лучше?",
]


def ask(question):
    """Кэш LLM → прямой LLM → умный фолбэк из данных."""
    if not question.strip():
        return {"answer": "Задайте вопрос."}

    cached = get_cached_answer(question)
    if cached:
        return {"answer": cached, "source": "llm_cache"}

    ctx = _build_context()
    prompt = "Данные: " + json.dumps(ctx, ensure_ascii=False, default=str) + " Вопрос: " + question
    answer = ai_analyst._call_llm(_system_prompt(), prompt)
    if answer:
        return {"answer": answer, "source": "llm_direct"}

    answer = _smart_answer(question, ctx)
    return {"answer": answer, "source": "smart_fallback"}


def _system_prompt():
    return (
        "Ты AI SMM-специалист. Используй ТОЛЬКО числа из данных. "
        "Нет данных — «недостаточно данных». Конкретные рекомендации. "
        "3-7 предложений. Отвечай по-русски.")


def _build_context():
    today = date.today()
    w_s = today - timedelta(days=6)
    m_s = today - timedelta(days=29)
    p7 = calc.period_report(w_s, today)
    p30 = calc.period_report(m_s, today)
    channels = []
    for ch in Channel.query.filter_by(is_active=True, is_competitor=False).all():
        p = calc.period_report(w_s, today, ch.id)
        channels.append({"name": ch.name, "platform": ch.platform,
                         "reach": p["agg"].get("reach"), "err": p["ind"].get("ERR"),
                         "regs": p["registrations"]})
    ctx = {
        "week": {"reach": p7["agg"].get("reach"), "regs": p7["registrations"],
                 "err": p7["ind"].get("ERR"), "cv": p7["ind"].get("CV_reach")},
        "month": {"reach": p30["agg"].get("reach"), "regs": p30["registrations"]},
        "channels": channels,
    }
    try:
        import utm as utm_mod
        br = utm_mod.breakdown(w_s, today)
        ctx["utm"] = {k: v for k, v in br.items() if k != "missing"}
    except Exception:
        pass
    try:
        import comments as cm
        dig = cm.digest(w_s, today)
        ctx["comments"] = {"total": dig["total"],
                           "pains": [c.text[:80] for c in dig["pains"][:3]],
                           "questions": [c.text[:80] for c in dig["questions"][:3]]}
    except Exception:
        pass
    try:
        import intel
        ctx["trends"] = intel.trend_radar(8)[:5]
    except Exception:
        pass
    return ctx


def _smart_answer(question, ctx):
    """Умный ответ без LLM — конкретные данные."""
    q = question.lower()
    chs = ctx.get("channels", [])
    L = []
    fmt = lambda n: "{:,.0f}".format(n).replace(",", " ")

    if any(w in q for w in ("хуже", "слаб", "плох", "worst")):
        by_err = sorted(chs, key=lambda c: c.get("err") or 0)
        if by_err:
            w, b = by_err[0], by_err[-1]
            L.append("Худший: «{}» (ERR {:.2f}%).".format(w["name"], w.get("err", 0)))
            L.append("Лучший: «{}» (ERR {:.2f}%).".format(b["name"], b.get("err", 0)))
            L.append("→ Усилить «{}», пересмотреть «{}».".format(b["name"], w["name"]))
    elif any(w in q for w in ("лучш", "топ", "best")):
        by_r = sorted(chs, key=lambda c: c.get("reach") or 0, reverse=True)
        L.append("Топ-3 по охвату:")
        for i, c in enumerate(by_r[:3], 1):
            L.append("  {}. {} — {}, ERR {:.2f}%".format(i, c["name"], fmt(c.get("reach", 0)), c.get("err", 0)))
    elif "регистрац" in q or "рег" in q:
        regs = ctx.get("week", {}).get("regs", 0)
        L.append("Регистрации за неделю: {}".format(fmt(regs)))
        for combo, v in (ctx.get("utm", {}).get("by_combo", []) or [])[:3]:
            L.append("  • {} × {}: {} рег.".format(combo[0], combo[1], fmt(v["regs"])))
    elif any(w in q for w in ("публиковать", "контент", "завтра", "план")):
        for t in (ctx.get("trends") or [])[:5]:
            L.append("  {} {} — ср. {}".format(t["status"], t["rubric"], fmt(t.get("recent_avg", 0))))
    elif "охват" in q:
        r = ctx.get("week", {}).get("reach", 0)
        L.append("Охват 7 дней: {}".format(fmt(r)))
        if chs:
            best = max(chs, key=lambda c: c.get("reach") or 0)
            L.append("Лидер: «{}» — {}".format(best["name"], fmt(best.get("reach", 0))))
    elif any(w in q for w in ("деньг", "продаж", "оплат", "заказ")):
        L.append("Воронка и деньги — на экране «Продажи».")
        L.append("ROI каналов — на экране «Расходы».")
    elif "конкурент" in q:
        L.append("Конкуренты — на экране «Конкуренты».")
    elif any(w in q for w in ("коммент", "болев", "аудитор")):
        cs = ctx.get("comments", {})
        L.append("Комментариев: {}".format(cs.get("total", 0)))
        for p in cs.get("pains", [])[:2]:
            L.append("  💔 {}".format(p))
    else:
        r = ctx.get("week", {}).get("reach", 0)
        g = ctx.get("week", {}).get("regs", 0)
        e = ctx.get("week", {}).get("err", 0)
        L.append("Охват {} | Рег {} | ERR {:.2f}%".format(fmt(r), fmt(g), e or 0))
        L.append("Спросите: «какой канал хуже», «что публиковать», «топ контент»...")

    return chr(10).join(L) if L else "Задайте вопрос."
