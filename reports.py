# -*- coding: utf-8 -*-
"""Формирование недельных и месячных отчётов."""
import json
from datetime import date, timedelta
from db import db, Report, Notification, ManualNote, Channel
import calc
import ai_analyst


def weekly_history(weeks=6, until=None):
    """Последние N недельных срезов (для аномалий и трендов)."""
    until = until or date.today()
    end = calc.week_bounds(until)[1]
    out = []
    for i in range(weeks):
        e = end - timedelta(days=7 * i)
        s = e - timedelta(days=6)
        out.append(calc.period_report(s, e))
    return list(reversed(out))


def generate_report(rtype="weekly", anchor=None):
    anchor = anchor or date.today()
    if rtype == "weekly":
        start, end = calc.week_bounds(anchor)
    else:
        start, end = calc.month_bounds(anchor)
    prev = Report.query.filter_by(rtype=rtype, start=start, end=end).first()

    payload = calc.period_report(start, end)
    content = calc.content_stats_for_period(start, end)
    payload["_top_content"] = content

    hist = weekly_history(6, end)
    anomalies = ai_analyst.detect_anomalies(hist)
    notes = ManualNote.query.filter(ManualNote.period_start <= end, ManualNote.period_end >= start).all()
    manual = "\n".join(f"[{n.period_start}–{n.period_end}] продукт: {n.product or '—'}; цель: {n.goal or '—'}; "
                       f"KPI: {n.kpi or '—'}; события: {n.events or '—'}" for n in notes)

    if rtype == "weekly":
        text = ai_analyst.generate_weekly_report_text(payload, anomalies, manual)
    else:
        chans = {}
        for ch in Channel.query.filter_by(is_active=True).all():
            chans[ch.name] = calc.period_report(start, end, ch.id)
        text = ai_analyst.generate_monthly_report_text(payload, chans, anomalies, manual)

    # AI-контролёр перед «отправкой»
    payload["_prev_agg"] = None
    controller = ai_analyst.controller_check(payload, text)

    body = {"payload": {k: v for k, v in payload.items() if k != "_prev_agg"}}
    rep = prev or Report(rtype=rtype, start=start, end=end)
    rep.payload = json.dumps(body, ensure_ascii=False, default=str)
    rep.ai_text = text
    rep.controller_text = controller
    db.session.add(rep)
    for a in anomalies:
        db.session.add(Notification(level="warn", message=f"Аномалия ({start}–{end}): {a}"))
    db.session.add(Notification(level="info", message=f"Отчёт за {start}–{end} сформирован."))
    db.session.commit()
    return rep
