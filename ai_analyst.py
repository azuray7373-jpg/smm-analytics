# -*- coding: utf-8 -*-
"""AI-аналитик и AI-контролёр.
Принцип: нейросеть НЕ считает и не придумывает цифры — она получает
уже рассчитанные данные (calc.py) и только формирует текст.
Если API-ключ не задан, работает детерминированный rule-based аналитик,
который тоже опирается только на переданные числа."""
import json, re
from db import get_setting


def _call_llm(system, user):
    """Вызов внешней LLM через OpenAI-совместимый API (OpenAI, Gemini и др.).
    Основная модель из настроек + автоматический фолбэк (например, при 503
    перегруженной gemini-3.8-flash пробуем старшие flash-модели)."""
    key = get_setting("ai_api_key")
    if not key:
        return None
    try:
        import requests
        base = get_setting("ai_base_url", "https://api.openai.com/v1").rstrip("/")
        models = [get_setting("ai_model", "gpt-4o-mini")]
        for fb in ("gemini-3.7-flash", "gemini-2.5-flash"):
            if fb not in models:
                models.append(fb)
        for model in models:
            try:
                r = requests.post(f"{base}/chat/completions",
                                  headers={"Authorization": f"Bearer {key}"},
                                  json={"model": model, "temperature": 0.2, "messages": [
                                      {"role": "system", "content": system},
                                      {"role": "user", "content": user}]}, timeout=120)
                d = r.json()
                if r.status_code == 200 and d.get("choices"):
                    content = d["choices"][0]["message"].get("content")
                    if content and str(content).strip():
                        return content
            except Exception:
                continue
        return None
    except Exception:
        return None


SYSTEM = (
    "Ты — SMM-аналитик. Тебе передаются ТОЛЬКО реальные рассчитанные данные в JSON. "
    "Запрещено: выдумывать числа, заменять NULL нулём, корректировать статистику, "
    "утверждать причинно-следственную связь без данных (пиши 'вероятный фактор'), "
    "сравнивать несовместимые метрики платформ. Если данных недостаточно — напиши "
    "'Недостаточно данных для вывода'. Отвечай по-русски, структурно."
)


def fmt(v, suffix=""):
    if v is None:
        return "н/д"
    if isinstance(v, float):
        return f"{v:,.1f}".replace(",", " ").replace(".0", "") + suffix
    return f"{v:,}".replace(",", " ") + suffix


def classify_content_text(text, format_=""):
    """Автоклассификация контента: AI по ключу или словарный эвристический разбор.
    Возвращает JSON-теги: тема, рубрика, тип, CTA, боль, сегмент, триггер, оффер."""
    tags = {
        "тема": None, "рубрика": None, "формат": format_ or None, "тип": None, "смысл": None,
        "CTA": None, "боль": None, "сегмент": None, "эмоциональный_триггер": None,
        "продающий_тип": None, "есть_оффер": False, "есть_регистрационный_CTA": False,
    }
    t = (text or "").lower()
    if not t:
        return tags
    tags["тема"] = (text[:60] + "…") if len(text) > 60 else text
    if any(w in t for w in ("рецепт", "готов", "еда", "продукт")):
        tags["рубрика"] = "еда/продукты"
    if any(w in t for w in ("тренировк", "упражнен", "программ")):
        tags["рубрика"] = "тренировки"
    if any(w in t for w in ("результат", "до/после", "похуд")):
        tags["рубрика"] = "результаты"
    reg_words = ("регистрир", "запишись", "записывай", "ссылка в", "переходи", "запись")
    tags["есть_регистрационный_CTA"] = any(w in t for w in reg_words)
    tags["есть_оффер"] = any(w in t for w in ("скидк", "бесплатн", "подарок", "акци"))
    tags["продающий_тип"] = ("продающий" if tags["есть_оффер"] or tags["есть_регистрационный_CTA"]
                             else "экспертный")
    if any(w in t for w in ("больно", "устал", "не получает", "проблема", "не работает")):
        tags["боль"] = "не видит результата"
    if any(w in t for w in ("страшно", "боишься", "страх")):
        tags["эмоциональный_триггер"] = "страх"
    elif any(w in t for w in ("удив", "шок", "вот это")):
        tags["эмоциональный_триггер"] = "удивление"
    llm = _call_llm(SYSTEM, "Проклассифицируй контент, верни строго JSON с ключами: "
                    f"тема, рубрика, тип, CTA, боль, сегмент, эмоциональный_триггер. Текст:\n{text[:1500]}")
    if llm:
        try:
            m = re.search(r"\{.*\}", llm, re.S)
            if m:
                tags.update({k: v for k, v in json.loads(m.group()).items() if v})
        except Exception:
            pass
    return tags


def detect_anomalies(history):
    """Математическое определение аномалий. history — список недельных срезов
    (по ТЗ: сравнение со средним последних 4 недель)."""
    out = []
    if len(history) < 2:
        return out
    cur = history[-1]
    base = history[:-1][-4:]
    def avg(lst, key):
        vals = [h["agg"].get(key) for h in lst if h["agg"].get(key) is not None]
        return sum(vals) / len(vals) if vals else None
    for key, thr, label in (("reach", -30, "охват"), ("views", -30, "просмотры"),
                            ("reach", 200, "охват (резкий рост)")):
        a, c = avg(base, key), cur["agg"].get(key)
        if a and c is not None:
            pct = (c - a) / a * 100
            if pct < thr if thr < 0 else pct > thr:
                out.append(f"{label}: {pct:+.0f}% к среднему за 4 недели ({fmt(a)} → {fmt(c)}). Вероятный фактор — требуется проверка.")
    err_cur = cur["ind"].get("ERR")
    errs = [h["ind"].get("ERR") for h in base if h["ind"].get("ERR") is not None]
    if err_cur and errs and (err_cur - sum(errs) / len(errs)) / (sum(errs) / len(errs)) * 100 < -20:
        out.append(f"ERR упал более чем на 20% ({sum(errs)/len(errs):.2f}% → {err_cur:.2f}%).")
    # аномальный вклад одного ролика
    items = cur.get("_top_content") or []
    if items and cur["agg"].get("reach"):
        best = max(items, key=lambda x: x.get("reach") or 0)
        share = (best.get("reach") or 0) / cur["agg"]["reach"] * 100
        if share > 60:
            out.append(f"Один материал дал {share:.0f}% недельного охвата: «{best['item'].title or best['item'].link}».")
    return out


def rule_based_report(payload, anomalies, manual_notes=""):
    """Детерминированный аналитик — только из переданных чисел."""
    d = payload["deltas"]
    ag = payload["agg"]
    ind = payload["ind"]
    L = []
    L.append("### Общая картина")
    for k, label, unit in (("reach", "Охваты", "%"), ("views", "Просмотры", "%"),
                           ("registrations", "Регистрации", "%"), ("ERR", "ERR", "п.п."),
                           ("net_growth", "Чистый прирост подписчиков", "abs")):
        v = d.get(k)
        if v and v.get("d") is not None:
            sign = "+" if v["d"] >= 0 else "−"
            if unit == "%":
                val = "%.0f%%" % abs(v["d"])
            elif unit == "pp":
                val = "%.2f pp" % abs(v["d"])
            else:
                val = fmt(abs(v["d"]))
            L.append(f"- {label}: {sign}{val} (с {fmt(v['prev'])} до {fmt(v['cur'])})")
        else:
            L.append(f"- {label}: недостаточно данных для сравнения")
    gc = payload.get("gc") or {}
    if gc:
        L.append("### Воронка GetCourse")
        L.append(f"- Заказы: {fmt(gc.get('orders'))}, оплаты: {fmt(gc.get('payments'))} "
                 f"на сумму {fmt(gc.get('payments_sum'))} руб. (детализация — на экране «GetCourse»)")
        L.append("")
    L.append("### Что дало рост / что ухудшилось")
    ups, downs = [], []
    for k, label in (("reach", "охват"), ("views", "просмотры"), ("registrations", "регистрации")):
        v = d.get(k)
        if v and v.get("d") is not None:
            (ups if v["d"] >= 0 else downs).append(f"{label} {v['d']:+.0f}%")
    if ups:
        L.append("- Рост: " + ", ".join(ups) + ".")
    if downs:
        L.append("- Падение: " + ", ".join(downs) + ".")
    L.append("")
    if anomalies:
        L.append("### Аномалии")
        L.extend("- " + a for a in anomalies)
        L.append("")
    items = payload.get("_top_content") or []
    if items:
        best = sorted([i for i in items if i.get("ERR") is not None], key=lambda x: x["ERR"], reverse=True)
        if best:
            b = best[0]
            tags = json.loads(b["item"].ai_tags or "{}")
            L.append("### Контент")
            L.append(f"- Лучший по ERR материал: «{b['item'].title or b['item'].link}» "
                     f"(ERR {b['ERR']:.2f}%, охват {fmt(b['reach'])}, рубрика: {tags.get('рубрика') or 'н/д'}).")
        cmp_text = payload.get("_compare_text")
        if cmp_text:
            L.append("- Что объединяет лучшие материалы (vs худшие 10):")
            L.append(cmp_text)
        L.append("")
    if payload.get("_comments_text"):
        L.append("### Голоса аудитории (комментарии)")
        L.append(payload["_comments_text"])
        L.append("")
    L.append("### Рекомендации")
    L.append("**УСИЛИТЬ:** форматы и рубрики из ТОП-10 по ERR и регистрациям (см. экран «Контент»).")
    L.append("**ИЗМЕНИТЬ:** материалы из худших 10 — проверить первые 3 секунды, CTA, длительность.")
    L.append("**УБРАТЬ/СОКРАТИТЬ:** рубрики с охватом ниже среднего 4 недели подряд.")
    if manual_notes:
        L.append("")
        L.append("### Ручной контекст периода")
        L.append(manual_notes)
    return "\n".join(L)


def generate_weekly_report_text(payload, anomalies, manual_notes=""):
    data = {k: v for k, v in payload.items() if not k.startswith("_")}
    llm = _call_llm(SYSTEM, (
        "Сформируй недельный отчет для руководителя по данным JSON.\n"
        "Структура: Общая картина; Что дало рост; Что ухудшилось; Аномалии; "
        "Голоса аудитории (если есть); Рекомендации в трёх блоках: УСИЛИТЬ / ИЗМЕНИТЬ / УБРАТЬ.\n"
        + (f"Ручной контекст (учти его и не противоречь ему):\n{manual_notes}\n" if manual_notes else "")
        + (f"Сравнение лучших и худших материалов (из данных):\n{payload.get('_compare_text') or 'нет'}\n"
           if payload.get("_compare_text") else "")
        + (f"Сводка комментариев (из данных):\n{payload.get('_comments_text')}\n"
           if payload.get("_comments_text") else "")
        + f"Аномалии, найденные математически:\n{json.dumps(anomalies, ensure_ascii=False)}\n"
        + f"Данные:\n{json.dumps(data, ensure_ascii=False)}"))
    return llm or rule_based_report(payload, anomalies, manual_notes)


def generate_monthly_report_text(payload, channel_payloads, anomalies, manual_notes=""):
    llm = _call_llm(SYSTEM, (
        "Сформируй ГЛУБОКИЙ месячный AI-отчет. Разделы: итог месяца; сравнение с прошлым месяцем; "
        "динамика каждого канала и его вклад; эффективность единицы контента; лучшие/худшие рубрики, "
        "темы, форматы, CTA; изменения поведения аудитории; рекомендации на следующий месяц; "
        "и раздел «Что произошло за месяц простыми словами» — 5-10 главных выводов.\n"
        + (f"Ручной контекст:\n{manual_notes}\n" if manual_notes else "")
        + (f"Сравнение лучших и худших материалов (из данных):\n{payload.get('_compare_text') or 'нет'}\n"
           if payload.get("_compare_text") else "")
        + (f"Сводка комментариев (из данных):\n{payload.get('_comments_text')}\n"
           if payload.get("_comments_text") else "")
        + "Обязательно заверши разделом «Что произошло за месяц простыми словами» — 5-10 выводов.\n"
        + f"Данные по всем каналам:\n{json.dumps({k: {x: y for x, y in v.items() if not x.startswith('_')} for k, v in channel_payloads.items()}, ensure_ascii=False)}\n"
        + f"Аномалии:\n{json.dumps(anomalies, ensure_ascii=False)}"))
    if llm:
        return llm
    text = rule_based_report(payload, anomalies, manual_notes)
    text += "\n\n### Динамика по каналам\n"
    ranked = []
    for name, p in channel_payloads.items():
        r = p["deltas"].get("reach")
        pct = f"{r['d']:+.0f}%" if r and r.get("d") is not None else "н/д"
        text += f"- {name}: охват {pct}\n"
        if r and r.get("d") is not None:
            ranked.append((r["d"], name, r["cur"]))
    text += "\n### Что произошло за месяц простыми словами\n"
    for line in month_plain_summary(payload, ranked):
        text += line + "\n"
    return text


def month_plain_summary(payload, ranked_reach=None):
    """5-10 главных выводов месяца, каждый — только из рассчитанных чисел."""
    out = []
    ag = payload["agg"]
    ind = payload["ind"]
    d = payload["deltas"]
    for k, label in (("reach", "охват"), ("registrations", "регистрации")):
        v = d.get(k)
        if v and v.get("d") is not None:
            out.append(f"{label.capitalize()}: {v['d']:+.0f}% "
                       f"({fmt(v['prev'])} → {fmt(v['cur'])}).")
    if ranked_reach:
        top = sorted(ranked_reach, reverse=True)
        if top:
            out.append(f"Главный драйвер охвата — {top[0][1]} "
                       f"({fmt(top[0][2])} охвата за месяц).")
        neg = [x for x in top if x[0] < -10]
        if neg:
            out.append("Просели: " + ", ".join(f"{n} ({p:+.0f}%)" for p, n, _ in neg[:3]) + ".")
    items = payload.get("_top_content") or []
    if items:
        by_reach = sorted(items, key=lambda x: x.get("reach") or 0, reverse=True)
        if by_reach and ag.get("reach"):
            share = (by_reach[0].get("reach") or 0) / ag["reach"] * 100
            out.append(f"Самый охватный материал месяца: «{(by_reach[0]['item'].title or '')[:60]}» — "
                       f"{share:.0f}% месячного охвата.")
    gc = payload.get("gc") or {}
    if gc.get("payments"):
        out.append(f"Оплачено заказов: {fmt(gc.get('payments'))} на сумму {fmt(gc.get('payments_sum'))} руб.")
    if ind.get("CV_reach") is not None:
        out.append(f"CV из охвата в регистрацию: {ind['CV_reach']:.3f}%.")
    if ag.get("subscribed") is not None:
        out.append(f"Чистый прирост подписчиков: {fmt((ag.get('subscribed') or 0) - (ag.get('unsubscribed') or 0))}.")
    if payload.get("_comments_text"):
        out.append("Главная боль аудитории в комментариях — см. блок «Голоса аудитории» выше.")
    if not out:
        out.append("Недостаточно данных для вывода.")
    return out[:10]


def controller_check(payload, report_text):
    """AI-контролёр качества: сверяет текст отчета с рассчитанными числами.
    Возвращает список найденных несоответствий."""
    issues = []
    d = payload["deltas"]
    reg = d.get("registrations")
    if reg and reg.get("prev") and reg["prev"] > 1000 and reg.get("d") is not None and reg["d"] > 250:
        issues.append(f"Заявлен рост регистраций {reg['d']:+.0f}%, но абсолюты {fmt(reg['prev'])} → {fmt(reg['cur'])} — проверить формулировку отчета.")
    ag = payload["agg"]
    for k in ("views", "reach", "likes", "comments"):
        v = ag.get(k)
        if v is not None and isinstance(v, str):
            issues.append(f"Метрика {k} имеет строковый формат вместо числа.")
    if ag.get("followers_end") is not None and ag.get("subscribed") is not None:
        diff = (ag.get("followers_end") or 0) - (ag.get("followers_start") or 0)
        net = (ag.get("subscribed") or 0) - (ag.get("unsubscribed") or 0)
        if net and abs(diff - net) > max(50, abs(net) * 0.5):
            issues.append(f"Подписчики изменились на {fmt(diff)}, но чистый прирост из подписок/отписок {fmt(net)} — данные не сходятся.")
    if payload.get("_prev_agg") and payload["agg"].get("reach") is not None:
        if payload["_prev_agg"].get("reach") and payload["agg"]["reach"] < payload["_prev_agg"]["reach"] * 0.25:
            issues.append("Охват периода в 4+ раза ниже предыдущего — проверить расчёт/полноту данных.")
    llm = _call_llm(SYSTEM + " Ты контролёр качества. Найди несоответствия между текстом отчета и числами.",
                    f"Отчёт:\n{report_text}\n\nДанные:\n{json.dumps({k: v for k, v in payload.items() if not str(k).startswith('_')}, ensure_ascii=False)}")
    if llm and "несоответств" in llm.lower():
        issues.append("AI-контролёр: " + llm[:500])
    if not issues:
        return "Проверка пройдена: несоответствий не найдено."
    return "\n".join("⚠ " + i for i in issues)
