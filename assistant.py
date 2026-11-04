# -*- coding: utf-8 -*-
"""AI SMM-специалист: отвечает строго на заданный вопрос.

Три уровня:
1. Социальные реплики (приветствие/благодарность/кто ты) — коротко, по делу.
2. LLM (GLM/Gemini, если задан ключ) — быстрый таймаут, строгий системный промпт.
3. Точный rule-движок по живым данным: период из вопроса, конкретный канал
   из вопроса, только наши UTM, воронка GC, цели, конкуренты, контент.

Источники данных: все таблицы приложения (LiveDune, YouTube API, TikTok/Дзен
импорт, GetCourse, комментарии, цели) — тот же слой, что и экраны.
"""
import json
import re
from datetime import date, timedelta

from db import db, get_setting, Channel
import calc

_cache = {"key": None, "data": None, "ts": 0}

RU_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5,
             "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
             "ноябр": 11, "декабр": 12}
MONTH_NAMES = ["январь", "февраль", "март", "апрель", "май", "июнь",
               "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]

CHANNEL_HINTS = [
    (("тикток", "tiktok"), "tiktok"),
    (("дзен", "дзэн", "dzen"), "dzen"),
    (("телеграм", "telegram", "тг "), "telegram"),
    (("инстаграм", "инста", "instagram"), "instagram"),
    (("вконтакте", " вк ", "vk"), "vk"),
    (("макс", "max"), "max"),
    (("ютуб", "youtube"), "youtube"),
    (("алексей стар", "alexey_syrover"), "instagram:Алексей старый"),
    (("алексей нов", "alexeysyrover"), "instagram:Алексей новый"),
    (("дин ", "дина", "syrover_school"), "instagram:Дина"),
    (("венер", "venera"), "instagram:ВЕНЕРА"),
]


# ═══════════════ ПЕРИОД ИЗ ВОПРОСА ═══════════════

def _detect_period(q):
    """(start, end, label) из вопроса. По умолчанию — последние 7 дней."""
    today = date.today()
    if "прошл" in q and "месяц" in q:
        first = today.replace(day=1) - timedelta(days=1)
        return first.replace(day=1), first, "прошлый месяц"
    if "прошл" in q and "недел" in q:
        e = today - timedelta(days=today.weekday() + 1) if False else today - timedelta(days=7)
        return e - timedelta(days=6), e, "прошлая неделя"
    for name, num in RU_MONTHS.items():
        if f"за {name}" in q or f"в {name}" in q or q.startswith(name):
            y = today.year if num <= today.month else today.year - 1
            s = date(y, num, 1)
            e = (s + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            if s <= today:
                return s, min(e, today), f"{MONTH_NAMES[num-1]} {y}"
    if "месяц" in q or "30 дн" in q:
        return today - timedelta(days=29), today, "30 дней"
    if "недел" in q or "7 дн" in q:
        return today - timedelta(days=6), today, "7 дней"
    if "вчера" in q:
        return today - timedelta(days=1), today - timedelta(days=1), "вчера"
    if "сегодня" in q:
        return today, today, "сегодня"
    return today - timedelta(days=6), today, "последние 7 дней"


def _get_context(start, end):
    """Полный контекст за период (кэш 5 минут)."""
    import time
    key = (start, end)
    now = time.time()
    if _cache["data"] and _cache["key"] == key and now - _cache["ts"] < 300:
        return _cache["data"]
    try:
        ctx = build_context(start, end)
        _cache.update({"key": key, "data": ctx, "ts": now})
        return ctx
    except Exception:
        return _cache["data"] if _cache["key"] == key else {"channels": [], "week": {}}


def build_context(start=None, end=None):
    """Строит полный контекст: каналы + воронка + контент + цели + конкуренты."""
    today = date.today()
    start = start or today - timedelta(days=6)
    end = end or today
    plen = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=plen - 1)
    p = calc.period_report(start, end)
    prev = calc.period_report(prev_start, prev_end)

    channels = []
    try:
        for ch in Channel.query.filter_by(is_active=True, is_competitor=False).all():
            cp = calc.period_report(start, end, ch.id)
            pp = calc.period_report(prev_start, prev_end, ch.id)
            reach = cp["agg"].get("reach") or 0
            prev_reach = pp["agg"].get("reach") or 0
            channels.append({
                "id": ch.id, "name": ch.name, "platform": ch.platform,
                "reach": reach, "prev_reach": prev_reach,
                "change": ((reach - prev_reach) / prev_reach * 100) if prev_reach else None,
                "err": cp["ind"].get("ERR"),
                "regs": cp["registrations"],
                "followers": cp["agg"].get("followers_end"),
                "subscribed": cp["agg"].get("subscribed"),
                "unsubscribed": cp["agg"].get("unsubscribed"),
                "views": cp["agg"].get("views"),
            })
    except Exception:
        pass

    utm_data = {}
    try:
        import utm as utm_mod
        br = utm_mod.breakdown(start, end)
        utm_data = {
            "combos": [(c[0], c[1], v["regs"]) for c, v in br.get("by_combo", [])[:5]],
            "mediums": [(m, v["regs"]) for m, v in sorted(br.get("by_medium", {}).items(),
                        key=lambda x: -x[1]["regs"])[:5] if v["regs"] > 0],
        }
    except Exception:
        pass

    trends, comments_data, top_content, best_slots = [], {}, [], []
    try:
        import intel
        for t in intel.trend_radar(8)[:5]:
            trends.append({"rubric": t["rubric"], "status": t["status"], "avg": t["recent_avg"]})
        hm = intel.posting_heatmap(30)
        best_slots = hm.get("best_slots", [])[:4] if hm else []
    except Exception:
        pass
    try:
        items = calc.content_stats_for_period(start, end)
        items.sort(key=lambda i: (i.get("reach") or 0), reverse=True)
        for i in items[:5]:
            top_content.append({"title": (i["item"].title or "(без названия)")[:48],
                                "channel": i["item"].channel.name,
                                "reach": i.get("reach"), "err": i.get("ERR")})
    except Exception:
        pass
    try:
        import comments as cm
        dig = cm.digest(start, end)
        comments_data = {
            "total": dig["total"],
            "pains": [c.text[:60] for c in dig["pains"][:3]],
            "questions": [c.text[:60] for c in dig["questions"][:3]],
            "ideas": [c.text[:60] for c in dig["ideas"][:3]],
        }
    except Exception:
        pass

    gc = {}
    try:
        import getcourse
        f = getcourse.funnel(start, end)
        gc = {"regs_gc": f.get("registrations"), "orders": f.get("orders"),
              "trials": f.get("orders_trials"), "revenue": f.get("orders_sum"),
              "all_deals": f.get("orders_total")}
    except Exception:
        pass

    goals = []
    try:
        from db import Goal
        for g in Goal.query.filter(Goal.end >= start).all():
            gs, ge = max(g.start, start), min(g.end, end)
            pr = calc.period_report(gs, ge)
            cur = {"reach": pr["agg"].get("reach"), "views": pr["agg"].get("views"),
                   "regs": pr["registrations"], "err": pr["ind"].get("ERR"),
                   "cv": pr["ind"].get("CV_reach"),
                   "payments": calc.payments_sum(gs, ge)}.get(g.metric)
            if g.metric == "orders":
                from utm import is_ours as _io
                from db import GcOrder
                cur = sum(1 for r in db.session.query(
                    GcOrder.utm_source, GcOrder.utm_medium, GcOrder.utm_campaign,
                    GcOrder.amount).filter(GcOrder.date >= gs, GcOrder.date <= ge).all()
                    if _io(r[0], r[1], r[2]) and (r[3] or 0) > 199)
            goals.append({"metric": g.metric, "target": g.target, "cur": cur,
                          "note": g.note,
                          "pct": (cur / g.target * 100 if (cur is not None and g.target) else None)})
    except Exception:
        pass

    competitors = []
    try:
        for ch in Channel.query.filter_by(is_competitor=True).all():
            cp = calc.period_report(start, end, ch.id)
            if cp["agg"].get("reach"):
                competitors.append({"name": ch.name, "reach": cp["agg"]["reach"],
                                    "err": cp["ind"].get("ERR")})
    except Exception:
        pass

    livedune_fresh = None
    try:
        from db import MetricSnapshot
        from sqlalchemy import func as _f
        last = db.session.query(_f.max(MetricSnapshot.fetched_at)).filter(
            MetricSnapshot.source == "livedune").scalar()
        livedune_fresh = str(last)[:16] if last else None
    except Exception:
        pass

    return {
        "period": {"start": str(start), "end": str(end)},
        "week": {
            "reach": p["agg"].get("reach"),
            "views": p["agg"].get("views"),
            "regs": p["registrations"],
            "err": p["ind"].get("ERR"),
            "cv": p["ind"].get("CV_reach"),
            "inter": p["ind"].get("interactions"),
            "followers": p["agg"].get("followers_end"),
            "net_growth": p["ind"].get("net_growth"),
        },
        "prev_week": {"reach": prev["agg"].get("reach"), "regs": prev["registrations"]},
        "channels": channels,
        "utm": utm_data,
        "trends": trends,
        "comments": comments_data,
        "top_content": top_content,
        "best_slots": best_slots,
        "gc": gc,
        "goals": goals,
        "competitors": competitors,
        "livedune_sync": livedune_fresh,
    }


# Совместимость со старым прокси-эндпоинтом
def _build_context():
    return build_context()


SUGGESTIONS = [
    "Какой канал работает хуже всех?",
    "Как дела в TikTok?",
    "Почему упали регистрации?",
    "Что публиковать завтра?",
    "Сколько заработали за месяц?",
    "Что думает аудитория в комментариях?",
    "Топ контента за месяц",
    "Что усилить на следующей неделе?",
]


def _normalize(text):
    if not text:
        return ""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = text.decode("cp1251")
            except Exception:
                text = text.decode("utf-8", errors="replace")
    return text.lower().strip()


def _fmt(n):
    if n is None:
        return "—"
    return "{:,.0f}".format(n).replace(",", " ")


def _fmt_money(n):
    return "{:,.0f}".format(n or 0).replace(",", " ") + " ₽"


# ═══════════════ LLM (быстрый поток, не блокирует чат) ═══════════════

_ASSISTANT_SYSTEM = (
    "Ты — встроенный AI SMM-аналитик приложения. Тебе даны ТОЛЬКО реальные "
    "рассчитанные данные в JSON и вопрос пользователя. Правила: "
    "1) отвечай СТРОГО на заданный вопрос — приветствие приветствием, без "
    "саморекламы и перечисления возможностей, если об этом не спросили; "
    "2) используй ТОЛЬКО числа из данных, ничего не выдумывай; "
    "3) если данных для ответа нет — так и скажи; "
    "4) кратко и по делу (до 6 предложений), по-русски; "
    "5) если уместно — заверши конкретным действием."
)


def _llm_answer(question, ctx):
    """Попытка LLM с жёстким таймаутом (хостинг может не пускать к API —
    тогда мгновенный переход к rule-движку)."""
    try:
        import requests
        key = get_setting("ai_api_key")
        if not key:
            return None
        base = (get_setting("ai_base_url") or "https://api.z.ai/api/paas/v4").rstrip("/")
        model = get_setting("ai_model") or "glm-4.6"
        payload_ctx = json.dumps(ctx, ensure_ascii=False, default=str)
        user = f"Данные:\n{payload_ctx}\n\nВопрос: {question}"
        # у думающих моделей отключаем «размышления» — чат должен отвечать быстро
        body = {"model": model, "temperature": 0.2,
                "thinking": {"type": "disabled"},
                "messages": [{"role": "system", "content": _ASSISTANT_SYSTEM},
                             {"role": "user", "content": user}]}
        r = requests.post(f"{base}/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json=body, timeout=(5, 45))
        if r.status_code == 400:  # модель не знает параметр thinking — без него
            body.pop("thinking", None)
            r = requests.post(f"{base}/chat/completions",
                              headers={"Authorization": f"Bearer {key}"},
                              json=body, timeout=(5, 45))
        d = r.json()
        if r.status_code == 200 and d.get("choices"):
            text = (d["choices"][0]["message"].get("content") or "").strip()
            return text or None
    except Exception:
        return None
    return None


# ═══════════════ ГЛАВНЫЙ ВХОД ═══════════════

def ask(question, period_hint=None):
    q = _normalize(question)
    if not q:
        return {"answer": "Задайте вопрос — например, «какой канал работает хуже всех?»",
                "source": "app"}

    # 1) социальные реплики — коротко и по-человечески
    social = _social(q)
    if social:
        return social

    # 2) период: селектор чата важнее слов в вопросе
    if period_hint in ("week", "30d", "month"):
        today = date.today()
        if period_hint == "30d":
            start, end = today - timedelta(days=29), today
        elif period_hint == "month":
            start, end = today.replace(day=1), today
        else:
            start, end = today - timedelta(days=6), today
        label = {"week": "7 дней", "30d": "30 дней", "month": "месяц"}[period_hint]
    else:
        start, end, label = _detect_period(q)
    ctx = _get_context(start, end)

    # 3) LLM (если настроен и доступен) — строгий промпт
    llm = _llm_answer(q, ctx)
    if llm:
        return {"answer": llm, "source": "llm", "period": label}

    # 4) точный rule-движок
    answer = _rule_answer(q, ctx, label)
    return {"answer": answer, "source": "app", "period": label}


# ═══════════════ СОЦИАЛЬНЫЕ РЕПЛИКИ ═══════════════

def _social(q):
    s = q.strip(" !.).,")
    if re.fullmatch(r"(привет|приветствую|здравствуй\w*|добрый день|добрый вечер|доброе утро|хай|hello|hi)\??", s):
        today = date.today()
        ctx = _get_context(today - timedelta(days=6), today)
        w = ctx.get("week", {})
        by_regs = sorted([c for c in ctx.get("channels", []) if c["regs"]],
                         key=lambda c: c["regs"], reverse=True)
        sug = []
        if by_regs:
            sug.append(f"«почему {by_regs[0]['name']} даёт больше всех регистраций?»")
        if w.get("err") is not None:
            sug.append(f"«почему ERR {w['err']:.2f}%?»")
        if not sug:
            sug.append("«какой канал работает хуже всех?»")
        return {"answer": "Привет! 👋\n"
                f"За последние 7 дней: охват {_fmt(w.get('reach'))}, "
                f"ERR {(w.get('err') or 0):.2f}%, регистрации {(w.get('regs') or 0):.0f}.\n"
                "Спросите, например: " + " или ".join(sug[:2]) + " — отвечу цифрами.",
                "source": "app"}
    if re.fullmatch(r"(спасибо|благодарю|спс|thanks|thank you|круто|супер|отлично)\W*", s):
        return {"answer": "Рад помочь! Если что — спрашивайте по цифрам.", "source": "app"}
    if re.search(r"(кто ты|что ты умеешь|что можешь|твои возможност|как тобой польз)", s):
        return {"answer":
                "Я AI SMM-аналитик вашего приложения: вижу все данные — каналы (LiveDune, "
                "YouTube, TikTok, Дзен), регистрации и продажи GetCourse, контент, "
                "комментарии, цели.\n"
                "Отвечаю строго на вопрос с реальными цифрами. Понимаю периоды: "
                "«за сентябрь», «за месяц», «вчера».\n"
                "Примеры: «как дела в тиктоке за месяц», «сколько заработали», "
                "«что публиковать», «почему упали регистрации».", "source": "app"}
    return None


# ═══════════════ RULE-ДВИЖОК ═══════════════

def _match_channel(q):
    for hints, target in CHANNEL_HINTS:
        for h in hints:
            if h in q:
                return target
    return None


def _rule_answer(q, ctx, label):
    chs = ctx.get("channels", [])
    w = ctx.get("week", {})
    pw = ctx.get("prev_week", {})
    L = []
    by_reach = sorted([c for c in chs if c["reach"] > 0], key=lambda c: c["reach"], reverse=True)
    by_err = sorted([c for c in chs if (c["reach"] or 0) >= 1000], key=lambda c: c["err"] or 0, reverse=True)
    by_regs = sorted([c for c in chs if c["regs"] > 0], key=lambda c: c["regs"], reverse=True)

    # ── конкретный канал упомянут ──
    target = _match_channel(q)
    if target and not any(x in q for x in ("всех", "все ", "лучший из", "сравни")):
        if ":" in target:
            _, name = target.split(":", 1)
            sel = [c for c in chs if c["name"] == name]
        else:
            sel = [c for c in chs if c["platform"] == target]
        if sel:
            for c in sel:
                L.append(f"«{c['name']}» за {label}:")
                if c["reach"]:
                    L.append(f"  охват {_fmt(c['reach'])}"
                             + (f" ({c['change']:+.0f}% к прошлому периоду)" if c["change"] is not None else ""))
                if c["views"]:
                    L.append(f"  просмотры {_fmt(c['views'])}"
                             + ("" if c["reach"] else " (источник не отдаёт охват — считаем просмотрами)"))
                if c["err"] is not None:
                    L.append(f"  ERR {c['err']:.2f}%")
                if c["regs"]:
                    L.append(f"  регистрации {c['regs']:.0f}")
                if c["followers"]:
                    net = (c["subscribed"] or 0) - (c["unsubscribed"] or 0)
                    L.append(f"  подписчики {_fmt(c['followers'])} (прирост {net:+.0f})")
                if c["reach"] and by_reach:
                    share = c["reach"] / sum(x["reach"] for x in by_reach) * 100
                    L.append(f"  доля в общем охвате: {share:.0f}%")
            v = sel[0]
            if v["change"] is not None and v["change"] < -20:
                L.append(f"⚠ Охват просел на {abs(v['change']):.0f}% — проверить контент и размещения.")
            elif v["change"] is not None and v["change"] > 20:
                L.append(f"✅ Рост охвата {v['change']:+.0f}% — усиливать формат.")
            return chr(10).join(L)

    # ── деньги/продажи ──
    if any(x in q for x in ("деньг", "заработ", "выруч", "продаж", "оплат", "заказ",
                            "воронк", "money", "сколько принёс", "сколько принес")):
        gc = ctx.get("gc", {})
        L.append(f"За {label} (наши метки):")
        L.append(f"  пользователи GC: {_fmt(gc.get('regs_gc'))}")
        L.append(f"  продуктовые заказы: {_fmt(gc.get('orders'))} "
                 f"(пробных занятий: {_fmt(gc.get('trials'))})")
        L.append(f"  выручка: {_fmt_money(gc.get('revenue'))}")
        cv = w.get("cv")
        if cv is not None:
            L.append(f"  CV охват→регистрация: {cv:.3f}%")
        L.append("Детали: экран «Продажи».")
        return chr(10).join(L)

    # ── регистрации ──
    if any(x in q for x in ("регистрац", "заявк", "лид", "reg")):
        regs, prev = w.get("regs") or 0, pw.get("regs") or 0
        L.append(f"Регистрации (наши метки) за {label}: {regs:.0f}.")
        if prev:
            L.append(f"  прошлый период: {prev:.0f} ({(regs - prev) / prev * 100:+.0f}%)")
        if by_regs:
            L.append("По каналам:")
            for c in by_regs[:3]:
                L.append(f"  {c['name']}: {c['regs']:.0f}")
        utm = ctx.get("utm", {})
        if utm.get("combos"):
            L.append("Лучшие связки: "
                     + "; ".join(f"{s}×{m} — {n:.0f}" for s, m, n in utm["combos"][:3]))
        return chr(10).join(L)

    # ── худший канал ──
    if any(x in q for x in ("хуже", "слаб", "плох", "worst", "аутсайдер")):
        if by_err:
            worst, best = by_err[-1], by_err[0]
            L.append(f"Худший по ERR за {label}: «{worst['name']}» — {worst['err']:.2f}% "
                     f"(охват {_fmt(worst['reach'])}).")
            L.append(f"Лучший: «{best['name']}» — {best['err']:.2f}%.")
            L.append(f"→ Пересобрать контент-план «{worst['name']}» по образцу «{best['name']}».")
        elif by_reach:
            L.append(f"Самый слабый по охвату: «{by_reach[-1]['name']}» — "
                     f"{_fmt(by_reach[-1]['reach'])}.")
        return chr(10).join(L)

    # ── лучший/топ ──
    if any(x in q for x in ("лучш", "топ ", "top", "best", "лидер")):
        if by_reach:
            L.append(f"Топ-3 канала по охвату за {label}:")
            for i, c in enumerate(by_reach[:3], 1):
                s_ = f"  {i}. {c['name']} — {_fmt(c['reach'])}"
                if c["change"] is not None:
                    s_ += f" ({c['change']:+.0f}%)"
                if c["err"]:
                    s_ += f", ERR {c['err']:.2f}%"
                L.append(s_)
        return chr(10).join(L)

    # ── что публиковать / контент / время ──
    if any(x in q for x in ("публиковать", "контент", "завтра", "план", "когда выклад",
                            "время публикац", "что постить", "что снимать")):
        trends = ctx.get("trends", [])
        if trends:
            growing = [t for t in trends if "растёт" in t["status"]]
            burning = [t for t in trends if "выгора" in t["status"]]
            if growing:
                L.append("📈 Масштабировать: "
                         + ", ".join(f"«{t['rubric']}» ({_fmt(t['avg'])} ср. охват)" for t in growing[:2]))
            if burning:
                L.append("📉 Пересмотреть: " + ", ".join(f"«{t['rubric']}»" for t in burning[:2]))
        tc = ctx.get("top_content", [])
        if tc:
            L.append(f"Лучший материал {label}: «{tc[0]['title']}» "
                     f"({tc[0]['channel']}, охват {_fmt(tc[0]['reach'])}).")
        slots = ctx.get("best_slots", [])
        if slots:
            L.append("Лучшее время: "
                     + "; ".join(f"{s['day']} {s['hour']}:00" for s in slots[:3]) + ".")
        if not L:
            L.append("Контент-данных за период мало — загрузите выгрузки TikTok/Дзен на «Импорте».")
        return chr(10).join(L)

    # ── подписчики ──
    if any(x in q for x in ("подписчик", "followers", "прирост", "отпис")):
        L.append(f"Всего подписчиков: {_fmt(w.get('followers'))} "
                 f"(прирост {label}: {(w.get('net_growth') or 0):+d}).")
        sub = sorted([c for c in chs if c["subscribed"]],
                     key=lambda c: c["subscribed"], reverse=True)
        for c in sub[:4]:
            L.append(f"  {c['name']}: +{c['subscribed']:.0f} / −{(c['unsubscribed'] or 0):.0f}")
        return chr(10).join(L)

    # ── ERR / вовлечённость ──
    if any(x in q for x in ("err", "вовлеч", "engagement", "лайк")):
        L.append(f"Общий ERR за {label}: {(w.get('err') or 0):.2f}% "
                 f"(взаимодействий {_fmt(w.get('inter'))}).")
        for c in by_err[:5]:
            if c["err"]:
                L.append(f"  {c['name']}: {c['err']:.2f}%")
        return chr(10).join(L)

    # ── охват/просмотры ──
    if any(x in q for x in ("охват", "reach", "просмотр", "views")):
        reach, prev = w.get("reach") or 0, pw.get("reach") or 0
        L.append(f"Охват за {label}: {_fmt(reach)} (прошлый период: {_fmt(prev)}"
                 + (f", {(reach - prev) / prev * 100:+.0f}%" if prev else "") + ").")
        if by_reach:
            c = by_reach[0]
            L.append(f"Лидер: «{c['name']}» — {_fmt(c['reach'])} "
                     f"({c['reach'] / max(reach, 1) * 100:.0f}% общего).")
        return chr(10).join(L)

    # ── комментарии/аудитория ──
    if any(x in q for x in ("коммент", "болев", "аудитор", "думает", "что спрашива")):
        cmx = ctx.get("comments", {})
        L.append(f"Комментариев за {label}: {cmx.get('total', 0)}.")
        for ptxt in cmx.get("pains", [])[:2]:
            L.append(f"  💔 «{ptxt}»")
        for qtxt in cmx.get("questions", [])[:2]:
            L.append(f"  ❓ «{qtxt}»")
        if cmx.get("ideas"):
            L.append(f"  💡 «{cmx['ideas'][0]}»")
        if not cmx.get("total"):
            L.append("Пока пусто — импортируйте комментарии (экран «Комментарии», там инструкция).")
        return chr(10).join(L)

    # ── цели ──
    if "цел" in q or "kpi" in q or "план " in q:
        goals = ctx.get("goals", [])
        if goals:
            for g in goals:
                pct = f" {g['pct']:.0f}%" if g["pct"] is not None else ""
                if g["pct"] is not None and g["pct"] > 999:
                    pct = " (проверьте единицы плана — факт в ₽)"
                unit = " ₽" if g["metric"] == "payments" and (g["cur"] or 0) > 1000 else ""
                note = f" ({g['note']})" if g.get("note") else ""
                L.append(f"  {g['metric']}: факт {_fmt(g['cur'])}{unit} из {_fmt(g['target'])}{pct}{note}")
        else:
            L.append("Активных целей нет — поставьте на экране «Цели» "
                     "(охват, регистрации, заказы, оплаты).")
        return chr(10).join(L)

    # ── конкуренты ──
    if "конкурент" in q or "соперник" in q:
        comps = ctx.get("competitors", [])
        if comps:
            for c in comps:
                L.append(f"  {c['name']}: охват {_fmt(c['reach'])}"
                         + (f", ERR {c['err']:.2f}%" if c["err"] else ""))
        else:
            L.append("По конкурентам за период данных нет — подключите их в LiveDune "
                     "(«Проверки аккаунтов»).")
        return chr(10).join(L)

    # ── свежесть данных / LiveDune ──
    if "livedune" in q or "лайвдюн" in q or "обновля" in q or "свеж" in q or "актуальн" in q:
        L.append(f"LiveDune доставил данные: {ctx.get('livedune_sync') or '—'}.")
        L.append("График: LiveDune/GetCourse — 4 раза в сутки, YouTube — ежедневно, "
                 "TikTok/Дзен — по выгрузкам.")
        L.append("Целостность: экран 🔍 «Аудит».")
        return chr(10).join(L)

    # ── почему/причина ──
    if "почему" in q or "причин" in q:
        regs, prev = w.get("regs") or 0, pw.get("regs") or 0
        if "регистр" in q:
            if prev and regs < prev:
                L.append(f"Регистрации: {prev:.0f} → {regs:.0f} "
                         f"({(regs - prev) / prev * 100:+.0f}%).")
                for c in chs:
                    if c["change"] is not None and c["change"] < -20 and c["reach"] > 0:
                        L.append(f"  вероятный фактор: охват «{c['name']}» {c['change']:+.0f}%")
                L.append("  также: смена размещений/меток, сезонность.")
            else:
                L.append(f"Регистрации за {label}: {regs:.0f} — падения к прошлому периоду нет.")
        elif "охват" in q:
            reach, prev = w.get("reach") or 0, pw.get("reach") or 0
            L.append(f"Охват: {_fmt(prev)} → {_fmt(reach)}"
                     + (f" ({(reach - prev) / prev * 100:+.0f}%)" if prev else "") + ".")
            for c in [c for c in chs if c["change"] is not None and c["change"] < -20][:3]:
                L.append(f"  сильнее всего просел «{c['name']}» ({c['change']:+.0f}%).")
        else:
            L.append("Уточните: «почему упали регистрации» или «почему упал охват».")
        return chr(10).join(L)

    # ── что усилить/рекомендации ──
    if any(x in q for x in ("усилить", "улучшить", "рекомендац", "что делать", "совет")):
        if by_reach:
            L.append(f"1. Масштабировать «{by_reach[0]['name']}» — лидер охвата "
                     f"({_fmt(by_reach[0]['reach'])}).")
        if by_err:
            L.append(f"2. Лучший ERR у «{by_err[0]['name']}» ({by_err[0]['err']:.2f}%) — "
                     "брать форматы оттуда.")
        if by_err and len(by_err) > 2:
            L.append(f"3. Пересмотреть «{by_err[-1]['name']}» (ERR {by_err[-1]['err']:.2f}%).")
        utm = ctx.get("utm", {})
        if utm.get("combos"):
            L.append(f"4. Связка {utm['combos'][0][0]} × {utm['combos'][0][1]} даёт "
                     f"{utm['combos'][0][2]:.0f} рег. — повторять.")
        return chr(10).join(L)

    # ── как дела (общее, кратко) ──
    if "как дела" in q or "что нового" in q or "итоги" in q or "сводка" in q:
        L.append(f"За {label}: охват {_fmt(w.get('reach'))}, ERR {(w.get('err') or 0):.2f}%, "
                 f"регистрации {(w.get('regs') or 0):.0f}, "
                 f"выручка {_fmt_money(ctx.get('gc', {}).get('revenue'))}.")
        if by_reach:
            L.append(f"Лидер охвата: «{by_reach[0]['name']}».")
        return chr(10).join(L)

    # ── не поняли вопрос: умные уточнения с реальными цифрами ──
    opts = []
    if by_reach:
        opts.append(f"«как дела в {by_reach[0]['platform']}?»")
    if (w.get("regs") or 0) > 0:
        opts.append(f"«почему {_fmt(w.get('regs'))} регистраций?»")
    opts.append("«что усилить на следующей неделе?»")
    return ("Я не понял вопрос точно. Спросите иначе, например:\n"
            + chr(10).join("• " + o for o in opts[:3])
            + "\nИли используйте кнопки быстрых вопросов ниже.")
