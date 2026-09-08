# -*- coding: utf-8 -*-
"""AI SMM-специалист: умный контекст с данными по каждому каналу.
Контекст предвычисляется и кэшируется (обновляется релеем и при прогреве).
Ассистент отвечает конкретными цифрами, а не общими фразами."""
import json
from datetime import date, timedelta
from db import db, get_setting, set_setting, Channel, Registration, GcOrder
import calc

_cache = {"data": None, "ts": 0}


def _get_context():
    """Получить контекст: кэш → пересчёт (максимум 5 минут давности)."""
    import time
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < 300:
        return _cache["data"]
    try:
        ctx = build_context()
        _cache["data"] = ctx
        _cache["ts"] = now
        return ctx
    except Exception:
        return _cache["data"] or {"channels": [], "week": {}}


def build_context():
    """Строит полный контекст с данными по каждому каналу."""
    today = date.today()
    w_s = today - timedelta(days=6)
    prev_s = w_s - timedelta(days=7)
    p = calc.period_report(w_s, today)
    prev = calc.period_report(prev_s, w_s)

    channels = []
    try:
        for ch in Channel.query.filter_by(is_active=True, is_competitor=False).all():
            cp = calc.period_report(w_s, today, ch.id)
            pp = calc.period_report(prev_s, w_s, ch.id)
            reach = cp["agg"].get("reach") or 0
            prev_reach = pp["agg"].get("reach") or 0
            change = ((reach - prev_reach) / prev_reach * 100) if prev_reach else None
            channels.append({
                "name": ch.name, "platform": ch.platform,
                "reach": reach, "prev_reach": prev_reach, "change": change,
                "err": cp["ind"].get("ERR"),
                "regs": cp["registrations"],
                "followers": cp["agg"].get("followers_end"),
                "subscribed": cp["agg"].get("subscribed"),
                "unsubscribed": cp["agg"].get("unsubscribed"),
            })
    except Exception:
        pass

    # UTM
    utm_data = {}
    try:
        import utm as utm_mod
        br = utm_mod.breakdown(w_s, today)
        utm_data = {
            "combos": [(c[0], c[1], v["regs"]) for c, v in br.get("by_combo", [])[:5]],
            "mediums": [(m, v["regs"]) for m, v in sorted(br.get("by_medium", {}).items(), key=lambda x: -x[1]["regs"])[:5] if v["regs"] > 0],
            "funnels": {f: v["regs"] for f, v in br.get("by_funnel", {}).items()},
        }
    except Exception:
        pass

    # Тренды
    trends = []
    try:
        import intel
        for t in intel.trend_radar(8)[:5]:
            trends.append({"rubric": t["rubric"], "status": t["status"], "avg": t["recent_avg"]})
    except Exception:
        pass

    # Комментарии
    comments_data = {}
    try:
        import comments as cm
        dig = cm.digest(w_s, today)
        comments_data = {
            "total": dig["total"],
            "pains": [c.text[:60] for c in dig["pains"][:3]],
            "questions": [c.text[:60] for c in dig["questions"][:3]],
            "ideas": [c.text[:60] for c in dig["ideas"][:3]],
        }
    except Exception:
        pass

    # GetCourse
    gc = {}
    try:
        gc = p.get("gc", {})
    except Exception:
        pass

    return {
        "week": {
            "reach": p["agg"].get("reach"),
            "views": p["agg"].get("views"),
            "regs": p["registrations"],
            "err": p["ind"].get("ERR"),
            "cv": p["ind"].get("CV_reach"),
            "inter": p["ind"].get("interactions"),
            "followers": p["agg"].get("followers_end"),
        },
        "prev_week": {
            "reach": prev["agg"].get("reach"),
            "regs": prev["registrations"],
        },
        "channels": channels,
        "utm": utm_data,
        "trends": trends,
        "comments": comments_data,
        "gc": gc,
    }


SUGGESTIONS = [
    "Какой канал работает хуже всех?",
    "Что публиковать завтра?",
    "Почему упали регистрации?",
    "Какая рубрика даёт больше регистраций?",
    "Топ контента за месяц",
    "Что думает аудитория в комментариях?",
    "Какая воронка конвертирует лучше?",
    "Что усилить на следующей неделе?",
]


def _normalize(text):
    """Нормализация текста: исправление кодировки, lowercase, strip."""
    if not text:
        return ""
    # Если bytes — декодируем как UTF-8 с fallback
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = text.decode("cp1251")
            except Exception:
                text = text.decode("utf-8", errors="replace")
    return text.lower().strip()


def ask(question):
    """Умный ответ с конкретными данными по каналу."""
    question = _normalize(question)
    if not question:
        return {"answer": "Задайте вопрос."}

    ctx = _get_context()
    answer = _smart_answer(question, ctx)
    return {"answer": answer, "source": "data_driven"}


def _fmt(n):
    return "{:,.0f}".format(n).replace(",", " ") if n else "0"


def _smart_answer(question, ctx):
    q = (question or "").lower().strip()
    L = []
    chs = ctx.get("channels", [])
    w = ctx.get("week", {})
    pw = ctx.get("prev_week", {})

    # Сортируем каналы по разным метрикам заранее
    by_reach = sorted([c for c in chs if c["reach"] > 0], key=lambda c: c["reach"], reverse=True)
    by_err = sorted([c for c in chs if (c["reach"] or 0) >= 1000], key=lambda c: c["err"] or 0, reverse=True)
    by_regs = sorted([c for c in chs if c["regs"] > 0], key=lambda c: c["regs"], reverse=True)

    # === Вопросы про каналы ===
    if any(x in q for x in ("хуже", "слаб", "плох", "worst", "отстаю", "аутсайдер")):
        if by_err:
            worst = by_err[-1]
            best = by_err[0]
            L.append(f"Худший по ERR: «{worst['name']}» — {worst['err']:.2f}% (охват {_fmt(worst['reach'])}).")
            L.append(f"Лучший: «{best['name']}» — {best['err']:.2f}%.")
            L.append(f"→ Перераспределить ресурсы с «{worst['name']}» на «{best['name']}».")
        elif by_reach:
            worst = by_reach[-1]
            L.append(f"Самый слабый по охвату: «{worst['name']}» — {_fmt(worst['reach'])}.")

    elif any(x in q for x in ("лучш", "топ", "best", "лидер", "лучший канал")):
        if by_reach:
            L.append("Топ-3 канала по охвату за неделю:")
            for i, c in enumerate(by_reach[:3], 1):
                ch_str = f"  {i}. {c['name']} — {_fmt(c['reach'])} охват"
                if c["change"] is not None:
                    ch_str += f" ({c['change']:+.0f}%)"
                if c["err"]:
                    ch_str += f", ERR {c['err']:.2f}%"
                if c["regs"]:
                    ch_str += f", {c['regs']:.0f} рег."
                L.append(ch_str)

    elif any(x in q for x in ("канал", "channel", "все каналы", "сравн")):
        L.append("Все каналы за неделю:")
        for c in chs:
            ch_str = f"  {c['name']}: {_fmt(c['reach'])} охват"
            if c["change"] is not None:
                ch_str += f" ({c['change']:+.0f}%)"
            if c["err"]:
                ch_str += f", ERR {c['err']:.2f}%"
            L.append(ch_str)

    # === Вопросы про регистрации ===
    elif any(x in q for x in ("регистрац", "рег", "reg", "заявк", "лид")):
        regs = w.get("regs", 0)
        prev_regs = pw.get("regs", 0)
        L.append(f"Регистрации за неделю: {regs:.0f} (прошлая неделя: {prev_regs:.0f}).")
        if prev_regs:
            pct = (regs - prev_regs) / prev_regs * 100
            L.append(f"Изменение: {pct:+.0f}%.")
        if by_regs:
            L.append("Топ-3 по регистрациям:")
            for c in by_regs[:3]:
                L.append(f"  {c['name']}: {c['regs']:.0f} рег.")
        utm = ctx.get("utm", {})
        if utm.get("combos"):
            L.append("Лучшие связки:")
            for src, med, n in utm["combos"][:3]:
                L.append(f"  {src} × {med}: {n:.0f} рег.")
        if ctx.get("funnels"):
            for f, n in ctx.get("funnels", {}).items():
                if n:
                    L.append(f"  {f}: {n:.0f}")

    # === Вопросы про контент/публикации ===
    elif any(x in q for x in ("публиковать", "контент", "завтра", "план", "publish", "content", "что делать")):
        trends = ctx.get("trends", [])
        if trends:
            L.append("Рекомендации по контенту:")
            growing = [t for t in trends if "растёт" in t["status"]]
            burning = [t for t in trends if "выгора" in t["status"]]
            if growing:
                L.append("📈 Усилить:")
                for t in growing[:2]:
                    L.append(f"  «{t['rubric']}» — ср. охват {_fmt(t['avg'])}")
            if burning:
                L.append("📉 Обновить или убрать:")
                for t in burning[:2]:
                    L.append(f"  «{t['rubric']}»")
        if by_reach:
            L.append(f"Лучший канал для важных публикаций: «{by_reach[0]['name']}».")
        L.append("AI-контент-план: экран «AI-аналитика».")

    # === Вопросы про охват ===
    elif any(x in q for x in ("охват", "reach", "просмотр", "views")):
        reach = w.get("reach", 0)
        prev_reach = pw.get("reach", 0)
        L.append(f"Охват за неделю: {_fmt(reach)} (прошлая: {_fmt(prev_reach)}).")
        if prev_reach:
            L.append(f"Изменение: {(reach - prev_reach) / prev_reach * 100:+.0f}%.")
        if by_reach:
            L.append(f"Лидер: «{by_reach[0]['name']}» — {_fmt(by_reach[0]['reach'])} ({by_reach[0]['reach'] / max(reach, 1) * 100:.0f}% общего).")

    # === Вопросы про ERR/вовлечённость ===
    elif any(x in q for x in ("err", "вовлеч", "engagement", "лайк")):
        L.append(f"Общий ERR: {w.get('err', 0):.2f}%.")
        if by_err:
            L.append("По каналам:")
            for c in by_err[:5]:
                if c["err"]:
                    L.append(f"  {c['name']}: {c['err']:.2f}%")

    # === Вопросы про подписчиков ===
    elif any(x in q for x in ("подписчик", "followers", "прирост")):
        L.append(f"Всего подписчиков: {_fmt(w.get('followers'))}.")
        sub = [c for c in chs if c["subscribed"]]
        if sub:
            sub.sort(key=lambda c: c["subscribed"], reverse=True)
            L.append("Прирост по каналам:")
            for c in sub[:5]:
                L.append(f"  {c['name']}: +{c['subscribed']:.0f} / -{c['unsubscribed'] or 0:.0f}")

    # === Вопросы про деньги ===
    elif any(x in q for x in ("деньг", "продаж", "оплат", "заказ", "money", "roi", "воронк", "конверс", "cv")):
        gc = ctx.get("gc", {})
        L.append(f"Заказы: {gc.get('orders', 0):.0f}, оплаты: {gc.get('payments_sum', 0):,.0f} ₽.".replace(",", " "))
        L.append(f"CV из охвата: {w.get('cv', 0):.3f}%.")
        L.append("Детали: экраны «Продажи» и «ROI».")

    # === Вопросы про аудиторию/комментарии ===
    elif any(x in q for x in ("коммент", "болев", "аудитор", "вопрос", "comment", "думает")):
        cm = ctx.get("comments", {})
        L.append(f"Комментариев за неделю: {cm.get('total', 0)}.")
        for p in cm.get("pains", [])[:2]:
            L.append(f"  💔 «{p}»")
        for qn in cm.get("questions", [])[:2]:
            L.append(f"  ❓ «{qn}»")
        if cm.get("ideas"):
            L.append(f"  💡 Идея: «{cm['ideas'][0]}»")

    # === Вопросы про тренды/рубрики ===
    elif any(x in q for x in ("тренд", "рубрик", "тема", "выгора", "trend")):
        trends = ctx.get("trends", [])
        if trends:
            for t in trends[:5]:
                L.append(f"  {t['status']} «{t['rubric']}» — ср. {_fmt(t['avg'])}")

    # === Почему/причина ===
    elif "почему" in q or "причин" in q or "why" in q:
        if "регистр" in q or "рег" in q:
            regs = w.get("regs", 0)
            prev_regs = pw.get("regs", 0)
            if prev_regs and regs < prev_regs:
                L.append(f"Регистрации упали с {prev_regs:.0f} до {regs:.0f} ({(regs - prev_regs) / prev_regs * 100:+.0f}%).")
                L.append("Вероятные факторы:")
                if by_reach and by_reach[0]["change"] is not None and by_reach[0]["change"] < 0:
                    L.append(f"  Снижение охвата «{by_reach[0]['name']}» ({by_reach[0]['change']:+.0f}%)")
                L.append("  Изменение контент-стратегии или размещений")
                L.append("  Сезонность/праздники")
            else:
                L.append(f"Регистрации: {regs:.0f} за неделю. Падения не зафиксировано.")
        elif "охват" in q:
            L.append(f"Охват: {_fmt(w.get('reach'))}. Сравнение с прошлой неделей — на главной странице.")
        else:
            L.append("Уточните: «почему упали регистрации», «почему упал охват».")

    # === Что усилить ===
    elif any(x in q for x in ("усилить", "улучшить", "improve", "рекомендац")):
        if by_reach:
            L.append(f"1. Усилить «{by_reach[0]['name']}» — лидер по охвату ({_fmt(by_reach[0]['reach'])}).")
        if by_err and len(by_err) > 1:
            L.append(f"2. Масштабировать формат «{by_err[0]['name']}» — лучший ERR ({by_err[0]['err']:.2f}%).")
        utm = ctx.get("utm", {})
        if utm.get("combos"):
            L.append(f"3. Чаще использовать {utm['combos'][0][0]} × {utm['combos'][0][1]} — {utm['combos'][0][2]:.0f} рег.")
        if by_err and len(by_err) > 2:
            L.append(f"4. Пересмотреть «{by_err[-1]['name']}» — ERR {by_err[-1]['err']:.2f}%.")

    # === Общий ===
    # Транслит для надёжности (если кириллица не распозналась)
    elif any(x in q for x in ("kanal", "channel", "worst", "best", "top")):
        if by_err:
            worst = by_err[-1]
            L.append(f"Худший: «{worst['name']}» (ERR {worst['err'] or 0:.2f}%).")
        if by_reach:
            L.append(f"Лучший: «{by_reach[0]['name']}» ({_fmt(by_reach[0]['reach'])}).")

    elif any(x in q for x in ("reg", "registr", "lead")):
        L.append(f"Регистрации: {w.get('regs', 0):.0f}.")

    else:
        L.append(f"Ключевые цифры за неделю:")
        L.append(f"  Охват: {_fmt(w.get('reach'))}")
        L.append(f"  Регистрации: {w.get('regs', 0):.0f}")
        L.append(f"  ERR: {w.get('err', 0):.2f}%")
        L.append(f"  Подписчики: {_fmt(w.get('followers'))}")
        L.append("")
        L.append("Я отвечаю на вопросы:")
        L.append("  «какой канал хуже всех» / «что публиковать» / «почему упали регистрации»")
        L.append("  «сколько регистраций» / «что думает аудитория» / «что усилить»")

    return chr(10).join(L) if L else "Задайте вопрос."
