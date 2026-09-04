# -*- coding: utf-8 -*-
import io, json, os, threading
from datetime import date, datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, g, session

from db import db, Channel, MetricSnapshot, ContentItem, ContentStat, Registration, \
    ManualNote, Report, Notification, Setting, RunLog, get_setting, set_setting, \
    Comment, GcOrder, GcPayment, Spend
import calc, connectors, reports, seed, getcourse, comments as comments_mod, livedune

IS_SERVERLESS = bool(os.environ.get("VERCEL"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me")
_default_db = os.environ.get("DATABASE_URL") or ("sqlite:////tmp/smm.db" if IS_SERVERLESS else "sqlite:///smm.db")
app.config["SQLALCHEMY_DATABASE_URI"] = _default_db.replace("postgres://", "postgresql://")
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
db.init_app(app)


@app.template_filter("fromjson")
def _fromjson(s):
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


@app.template_filter("numfmt")
def _numfmt(v):
    """1234567 -> '1 234 567'; None/отсутствует -> 'н/д'."""
    try:
        return f"{float(v):,.0f}".replace(",", " ")
    except Exception:
        return "н/д"


@app.template_filter("mdfmt")
def _mdfmt(text):
    """Мини-markdown для AI-текстов: заголовки, списки, жирный."""
    import html as _h
    import re as _re
    if not text:
        return ""
    esc = _h.escape(str(text))
    esc = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
    out, in_list = [], False
    for line in esc.split("\n"):
        l = line.strip()
        if l.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h4>{l[4:]}</h4>")
        elif l.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{l[3:]}</h3>")
        elif l.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{l[2:]}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            if l:
                out.append(f"<p>{l}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


@app.template_filter("pctfmt")
def _pctfmt(v, nd=2):
    try:
        return (f"{float(v):.{nd}f}%").replace(".", ",")
    except Exception:
        return "н/д"


@app.route("/reports/view/<int:id>")
def report_view(id):
    return render_template("report_view.html", r=Report.query.get_or_404(id))


@app.context_processor
def inject_globals():
    from flask import request as _req
    import time as _time
    now = _time.time()
    hc = app.config.get("_HEALTH_CACHE")
    if hc and now - hc[0] < 60:
        health_data = hc[1]
    else:
        health_data = _compute_health()
        app.config["_HEALTH_CACHE"] = (now, health_data)
    unread = Notification.query.filter_by(is_read=False).count()
    period = None
    try:
        s, e = _req.args.get("start"), _req.args.get("end")
        if s and e:
            period = (s, e)
    except Exception:
        pass
    if not period:
        import calc as _calc
        period = _calc.week_bounds(date.today())
    period2 = period
    render_ms = None
    try:
        from flask import g as _g2
        import time as _t2
        render_ms = int((_t2.time() - _g2._t0) * 1000)
    except Exception:
        pass
    return {"channels": Channel.query.filter_by(is_active=True).all(),
            "unread_notifications": unread, "period": period2, "health": health_data,
            "render_ms": render_ms}


def _compute_health():
    """Панель здоровья (кэшируется на 60с)."""
    health = {}
    try:
        from datetime import timedelta as _td
        yesterday = date.today() - _td(days=1)
        from sqlalchemy import func as _f
        rows = db.session.query(MetricSnapshot.channel_id, MetricSnapshot.metric,
                                MetricSnapshot.status,
                                _f.max(MetricSnapshot.fetched_at)).filter(
            MetricSnapshot.date == yesterday).group_by(
            MetricSnapshot.channel_id, MetricSnapshot.metric).all()
        health["missing"] = sum(1 for r in rows if r[2] == "MISSING")
        last_collect = RunLog.query.filter(RunLog.kind == "daily_collect") \
            .order_by(RunLog.started_at.desc()).first()
        health["collect"] = last_collect.started_at.strftime("%d.%m %H:%M") if last_collect else "нет"
        relay = RunLog.query.filter(RunLog.kind == "gc_relay") \
            .order_by(RunLog.started_at.desc()).first()
        health["relay"] = (relay.started_at.strftime("%d.%m %H:%M") + " (" + (relay.details or "") + ")") if relay else "нет"
        ld = RunLog.query.filter(RunLog.kind == "livedune_sync")             .order_by(RunLog.started_at.desc()).first()
        health["livedune"] = (ld.started_at.strftime("%d.%m %H:%M") + " (" + (ld.details or "") + ")") if ld else "нет"
        health["gc_orders"] = GcOrder.query.count()
        health["comments"] = Comment.query.count()
    except Exception:
        pass
    return health


# ---------------- Дашборд: 5 экранов ----------------

_pr_cache = {}


def cached_period_report(start, end, channel_id=None):
    """Кэш расчётов периода на время запроса (экран «Каналы» экономит ~100 запросов)."""
    key = (str(start), str(end), channel_id)
    if key not in _pr_cache:
        _pr_cache[key] = calc.period_report(start, end, channel_id)
        if len(_pr_cache) > 64:
            _pr_cache.clear()
    return _pr_cache[key]


@app.route("/")
def overview():
    """Экран 1. Всё вместе — основные KPI всей системы."""
    d = _period_from_args()
    p = calc.period_report(*d)
    chart = _chart_series(*d)
    return render_template("overview.html", p=p, period=d, chart=chart,
                           report=_latest_report("weekly"),
                           trends=calc.weekly_series(8),
                           growth=calc.growth_points(*d),
                           forecast=calc.month_forecast())


@app.route("/comments")
def comments_screen():
    """Экран 6. Комментарии: вопросы, боли, возражения, идеи (по ТЗ п.4)."""
    d = _period_from_args()
    dig = comments_mod.digest(*d)
    recent = Comment.query.order_by(Comment.date.desc(), Comment.id.desc()).limit(50).all()
    # тренд тональности: последние 8 недель по типам
    from collections import defaultdict as _dd
    from sqlalchemy import func as _f
    from datetime import timedelta as _td
    trend = {"labels": [], "series": _dd(list)}
    end_w = calc.week_bounds(date.today())[1]
    rows = db.session.query(Comment.main_type, Comment.date, _f.count(Comment.id)).filter(
        Comment.date >= end_w - _td(days=55)).group_by(Comment.main_type, Comment.date).all()
    weekly = _dd(lambda: _dd(int))
    for mt, dt_, n in rows:
        wk = calc.week_bounds(dt_)[1]
        weekly[wk][mt] += n
    for wk in sorted(weekly):
        trend["labels"].append(wk.strftime("%d.%m"))
        seen_types = set()
        for mt, n in weekly[wk].items():
            trend["series"][mt].append(n)
            seen_types.add(mt)
        for mt in list(trend["series"]):
            if mt not in seen_types:
                trend["series"][mt].append(0)
    trend["series"] = dict(trend["series"])
    return render_template("comments.html", d=dig, recent=recent, period=d,
                           ai_summary=get_setting("comments_ai_summary"),
                           trend=trend)


@app.route("/comments/ai_summary", methods=["POST"])
def comments_ai_summary():
    """AI-вывод по комментариям за период (Gemini/эвристика — только из данных)."""
    import ai_analyst
    d = _period_from_args()
    dig = comments_mod.digest(*d)
    if not dig["total"]:
        flash("Комментариев за период нет — сначала импортируйте.")
        return redirect(url_for("comments_screen"))
    text = ai_analyst._call_llm(ai_analyst.SYSTEM,
        "Ты SMM-аналитик. Ниже — статистика комментариев за период (только факты из данных). "
        "Напиши короткий вывод: 3 главных боли/вопроса аудитории, 2 идеи для контента, "
        "1 предупреждение (если есть негатив). Не выдумывай числа.\n"
        + comments_mod.digest_text(dig))
    set_setting("comments_ai_summary", text or comments_mod.digest_text(dig))
    db.session.commit()
    flash("AI-вывод по комментариям обновлён" + (" (LLM)." if text else " (эвристически — LLM недоступна)."))
    return redirect(url_for("comments_screen"))


@app.route("/comments/import", methods=["POST"])
def comments_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Файл не выбран")
    else:
        n = comments_mod.import_csv(f)
        flash(f"Импортировано и классифицировано комментариев: {n}")
    return redirect(url_for("comments_screen"))


@app.route("/channels")
def channels_screen():
    """Экран 2. Каналы — сравнение 11 аккаунтов + динамика к прошлому периоду."""
    d = _period_from_args()
    rows = []
    days_n = max((d[1] - d[0]).days + 1, 1)
    for ch in Channel.query.filter_by(is_active=True).all():
        p = cached_period_report(*d, ch.id)
        rows.append({"ch": ch, "p": p, "avg_reach": (p["agg"].get("reach") or 0) / days_n})
    if request.args.get("export") == "csv":
        import csv as _csv
        out = io.StringIO()
        w = _csv.writer(out)
        w.writerow(["канал", "платформа", "подписчики", "прирост", "охват", "просмотры",
                    "взаимодействия", "ERR %", "ER %", "CV %", "регистрации"])
        for r in rows:
            p = r["p"]
            w.writerow([r["ch"].name, r["ch"].platform, p["agg"].get("followers_end"),
                        p["ind"].get("net_growth"), p["agg"].get("reach"), p["agg"].get("views"),
                        p["ind"].get("interactions"), p["ind"].get("ERR"), p["ind"].get("ER"),
                        p["ind"].get("CV_reach"), p["registrations"]])
        return Response("﻿" + out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=channels.csv"})
    chart = {"labels": [r["ch"].name for r in rows],
             "reach": [r["p"]["agg"].get("reach") or 0 for r in rows],
             "regs": [r["p"]["registrations"] for r in rows]}
    return render_template("channels.html", rows=rows, period=d, chart=chart)


@app.route("/content")
def content_screen():
    """Экран 3. Контент — все материалы и их эффективность."""
    d = _period_from_args()
    items = calc.content_stats_for_period(*d)
    f_platform = request.args.get("platform")
    f_format = request.args.get("format")
    f_rubric = request.args.get("rubric")
    f_account = request.args.get("account")
    f_type = request.args.get("type")      # продающий/экспертный/...
    f_theme = request.args.get("theme")
    f_search = (request.args.get("q") or "").strip().lower()
    preset = request.args.get("preset")    # best/worst

    def match(it):
        ci = it["item"]
        tags = json.loads(ci.ai_tags or "{}")
        if f_platform and ci.channel.platform != f_platform: return False
        if f_format and ci.format != f_format: return False
        if f_rubric and tags.get("рубрика") != f_rubric: return False
        if f_account and str(ci.channel_id) != f_account: return False
        if f_type and tags.get("продающий_тип") != f_type: return False
        if f_theme and f_theme.lower() not in (tags.get("тема") or "").lower(): return False
        if f_search and f_search not in ((ci.title or "") + (ci.text or "")).lower(): return False
        return True

    items = [i for i in items if match(i)]
    sort = request.args.get("sort", "reach")
    items.sort(key=lambda x: (x.get(sort) if x.get(sort) is not None else -1), reverse=True)
    if preset == "best":
        items = sorted([i for i in items if i.get("ERR") is not None],
                       key=lambda x: x["ERR"], reverse=True)[:10]
    elif preset == "worst":
        items = sorted([i for i in items if i.get("ERR") is not None],
                       key=lambda x: x["ERR"])[:10]
    compare_text, best, flop = calc.compare_best_worst(items if not preset else
                                                       calc.content_stats_for_period(*d), "ERR", 10)
    rubrics = sorted({t.get("рубрика") for t in (json.loads(i["item"].ai_tags or "{}") for i in items) if t.get("рубрика")})
    types = sorted({t.get("продающий_тип") for t in (json.loads(i["item"].ai_tags or "{}") for i in items) if t.get("продающий_тип")})
    if request.args.get("export") == "csv":
        import csv as _csv
        out = io.StringIO()
        w = _csv.writer(out)
        w.writerow(["дата", "канал", "формат", "название", "рубрика", "охват", "просмотры",
                    "ERR%", "сохранения", "репосты", "подписки", "регистрации", "CV%"])
        for i in items:
            tags = json.loads(i["item"].ai_tags or "{}")
            w.writerow([i["item"].published_at, i["item"].channel.name, i["item"].format,
                        i["item"].title, tags.get("рубрика", ""), i["reach"], i["views"],
                        round(i["ERR"], 2) if i.get("ERR") is not None else "",
                        i["saves"], i["shares"], i["subs"], i["registrations"],
                        round(i["CV"], 3) if i.get("CV") is not None else ""])
        return Response("﻿" + out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=content.csv"})
    # лучшее время публикации: средний охват по дням недели и часам (из данных)
    wd_stats, hr_stats = {}, {}
    for i in items:
        pt = i["item"].published_at
        if not pt:
            continue
        wd = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][pt.weekday()]
        for agg, key in ((wd_stats, wd), (hr_stats, f"{pt.hour:02d}")):
            a = agg.setdefault(key, {"n": 0, "reach": 0})
            a["n"] += 1
            a["reach"] += i.get("reach") or 0
    for agg in (wd_stats, hr_stats):
        for v in agg.values():
            v["avg"] = v["reach"] / v["n"] if v["n"] else 0
    best_time = {"wd": sorted(wd_stats.items(), key=lambda x: -x[1]["avg"]),
                 "hr": sorted(hr_stats.items(), key=lambda x: -x[1]["avg"])}
    # хэштеги: топ по среднему охвату и количеству (фича Sprout/Talkwalker)
    import re as _re
    tags = {}
    for i in items:
        for t in set(_re.findall(r"#([A-Za-zА-Яа-я0-9_]{2,30})", (i["item"].text or "") + " " + (i["item"].title or ""))):
            a = tags.setdefault(t.lower(), {"n": 0, "reach": 0, "regs": 0})
            a["n"] += 1
            a["reach"] += i.get("reach") or 0
            a["regs"] += i.get("registrations") or 0
    for v in tags.values():
        v["avg"] = v["reach"] / v["n"] if v["n"] else 0
    hashtags = sorted(tags.items(), key=lambda x: -x[1]["avg"])[:15]
    by_rubric, by_format = {}, {}
    for i in items:
        tags = json.loads(i["item"].ai_tags or "{}")
        for key, agg in ((tags.get("рубрика") or "—", by_rubric), (i["item"].format or "—", by_format)):
            a = agg.setdefault(key, {"n": 0, "reach": 0, "regs": 0, "err": []})
            a["n"] += 1
            a["reach"] += i.get("reach") or 0
            a["regs"] += i.get("registrations") or 0
            if i.get("ERR") is not None:
                a["err"].append(i["ERR"])
    for agg in (by_rubric, by_format):
        for v in agg.values():
            v["avg_reach"] = v["reach"] / v["n"] if v["n"] else 0
            v["avg_err"] = sum(v["err"]) / len(v["err"]) if v["err"] else None
    return render_template("content.html", items=items[:200], period=d, sort=sort,
                           platforms=sorted({i["item"].channel.platform for i in items}),
                           formats=sorted({i["item"].format for i in items if i["item"].format}),
                           rubrics=rubrics, types=types, compare=compare_text,
                           preset=preset, by_rubric=by_rubric, by_format=by_format,
                           best_time=best_time, hashtags=hashtags,
                           total_matched=len(items))


def _regs_with_time(q):
    """Если задано время границ — фильтруем регистрации по точному времени."""
    sdt, edt = _period_datetimes()
    if sdt and edt:
        with_time = Registration.query.filter(Registration.created_at.isnot(None))
        if with_time.count():
            return q.filter(Registration.created_at >= sdt, Registration.created_at < edt)
    return q


@app.route("/registrations")
def registrations_screen():
    """Экран 4. Регистрации: источник → канал → CV."""
    d = _period_from_args()
    q = Registration.query.filter(Registration.date >= d[0], Registration.date <= d[1],
                                  Registration.status == "OK",
                                  ~Registration.utm_source.like("demo_%"))
    q = _regs_with_time(q)
    by_source = {}
    for r in q.all():
        s = by_source.setdefault(r.utm_source or "(нет)", {"count": 0, "landings": {}})
        s["count"] += r.count or 0
        s["landings"][r.landing or "—"] = s["landings"].get(r.landing or "—", 0) + (r.count or 0)
    total = sum(v["count"] for v in by_source.values())
    cv_rows = []
    agg, _ = calc.aggregate(*d)
    total_reach = agg.get("reach")
    # охват каждой платформы одним группирующим запросом (было 11 агрегатов)
    from sqlalchemy import func as _f
    reach_by_platform = {}
    for pid, rsum in db.session.query(Channel.id, _f.coalesce(_f.sum(MetricSnapshot.value), 0)).join(
            MetricSnapshot, MetricSnapshot.channel_id == Channel.id).filter(
            MetricSnapshot.date >= d[0], MetricSnapshot.date <= d[1],
            MetricSnapshot.metric == "reach", MetricSnapshot.value.isnot(None),
            Channel.is_competitor == False).group_by(Channel.id).all():  # noqa
        ch = Channel.query.get(pid)
        if ch:
            reach_by_platform[ch.platform] = reach_by_platform.get(ch.platform, 0) + rsum
    for src, v in sorted(by_source.items(), key=lambda x: -x[1]["count"]):
        plat = calc.UTM_TO_PLATFORM.get(src.lower())
        ch_reach = reach_by_platform.get(plat) if plat else None
        cv_rows.append({"source": src, "platform": plat or "—", "count": v["count"],
                        "share": v["count"] / total * 100 if total else None,
                        "reach": ch_reach, "CV": (v["count"] / ch_reach * 100) if ch_reach else None})
    if request.args.get("export") == "csv":
        import csv as _csv
        out = io.StringIO()
        w = _csv.writer(out)
        w.writerow(["utm_source", "utm_medium", "utm_campaign", "регистрации", "охват канала", "CV %"])
        for r in cv_rows:
            w.writerow([r["source"], "", "", r["count"], r["reach"] or "",
                        round(r["CV"], 3) if r["CV"] is not None else ""])
        return Response("﻿" + out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=registrations.csv"})
    import utm as utm_mod
    sdt, edt = _period_datetimes()
    br = utm_mod.breakdown(d[0], d[1], sdt, edt)
    return render_template("registrations.html", rows=cv_rows, total=total,
                           total_reach=total_reach,
                           total_cv=total / total_reach * 100 if total_reach else None, period=d,
                           chart=_chart_series(*d), utm=br)


@app.route("/ai")
def ai_screen():
    """Экран 5. AI-аналитика."""
    weekly = Report.query.filter_by(rtype="weekly").order_by(Report.end.desc()).all()
    monthly = Report.query.filter_by(rtype="monthly").order_by(Report.end.desc()).all()
    plans = Report.query.filter_by(rtype="plan").order_by(Report.end.desc()).limit(4).all()
    anomalies = [n for n in Notification.query.filter(Notification.level == "warn").order_by(Notification.created_at.desc()).limit(20)]
    model = get_setting("ai_model") if get_setting("ai_api_key") else ""
    return render_template("ai.html", weekly=weekly, monthly=monthly, anomalies=anomalies,
                           plans=plans, values_model=model)


# ---------------- Отчёты и операции ----------------

@app.route("/reports")
def reports_list():
    reps = Report.query.order_by(Report.end.desc()).limit(50).all()
    return render_template("reports.html", reports=reps)


@app.route("/reports/generate", methods=["POST"])
def generate():
    rtype = request.form.get("rtype", "weekly")
    anchor = datetime.strptime(request.form.get("anchor", date.today().isoformat()), "%Y-%m-%d").date()
    rep = reports.generate_report(rtype, anchor)
    flash(f"Отчёт за {rep.start}–{rep.end} сформирован.")
    return redirect(url_for("reports_list"))


@app.route("/compare")
def compare_screen():
    """Сравнение двух произвольных периодов: итоги системы + по каналам."""
    def _d(name, default=None):
        v = request.args.get(name)
        try:
            return datetime.strptime(v, "%Y-%m-%d").date() if v else default
        except ValueError:
            return default
    from datetime import timedelta as _td
    today = date.today()
    a_end_d = _d("a2", today)
    a_start_d = _d("a1", a_end_d - _td(days=6))
    b_end_d = _d("b2", a_start_d - _td(days=1))
    b_start_d = _d("b1", b_end_d - _td(days=(a_end_d - a_start_d).days))
    pa = cached_period_report(a_start_d, a_end_d)
    pb = cached_period_report(b_start_d, b_end_d)

    def pct(cur, prev):
        if cur is None or prev in (None, 0):
            return None
        return (cur - prev) / prev * 100

    def fmt(v, kind="num"):
        if v is None:
            return "н/д"
        if kind == "pct":
            return f"{v:.2f}%"
        if kind == "money":
            return f"{v:,.0f} ₽".replace(",", " ")
        return f"{v:,.0f}".replace(",", " ")

    fields = [
        ("Охват", pa["agg"].get("reach"), pb["agg"].get("reach"), "num"),
        ("Просмотры", pa["agg"].get("views"), pb["agg"].get("views"), "num"),
        ("Взаимодействия", pa["ind"].get("interactions"), pb["ind"].get("interactions"), "num"),
        ("Регистрации", pa["registrations"], pb["registrations"], "num"),
        ("ERR", pa["ind"].get("ERR"), pb["ind"].get("ERR"), "pct"),
        ("ER", pa["ind"].get("ER"), pb["ind"].get("ER"), "pct"),
        ("CV из охвата", pa["ind"].get("CV_reach"), pb["ind"].get("CV_reach"), "pct"),
        ("Подписчики (конец)", pa["agg"].get("followers_end"), pb["agg"].get("followers_end"), "num"),
        ("Чистый прирост", pa["ind"].get("net_growth"), pb["ind"].get("net_growth"), "num"),
        ("Заказы", (pa.get("gc") or {}).get("orders"), (pb.get("gc") or {}).get("orders"), "num"),
        ("Оплаты, сумма", (pa.get("gc") or {}).get("payments_sum"), (pb.get("gc") or {}).get("payments_sum"), "money"),
    ]
    total_rows = [{"name": n, "a": fmt(x, k), "b": fmt(y, k), "d": pct(x, y)} for n, x, y, k in fields]
    channel_rows = []
    for ch in Channel.query.filter_by(is_active=True).all():
        ra = cached_period_report(a_start_d, a_end_d, ch.id)
        rb = cached_period_report(b_start_d, b_end_d, ch.id)
        channel_rows.append({
            "ch": ch,
            "reach_a": ra["agg"].get("reach"), "reach_b": rb["agg"].get("reach"),
            "reach_d": pct(ra["agg"].get("reach"), rb["agg"].get("reach")),
            "regs_a": ra["registrations"], "regs_b": rb["registrations"],
            "err_a": ra["ind"].get("ERR"), "err_b": rb["ind"].get("ERR"),
        })
    return render_template("compare.html",
                           a={"start": a_start_d, "end": a_end_d},
                           b={"start": b_start_d, "end": b_end_d},
                           total_rows=total_rows, channel_rows=channel_rows)


@app.route("/competitors")
def competitors_screen():
    """Бенчмаркинг: наши каналы vs конкуренты (конкуренты исключены из общих итогов)."""
    d = _period_from_args()
    ours = [r for r in [{"ch": c, "p": cached_period_report(*d, c.id)}
                        for c in Channel.query.filter_by(is_active=True, is_competitor=False)]]
    comps = [r for r in [{"ch": c, "p": cached_period_report(*d, c.id)}
                         for c in Channel.query.filter_by(is_competitor=True)]]
    if request.method == "POST":
        pass
    return render_template("competitors.html", ours=ours, comps=comps, period=d)


@app.route("/competitors/add", methods=["POST"])
def competitors_add():
    from db import Channel as _C
    name = (request.form.get("name") or "").strip()
    platform = (request.form.get("platform") or "instagram").strip()
    ld = request.form.get("ld_account_id", "").strip()
    if name:
        ch = _C(platform=platform, name=name, url=request.form.get("url") or "",
                is_competitor=True,
                ld_account_id=int(ld) if ld.isdigit() else None)
        db.session.add(ch)
        db.session.commit()
        schedule_rewarm()
        flash(f"Конкурент «{name}» добавлен" + (" и будет синхронизироваться с LiveDune." if ld.isdigit() else ". Для автосборки укажите его id аккаунта в LiveDune (экран LiveDune в кабинете)."))
    return redirect(url_for("competitors_screen"))


@app.route("/competitors/remove", methods=["POST"])
def competitors_remove():
    ch = Channel.query.get(int(request.form.get("id") or 0))
    if ch and ch.is_competitor:
        ch.is_active = False
        db.session.commit()
        schedule_rewarm()
        flash("Конкурент скрыт.")
    return redirect(url_for("competitors_screen"))


@app.route("/api/ld_map")
def ld_map_endpoint():
    """Карта ld_id -> channel_id для релея (обновляется при добавлении конкурентов)."""
    token = os.environ.get("INGEST_TOKEN") or get_setting("ingest_token")
    if request.args.get("token") != token:
        return jsonify({"error": "invalid token"}), 403
    m = {str(k): v for k, v in livedune.DEFAULT_LD_MAP.items()}
    raw = get_setting("livedune_map", "")
    if raw:
        try:
            m.update({str(k): int(v) for k, v in json.loads(raw).items()})
        except Exception:
            pass
    for c in Channel.query.filter(Channel.ld_account_id.isnot(None)):
        m[str(c.ld_account_id)] = c.id
    return jsonify(m)


def _metric_value(metric, start, end):
    """Фактическое значение метрики за период (для целей и гипотез)."""
    from datetime import timedelta as _td
    p = cached_period_report(start, end)
    key = {"reach": "reach", "views": "views"}.get(metric)
    if key:
        return p["agg"].get(key)
    if metric == "regs":
        return p["registrations"]
    if metric == "err":
        return p["ind"].get("ERR")
    if metric == "cv":
        return p["ind"].get("CV_reach")
    if metric == "payments":
        from db import GcPayment
        from sqlalchemy import func as _f
        return db.session.query(_f.coalesce(_f.sum(GcPayment.amount), 0)).filter(
            GcPayment.date >= start, GcPayment.date <= end,
            GcPayment.status == "accepted").scalar()
    return None


METRIC_LABELS = {"reach": "Охват", "views": "Просмотры", "regs": "Регистрации",
                 "err": "ERR, %", "cv": "CV, %", "payments": "Оплаты, ₽"}


@app.route("/goals", methods=["GET", "POST"])
def goals_screen():
    from db import Goal
    if request.method == "POST":
        if request.form.get("delete"):
            g = Goal.query.get(int(request.form["delete"]))
            if g:
                db.session.delete(g)
                db.session.commit()
            return redirect(url_for("goals_screen"))
        from datetime import datetime as _dt
        try:
            db.session.add(Goal(
                metric=request.form.get("metric", "reach"),
                target=float(str(request.form.get("target", "0")).replace(" ", "").replace(",", ".")),
                start=_dt.strptime(request.form.get("start"), "%Y-%m-%d").date(),
                end=_dt.strptime(request.form.get("end"), "%Y-%m-%d").date(),
                note=request.form.get("note", "")))
            db.session.commit()
            flash("Цель добавлена.")
        except Exception as e:
            flash(f"Ошибка: {e}")
        return redirect(url_for("goals_screen"))
    goals = []
    for g in Goal.query.order_by(Goal.end.desc()).limit(30):
        cur = _metric_value(g.metric, g.start, min(g.end, date.today()))
        pct = (cur / g.target * 100) if (cur is not None and g.target) else None
        goals.append({"g": g, "cur": cur, "pct": pct,
                      "label": METRIC_LABELS.get(g.metric, g.metric)})
    return render_template("goals.html", goals=goals, period=calc.week_bounds(date.today()),
                           labels=METRIC_LABELS)


@app.route("/spends", methods=["GET", "POST"])
def spends_screen():
    """Расходы на каналы и ROI: окупает ли канал себя (фича уровня сквозной аналитики)."""
    from db import Spend as _S
    if request.method == "POST":
        if request.form.get("delete"):
            sp = _S.query.get(int(request.form["delete"]))
            if sp:
                db.session.delete(sp)
                db.session.commit()
            return redirect(url_for("spends_screen"))
        from datetime import datetime as _dt
        try:
            db.session.add(_S(
                channel_id=int(request.form["channel_id"]),
                date=_dt.strptime(request.form["date"], "%Y-%m-%d").date(),
                amount=float(str(request.form["amount"]).replace(" ", "").replace(",", ".")),
                note=request.form.get("note", "")))
            db.session.commit()
            flash("Расход добавлен.")
        except Exception as e:
            flash(f"Ошибка: {e}")
        return redirect(url_for("spends_screen"))
    d = _period_from_args()
    import utm as utm_mod
    from sqlalchemy import func as _f
    pays = utm_mod.payments_by_platform(*d)
    spend_rows = (db.session.query(Channel.platform, _f.coalesce(_f.sum(_S.amount), 0))
                  .join(_S, _S.channel_id == Channel.id)
                  .filter(_S.date >= d[0], _S.date <= d[1])
                  .group_by(Channel.platform).all())
    spends = {p: a for p, a in spend_rows}
    roi = []
    for plat in sorted(set(pays) | set(spends)):
        sp = spends.get(plat, 0)
        pa = pays.get(plat, 0)
        roi.append({"platform": plat, "spend": sp, "payments": pa,
                    "roi": ((pa - sp) / sp * 100) if sp else None})
    recent = _S.query.order_by(_S.date.desc()).limit(30).all()
    total_spend = sum(r[1] for r in spend_rows)
    total_pay = sum(pays.values())
    return render_template("spends.html", roi=roi, recent=recent, period=d,
                           total_spend=total_spend, total_pay=total_pay,
                           total_roi=((total_pay - total_spend) / total_spend * 100) if total_spend else None)


@app.route("/tasks", methods=["GET", "POST"])
def tasks_screen():
    """Задачи недели: план и отчёт (листы «Еженедельные статистики»/«Задачи» таблицы)."""
    from db import Task
    from datetime import timedelta as _td
    ws_, we_ = calc.week_bounds(date.today())
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            txt = (request.form.get("text") or "").strip()
            if txt:
                try:
                    d = datetime.strptime(request.form.get("week", ""), "%Y-%m-%d").date()
                except ValueError:
                    d = ws_
                db.session.add(Task(week_start=calc.week_bounds(d)[0], text=txt))
                db.session.commit()
                flash("Задача добавлена.")
        elif action == "status":
            t = Task.query.get(int(request.form.get("id") or 0))
            if t:
                t.status = {"план": "в работе", "в работе": "сделано", "сделано": "план"}.get(t.status, "план")
                db.session.commit()
        elif action == "delete":
            t = Task.query.get(int(request.form.get("id") or 0))
            if t:
                db.session.delete(t)
                db.session.commit()
        return redirect(url_for("tasks_screen"))
    items = Task.query.filter(Task.week_start >= ws_ - _td(days=60))         .order_by(Task.week_start.desc(), Task.id).all()
    weeks = {}
    for t in items:
        weeks.setdefault(t.week_start, []).append(t)
    return render_template("tasks.html", weeks=sorted(weeks.items(), reverse=True),
                           week=ws_)


@app.route("/calendar")
def calendar_screen():
    """Контент-календарь: месяц публикаций с эффективностью (фича Metricool)."""
    import calendar as _cal
    from datetime import timedelta as _td
    try:
        y, m = (int(x) for x in (request.args.get("month") or date.today().strftime("%Y-%m")).split("-"))
    except Exception:
        y, m = date.today().year, date.today().month
    first = date(y, m, 1)
    last = date(y, m, _cal.monthrange(y, m)[1])
    items = calc.content_stats_for_period(first - _td(days=first.weekday()), last)
    by_day = {}
    for it in items:
        pt = it["item"].published_at
        if not pt or not (first <= pt.date() <= last):
            continue
        by_day.setdefault(pt.date(), []).append(it)
    grid, week = [], [None] * first.weekday()
    d = first
    while d <= last:
        week.append(d)
        if len(week) == 7:
            grid.append(week)
            week = []
        d += _td(days=1)
    if week:
        week += [None] * (7 - len(week))
        grid.append(week)
    prev_m = (first - _td(days=1)).strftime("%Y-%m")
    next_m = (last + _td(days=1)).strftime("%Y-%m")
    RU_MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
                 "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
    return render_template("calendar.html", grid=grid, by_day=by_day,
                           month_title=f"{RU_MONTHS[m - 1]} {y}".capitalize(),
                           prev_m=prev_m, next_m=next_m, today=date.today())


@app.route("/content_item/<int:item_id>")
def content_item(item_id):
    """Полная карточка материала: теги AI, статистика, динамика, ссылки."""
    ci = ContentItem.query.get_or_404(item_id)
    stats = ContentStat.query.filter_by(content_id=item_id).order_by(ContentStat.date).all()
    tot = {k: sum(getattr(s_, k) or 0 for s_ in stats)
           for k in ("views", "reach", "likes", "comments", "saves", "shares",
                     "reactions", "subs", "registrations")}
    inter = tot["likes"] + tot["comments"] + tot["saves"] + tot["shares"] + tot["reactions"]
    err = inter / tot["reach"] * 100 if tot["reach"] else None
    cv = tot["registrations"] / tot["reach"] * 100 if tot["reach"] else None
    chart = {"labels": [s_.date.isoformat() for s_ in stats],
             "reach": [s_.reach or 0 for s_ in stats],
             "regs": [s_.registrations or 0 for s_ in stats]}
    return render_template("content_item.html", ci=ci, stats=stats, tot=tot,
                           err=err, cv=cv, inter=inter, chart=chart,
                           tags=json.loads(ci.ai_tags or "{}"))


@app.route("/utm")
def utm_builder():
    import utm as utm_mod
    return render_template("utm.html",
                           sources=utm_mod.SOURCES, mediums=utm_mod.MEDIUMS,
                           campaigns=utm_mod.CAMPAIGN_TYPES)


@app.route("/hypotheses", methods=["GET", "POST"])
def hypotheses_screen():
    """A/B-гипотезы: ожидание -> автоматическая сверка с фактом."""
    from db import Hypothesis
    if request.method == "POST":
        from datetime import datetime as _dt
        try:
            db.session.add(Hypothesis(
                text=request.form.get("text", ""),
                metric=request.form.get("metric", "reach"),
                expectation=request.form.get("expectation", ""),
                start=_dt.strptime(request.form.get("start", ""), "%Y-%m-%d").date(),
                end=_dt.strptime(request.form.get("end", ""), "%Y-%m-%d").date()))
            db.session.commit()
            flash("Гипотеза добавлена.")
        except Exception as e:
            flash(f"Ошибка: {e}")
        return redirect(url_for("hypotheses_screen"))
    # авто-сверка завершившихся
    from datetime import timedelta as _td
    for h in Hypothesis.query.filter_by(status="active").all():
        if h.end >= date.today():
            continue
        pa = cached_period_report(h.start, h.end)
        pb = cached_period_report(h.start - _td(days=(h.end - h.start).days + 1),
                                  h.start - _td(days=1))
        key = {"reach": "reach", "views": "views", "regs": "registrations"}.get(h.metric)
        cur = pa["agg"].get(key) if key else pa["ind"].get(
            {"err": "ERR", "cv": "CV_reach"}.get(h.metric, ""), None)
        prev = pb["agg"].get(key) if key else pb["ind"].get(
            {"err": "ERR", "cv": "CV_reach"}.get(h.metric, ""), None)
        if h.metric == "payments":
            from db import GcPayment
            cur = db.session.query(db.func.coalesce(db.func.sum(GcPayment.amount), 0)).filter(
                GcPayment.date >= h.start, GcPayment.date <= h.end,
                GcPayment.status == "accepted").scalar()
            prev = db.session.query(db.func.coalesce(db.func.sum(GcPayment.amount), 0)).filter(
                GcPayment.date >= h.start - _td(days=(h.end - h.start).days + 1),
                GcPayment.date <= h.start - _td(days=1),
                GcPayment.status == "accepted").scalar()
        if cur is not None and prev:
            pct = (cur - prev) / prev * 100
            h.result = f"Факт: {pct:+.1f}% ({prev:,.0f} → {cur:,.0f})".replace(",", " ")
        else:
            h.result = "Недостаточно данных для проверки"
        h.status = "done"
    db.session.commit()
    items = Hypothesis.query.order_by(Hypothesis.id.desc()).limit(30).all()
    return render_template("hypotheses.html", items=items,
                           period=calc.week_bounds(date.today()))


@app.route("/api/notifications/pending")
def notifications_pending():
    """Очередь недоставленных уведомлений (забирает релей и шлёт в Telegram)."""
    token = os.environ.get("INGEST_TOKEN") or get_setting("ingest_token")
    if request.args.get("token") != token:
        return jsonify({"error": "invalid token"}), 403
    ns = Notification.query.filter_by(delivered=False).order_by(Notification.id).limit(50).all()
    return jsonify({"items": [{"id": n.id, "level": n.level, "message": n.message,
                               "at": n.created_at.isoformat()} for n in ns]})


@app.route("/api/notifications/delivered", methods=["POST"])
def notifications_delivered():
    token = os.environ.get("INGEST_TOKEN") or get_setting("ingest_token")
    if request.headers.get("X-Ingest-Token") != token:
        return jsonify({"error": "invalid token"}), 403
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    Notification.query.filter(Notification.id.in_(ids)).update(
        {"delivered": True}, synchronize_session=False)
    db.session.commit()
    return jsonify({"ok": True, "marked": len(ids)})


@app.route("/telegram/test", methods=["POST"])
def telegram_test():
    """Прямая попытка отправки тестового сообщения (работает, если api.telegram.org доступен)."""
    token = get_setting("tg_bot_token")
    chat = get_setting("tg_chat_id")
    if not token or not chat:
        flash("Укажите tg_bot_token и tg_chat_id в настройках ниже")
        return redirect(url_for("settings"))
    try:
        import requests as _rq
        r = _rq.post(f"https://api.telegram.org/bot{token}/sendMessage",
                     json={"chat_id": chat,
                           "text": "✅ SMM Аналитика: тест связи успешен. Сюда будут приходить уведомления и недельные отчёты."},
                     timeout=20)
        if r.status_code == 200:
            flash("✅ Тестовое сообщение отправлено — проверьте Telegram.")
        else:
            flash(f"Telegram ответил ошибкой: {r.json().get('description', r.status_code)}")
    except Exception as e:
        flash(f"⚠ Прямая отправка недоступна с этого хостинга ({str(e)[:80]}). "
              "Сообщения доставит релей GitHub Actions — запустите его или дождитесь расписания.")
    return redirect(url_for("settings"))


@app.route("/admin/heal_missing")
def heal_missing():
    """Перемаркировать статусы за последние дни адаптивной логикой:
    метрики, которые ни один источник никогда не давал, становятся NOT_AVAILABLE
    (перекрывают старые MISSING свежими снапшотами). Токен как у /cron."""
    tokens = {app.secret_key, os.environ.get("CRON_TOKEN", "")} - {""}
    if request.args.get("token") not in tokens:
        return jsonify({"error": "invalid token"}), 403
    from datetime import timedelta as _td
    run_id = connectors.start_run("heal_missing")
    schedule_rewarm(delay=30)
    app.config["_HEALTH_CACHE"] = None
    results = []
    for back in range(1, int(request.args.get("days", 7)) + 1):
        d = date.today() - _td(days=back)
        results.append(connectors.mark_missing(run_id, d))
    connectors.finish_run(run_id, "; ".join(results))
    return jsonify({"ok": True, "days": results})


@app.route("/livedune/sync", methods=["POST"])
def livedune_sync():
    if not livedune.configured():
        flash("Не задан токен LiveDune (экран «Настройки»)")
        return redirect(url_for("overview"))
    if not livedune.can_reach():
        flash("⚠ Прямой доступ к api.livedune.com с этого хостинга закрыт (прокси free-тарифа). "
              "Данные доставляет релей GitHub Actions по расписанию.")
        return redirect(url_for("overview"))
    livedune.sync_livedune(days=7, threaded=True)
    flash("Синхронизация LiveDune запущена в фоне (1–2 минуты).")
    return redirect(url_for("overview"))


@app.route("/collect", methods=["POST"])
def collect():
    schedule_rewarm()
    results = connectors.run_daily_collection()
    flash("Сбор данных выполнен: " + "; ".join(results))
    return redirect(request.referrer or url_for("overview"))


@app.route("/import", methods=["GET", "POST"])
def import_csv():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Файл не выбран")
            return redirect(url_for("import_csv"))
        if f.filename.lower().endswith((".xlsx", ".xlsm")):
            year = request.form.get("year", "").strip()
            stats = connectors.import_xlsx(f, int(year) if year.isdigit() else None)
            schedule_rewarm()
            flash(f"XLSX обработан: охваты {stats['reach']} значений, лиды {stats['leads']} строк, "
                  f"подписчики {stats['followers']} значений"
                  + (f", пропущены листы: {'; '.join(stats['skipped'])}" if stats["skipped"] else ""))
        elif f.filename.lower().endswith((".csv", ".txt", ".tsv")):
            n = connectors.import_csv_channel(f)
            schedule_rewarm()
            flash(f"Импортировано строк: {n}")
        else:
            flash("Поддерживаются .xlsx и .csv")
        return redirect(url_for("import_csv"))
    return render_template("import.html")


@app.route("/manual", methods=["GET", "POST"])
def manual():
    if request.method == "POST":
        if request.form.get("kind") == "note":
            db.session.add(ManualNote(
                period_start=datetime.strptime(request.form["period_start"], "%Y-%m-%d").date(),
                period_end=datetime.strptime(request.form["period_end"], "%Y-%m-%d").date(),
                product=request.form.get("product"), goal=request.form.get("goal"),
                kpi=request.form.get("kpi"), events=request.form.get("events")))
            db.session.commit()
            flash("Контекст периода сохранён")
            return redirect(url_for("manual"))
        ch_id = int(request.form["channel_id"])
        d = datetime.strptime(request.form["date"], "%Y-%m-%d").date()
        run_id = connectors.start_run("manual_entry")
        cnt = 0
        for m in connectors.DAILY_METRICS:
            v = request.form.get(m, "").strip()
            if v != "":
                connectors.save_metric(run_id, ch_id, d, m, float(v.replace(" ", "").replace(",", ".")), "manual", status="MANUAL")
                cnt += 1
        connectors.finish_run(run_id, f"вручную {cnt} метрик")
        flash(f"Сохранено метрик: {cnt}")
        return redirect(url_for("manual"))
    notes = ManualNote.query.order_by(ManualNote.period_start.desc()).limit(20).all()
    return render_template("manual.html", notes=notes, metrics=connectors.DAILY_METRICS)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    keys = ["app_password", "week_start_day", "alert_reach_drop", "alert_views_drop", "alert_err_drop",
            "tg_bot_token", "tg_chat_id", "livedune_token",
            "youtube_api_key", "youtube_channel_id",
            "instagram_token", "vk_token", "telegram_bot_token", "max_bot_tokens",
            "gc_account", "gc_api_key",
            "ai_api_key", "ai_base_url", "ai_model"]
    if request.method == "POST":
        for k in keys:
            set_setting(k, request.form.get(k, ""))
        db.session.commit()
        flash("Настройки сохранены")
        return redirect(url_for("settings"))
    return render_template("settings.html", values={k: get_setting(k) for k in keys},
                           webhook_token=os.environ.get("INGEST_TOKEN") or get_setting("ingest_token"))


@app.route("/getcourse")
def getcourse_screen():
    """Экран 4 (расширение): заказы и оплаты из GetCourse, воронка, новичок/старичок."""
    d = _period_from_args()
    step_status = None
    if IS_SERVERLESS and getcourse.configured():
        step_status = getcourse.gc_status()
    f = getcourse.funnel(*d)
    logs = RunLog.query.filter(RunLog.kind.in_(["gc_sync", "gc_relay"])) \
        .order_by(RunLog.started_at.desc()).limit(10).all()
    chart = _gc_daily_chart(d)
    recent_orders = GcOrder.query.order_by(GcOrder.created_at.desc().nullslast()).limit(20).all()
    import utm as utm_mod
    cohorts = utm_mod.retention_cohorts(6)
    return render_template("getcourse.html", f=f, period=d, logs=logs, chart=chart,
                           recent_orders=recent_orders, cohorts=cohorts,
                           configured=getcourse.configured(), step_status=step_status)


@app.route("/getcourse/sync", methods=["POST"])
def getcourse_sync():
    if not getcourse.configured():
        flash("Не задан API-ключ GetCourse (экран «Настройки»)")
        return redirect(url_for("getcourse_screen"))
    if not getcourse.can_reach():
        flash("⚠ Прямой доступ к syrover.com с этого хостинга закрыт (ограничение бесплатного "
              "тарифа PythonAnywhere). Данные доставляет релей GitHub Actions по расписанию "
              "4 раза в сутки; запустить вручную: GitHub → репозиторий smm-analytics → "
              "Actions → GetCourse sync relay → Run workflow.")
        return redirect(url_for("getcourse_screen"))
    if IS_SERVERLESS:
        status = getcourse.gc_run_steps(seconds=35)
        flash(f"Синхронизация GetCourse (пошаговый режим): {status}. "
              "Если не завершилась — продолжится при следующих пингах /cron.")
    else:
        getcourse.sync_getcourse(days=7, threaded=True)
        flash("Синхронизация GetCourse запущена в фоне; результат появится через 1–3 минуты "
              "(экспорты ГК выполняются по одному, при занятости — повторные попытки).")
    return redirect(url_for("getcourse_screen"))


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Приём данных GetCourse от релея (GitHub Actions / локальный скрипт).
    Нужен, когда хостинг приложения не имеет прямого доступа к syrover.com
    (например, бесплатный PythonAnywhere с прокси-белым списком).
    Токен: env INGEST_TOKEN или настройка ingest_token. Тело:
    {"users": [...], "deals": [...], "payments": [...], "window_start": "YYYY-MM-DD"}"""
    token = os.environ.get("INGEST_TOKEN") or get_setting("ingest_token")
    if request.headers.get("X-Ingest-Token") != token:
        return jsonify({"error": "invalid token"}), 403
    data = request.get_json(force=True, silent=True) or {}
    schedule_rewarm()
    app.config["_HEALTH_CACHE"] = None
    default = date.today()
    ws = data.get("window_start")
    if ws:
        try:
            default = datetime.strptime(ws[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    counts = {"users": 0, "deals": 0, "payments": 0, "ld": 0}
    if data.get("ld"):
        try:
            counts["ld"] = livedune.ingest_packet(data["ld"])
            return jsonify(counts)
        except Exception as e:
            return jsonify({"error": f"ld ingest: {e}"}), 500
    if data.get("users"):
        counts["users"] = getcourse._ingest_users(data["users"], default)
    if data.get("deals"):
        counts["deals"] = getcourse._ingest_deals(data["deals"], default)
    if data.get("payments"):
        counts["payments"] = getcourse._ingest_payments(data["payments"], default)
    r = RunLog(kind="gc_relay", status="OK",
               details=f"users={counts['users']} deals={counts['deals']} payments={counts['payments']}")
    db.session.add(r)
    db.session.add(Notification(level="info", message=(
        f"GetCourse (релей): загружено пользователей {counts['users']}, "
        f"заказов {counts['deals']}, оплат {counts['payments']}.")))
    db.session.commit()
    return jsonify(counts)


@app.route("/api/gc-webhook", methods=["GET", "POST"])
def gc_webhook():
    """Приём мгновенных событий из процессов Геткурса («Вызвать URL»).
    Токен: заголовок X-Ingest-Token или параметр token.
    Типы: user | deal | payment. Параметры сливаются из query, form и JSON."""
    token = os.environ.get("INGEST_TOKEN") or get_setting("ingest_token")
    args = {k: v for k, v in request.args.items()}
    if request.form:
        args.update(request.form.to_dict())
    j = request.get_json(silent=True)
    if isinstance(j, dict):
        args.update(j)
    if args.get("token") != token and request.headers.get("X-Ingest-Token") != token:
        return jsonify({"error": "invalid token"}), 403
    t = (args.get("type") or "").lower()
    schedule_rewarm()
    from datetime import datetime as _dtx
    def _val(*names):
        for n in names:
            for k, v in args.items():
                if k.lower() == n.lower() and v not in (None, ""):
                    return v
        return None
    def _dateval(*names):
        v = _val(*names)
        if not v:
            return date.today()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return _dtx.strptime(str(v)[:19].replace("T", " "), fmt).date()
            except ValueError:
                continue
        return date.today()
    try:
        if t == "user":
            uid = int(float(_val("gc_id", "user_id", "id") or 0))
            if uid:
                reg = Registration.query.filter_by(gc_user_id=uid).first()
                if not reg:
                    reg = Registration(count=1, gc_user_id=uid)
                    db.session.add(reg)
                reg.date = _dateval("created", "created_at", "Создан")
                try:
                    reg.created_at = datetime.strptime(str(_val("created", "created_at", "Создан"))[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
                reg.utm_source = str(_val("utm_source") or "")[:64]
                reg.utm_medium = str(_val("utm_medium") or "")[:64]
                reg.utm_campaign = str(_val("utm_campaign") or "")[:128]
                reg.landing = str(_val("landing", "page_url") or "")[:255]
                reg.status = "OK"
                getcourse._log_event("user", uid, dict(args))
        elif t == "deal":
            did = int(float(_val("deal_id", "gc_id", "id") or 0))
            if did:
                o = GcOrder.query.get(did) or GcOrder(id=did)
                db.session.add(o)
                o.date = _dateval("created", "created_at", "Создан")
                o.created_at = getcourse._dt(_val("created", "created_at", "Создан"))
                o.user_id = getcourse._num(_val("user_id", "gc_user_id"))
                o.email = str(_val("email") or "")[:255]
                o.phone = str(_val("phone", "Телефон") or "")[:64]
                o.product = str(_val("product", "Название предложения") or "")[:255]
                o.amount = getcourse._num(_val("amount", "Сумма", "price"))
                o.status = str(_val("status", "Статус") or "")[:32]
                o.status_title = str(_val("status", "Статус") or "")[:64]
                o.utm_source = str(_val("utm_source") or "")[:64]
                o.utm_medium = str(_val("utm_medium") or "")[:64]
                o.utm_campaign = str(_val("utm_campaign") or "")[:128]
                o.direction = getcourse._direction(o.utm_source)
                o.updated_at = datetime.utcnow()
                getcourse._log_event("deal", did, dict(args))
                getcourse._recompute_customer_status()
        elif t == "payment":
            pid = int(float(_val("payment_id", "gc_id", "id") or 0))
            if pid:
                p = GcPayment.query.get(pid) or GcPayment(id=pid)
                db.session.add(p)
                p.date = _dateval("created", "created_at", "Создан", "payed_at")
                p.created_at = getcourse._dt(_val("created", "created_at", "Создан", "payed_at"))
                p.user_id = getcourse._num(_val("user_id", "gc_user_id"))
                p.email = str(_val("email") or "")[:255]
                p.amount = getcourse._num(_val("amount", "Сумма"))
                p.status = str(_val("status", "Статус") or "accepted")[:32]
                p.deal_id = getcourse._num(_val("deal_id"))
                p.product = str(_val("product") or "")[:255]
                p.updated_at = datetime.utcnow()
                getcourse._log_event("payment", pid, dict(args))
        else:
            return jsonify({"error": "unknown type"}), 400
        db.session.commit()
        return jsonify({"ok": True, "type": t})
    except Exception as e:
        db.session.rollback()
        db.session.add(Notification(level="error", message=f"Ошибка вебхука ГК: {e}"))
        db.session.commit()
        return jsonify({"error": str(e)}), 500


@app.route("/ai/plan", methods=["POST"])
def ai_plan():
    """Сгенерировать контент-план следующей недели на основе данных."""
    import ai_analyst
    from datetime import timedelta as _td
    anchor = datetime.strptime(request.form.get("anchor", date.today().isoformat()), "%Y-%m-%d").date()
    s_, e_ = calc.week_bounds(anchor)
    ps, pe = s_ - _td(days=7), e_ - _td(days=7)   # база — прошлая полная неделя
    text = ai_analyst.generate_content_plan(ps, pe)
    from db import Report
    r = Report.query.filter_by(rtype="plan", start=s_, end=e_).first() or         Report(rtype="plan", start=s_, end=e_)
    r.ai_text = text
    db.session.add(r)
    db.session.commit()
    flash("Контент-план на неделю готов — раздел «Планы недели» ниже.")
    return redirect(url_for("ai_screen"))


@app.route("/ai/reclassify", methods=["POST"])
def ai_reclassify():
    """Батч-классификация контента через LLM (кнопка на экране AI-аналитики)."""
    from db import ContentItem as _CI
    import ai_analyst, re as _re
    items = _CI.query.order_by(_CI.published_at.desc()).limit(40).all()
    done, used_llm = 0, False
    for i in range(0, len(items), 8):
        batch = items[i:i + 8]
        lines = [f"{ci.id}: {(ci.title or '')} | {(ci.text or '')[:180]} | формат: {ci.format}"
                 for ci in batch]
        llm = ai_analyst._call_llm(ai_analyst.SYSTEM,
            "Проклассифицируй каждый материал. Верни СТРОГО JSON вида "
            '{"<id>": {"тема": "...", "рубрика": "...", "боль": "...", "сегмент": "...", '
            '"эмоциональный_триггер": "...", "есть_оффер": true, '
            '"есть_регистрационный_CTA": true, '
            '"продающий_тип": "продающий|экспертный|доверительный|охватный"}}\n'
            "Материалы:\n" + "\n".join(lines))
        mapping = {}
        if llm:
            try:
                m = _re.search(r"\{.*\}", llm, _re.S)
                if m:
                    mapping = json.loads(m.group())
                    used_llm = True
            except Exception:
                mapping = {}
        for ci in batch:
            tags = mapping.get(str(ci.id)) or ai_analyst.classify_content_text(ci.text, ci.format)
            ci.ai_tags = json.dumps(tags, ensure_ascii=False)
            done += 1
    db.session.commit()
    flash(f"Классифицировано материалов: {done}" +
          (" (через LLM)" if used_llm else
           " (эвристически — LLM недоступна, проверьте баланс OpenAI в Настройках)"))
    return redirect(url_for("ai_screen"))


@app.route("/guide")
def guide():
    return render_template("guide.html")


@app.route("/channel/<int:channel_id>")
def channel_detail(channel_id):
    """Детализация канала: KPI, динамика по дням, материалы, регистрации, история метрик."""
    ch = Channel.query.get_or_404(channel_id)
    d = _period_from_args()
    p = calc.period_report(*d, ch.id)
    chart = _chart_series(*d)
    items = calc.content_stats_for_period(*d, ch.id)
    items.sort(key=lambda x: x.get("reach") or 0, reverse=True)
    # регистрации по UTM этого канала
    q = Registration.query.filter(Registration.date >= d[0], Registration.date <= d[1],
                                  Registration.status == "OK",
                                  ~Registration.utm_source.like("demo_%"))
    q = _regs_with_time(q)
    by_utm = {}
    from sqlalchemy import func as _f
    rows = db.session.query(Registration.utm_source, _f.sum(Registration.count)).filter(
        Registration.date >= d[0], Registration.date <= d[1],
        Registration.status == "OK", ~Registration.utm_source.like("demo_%")).group_by(
        Registration.utm_source).all()
    plat = calc.UTM_TO_PLATFORM.get((ch.platform or "").lower())
    total_regs = sum(r[1] or 0 for r in rows if plat and calc.UTM_TO_PLATFORM.get((r[0] or "").lower()) == plat)
    return render_template("channel.html", ch=ch, p=p, chart=chart, period=d,
                           items=items[:30], by_utm=rows, total_regs=total_regs,
                           trends=calc.weekly_series(8, ch.id))


@app.route("/notifications")
def notifications():
    ns = Notification.query.order_by(Notification.created_at.desc()).limit(100).all()
    for n in ns:
        n.is_read = True
    db.session.commit()
    return render_template("notifications.html", notifications=ns)


@app.route("/history/<int:channel_id>/<metric>/<day>")
def metric_history(channel_id, metric, day):
    d = datetime.strptime(day, "%Y-%m-%d").date()
    snaps = MetricSnapshot.query.filter_by(channel_id=channel_id, metric=metric, date=d) \
        .order_by(MetricSnapshot.fetched_at).all()
    return render_template("history.html", snaps=snaps, metric=metric, day=day,
                           channel=Channel.query.get(channel_id))


# ---------------- helpers ----------------

def _period_from_args():
    """Период с датами и (опционально) точным временем границ — по ТЗ клиента
    отчётная неделя типа «с 30-го 10:00 до 6-го 10:00»."""
    ps = request.args.get("start"); pe = request.args.get("end")
    preset = request.args.get("preset", "week")
    anchor = datetime.strptime(ps, "%Y-%m-%d").date() if ps else date.today()
    if ps and pe:
        return datetime.strptime(ps, "%Y-%m-%d").date(), datetime.strptime(pe, "%Y-%m-%d").date()
    from datetime import timedelta as _td
    if preset == "month":
        return calc.month_bounds(anchor)
    if preset == "7d":
        return anchor - _td(days=6), anchor
    if preset == "30d":
        return anchor - _td(days=29), anchor
    if preset == "prevmonth":
        s, e = calc.month_bounds(anchor)
        ps_ = s - _td(days=1)
        return ps_.replace(day=1), ps_
    return calc.week_bounds(anchor)


@calc.ttl_cache(120)
def _period_datetimes():
    """(start_dt, end_dt) с учётом полей t0/t1 (часы:минуты), None если не заданы."""
    t0 = request.args.get("t0", "")
    t1 = request.args.get("t1", "")
    try:
        from datetime import datetime as _dt
        d0 = _dt.strptime(request.args.get("start", ""), "%Y-%m-%d")
    except ValueError:
        d0 = None
    try:
        from datetime import datetime as _dt
        d1 = _dt.strptime(request.args.get("end", ""), "%Y-%m-%d")
    except ValueError:
        d1 = None
    import datetime as _dtm
    sdt = None
    edt = None
    if d0:
        sdt = _dtm.datetime.combine(d0, _dtm.time())
        if ":" in t0:
            h, m = t0.split(":")[:2]
            sdt = _dtm.datetime.combine(d0, _dtm.time(int(h), int(m)))
    if d1:
        edt = _dtm.datetime.combine(d1 + _dtm.timedelta(days=1), _dtm.time())  # конец дня включительно
        if ":" in t1:
            h, m = t1.split(":")[:2]
            edt = _dtm.datetime.combine(d1, _dtm.time(int(h), int(m)))
    return sdt, edt


@calc.ttl_cache(600)
def _gc_daily_chart(d):
    """По-дневная динамика заказов/оплат (кэш 10 минут)."""
    from sqlalchemy import func as _f
    from datetime import timedelta as _td
    days = []
    cur = d[0]
    while cur <= d[1]:
        days.append(cur.isoformat())
        cur += _td(days=1)
    o_by_day = dict(db.session.query(_f.date(GcOrder.date), _f.count(GcOrder.id))
                    .filter(GcOrder.date >= d[0], GcOrder.date <= d[1]).group_by(GcOrder.date).all())
    p_by_day = dict(db.session.query(_f.date(GcPayment.date), _f.count(GcPayment.id))
                    .filter(GcPayment.date >= d[0], GcPayment.date <= d[1]).group_by(GcPayment.date).all())
    s_by_day = dict(db.session.query(_f.date(GcPayment.date), _f.coalesce(_f.sum(GcPayment.amount), 0))
                    .filter(GcPayment.date >= d[0], GcPayment.date <= d[1],
                            GcPayment.status == "accepted").group_by(GcPayment.date).all())
    return {"labels": days,
            "orders": [o_by_day.get(x, 0) for x in days],
            "payments": [p_by_day.get(x, 0) for x in days],
            "sums": [s_by_day.get(x, 0) for x in days]}


def _chart_series(start, end):
    days = []
    d = start
    while d <= end:
        days.append(d.isoformat())
        from datetime import timedelta
        d += timedelta(days=1)
    q = (db.session.query(MetricSnapshot.date, MetricSnapshot.metric, MetricSnapshot.value,
                          db.func.max(MetricSnapshot.fetched_at))
         .filter(MetricSnapshot.date >= start, MetricSnapshot.date <= end,
                 MetricSnapshot.metric.in_(["reach", "registrations_daily"]))
         .group_by(MetricSnapshot.date, MetricSnapshot.metric, MetricSnapshot.value)).all()
    reach = {r.date.isoformat(): r.value for r in q if r.metric == "reach" and r.value}
    inter_q = (db.session.query(MetricSnapshot.date, MetricSnapshot.metric, MetricSnapshot.value,
                                db.func.max(MetricSnapshot.fetched_at))
               .filter(MetricSnapshot.date >= start, MetricSnapshot.date <= end,
                       MetricSnapshot.metric.in_(["likes", "comments", "saves", "shares", "reactions"]))
               .group_by(MetricSnapshot.date, MetricSnapshot.metric, MetricSnapshot.value)).all()
    inter = {}
    for r in inter_q:
        inter[r.date.isoformat()] = inter.get(r.date.isoformat(), 0) + (r.value or 0)
    regs = {}
    for r in Registration.query.filter(Registration.date >= start, Registration.date <= end).all():
        regs[r.date.isoformat()] = regs.get(r.date.isoformat(), 0) + (r.count or 0)
    return {"labels": days, "reach": [reach.get(x) for x in days],
            "regs": [regs.get(x) for x in days], "inter": [inter.get(x) for x in days]}


def _latest_report(rtype):
    return Report.query.filter_by(rtype=rtype).order_by(Report.end.desc()).first()


@app.route("/cron")
def cron():
    """Планировщик. Два режима:
    - с token (= SECRET_KEY или CRON_TOKEN) — полный доступ (до 45с шагов ГК);
    - без token, но с заголовком x-vercel-cron (вызов самого Vercel) — быстрый режим,
      не чаще раза в 20 минут (защита от подделок: лимит шагов ГК не выжечь)."""
    tokens = {app.secret_key, os.environ.get("CRON_TOKEN", "")} - {""}
    token_ok = request.args.get("token") in tokens
    from db import Setting as _S
    last_raw = get_setting("last_free_cron", "")
    from datetime import datetime as _dt
    last = None
    try:
        last = _dt.fromisoformat(last_raw) if last_raw else None
    except ValueError:
        last = None
    free_ok = False
    if not token_ok and request.headers.get("x-vercel-cron"):
        if not last or (_dt.utcnow() - last).total_seconds() > 20 * 60:
            free_ok = True
    if not token_ok and not free_ok:
        return jsonify({"error": "invalid token"}), 403
    fast = not token_ok
    if free_ok:
        set_setting("last_free_cron", _dt.utcnow().isoformat())
        db.session.commit()
    out = {}
    try:
        out["collect"] = "; ".join(connectors.run_daily_collection())
    except Exception as e:
        out["collect"] = f"error: {e}"
    try:
        if getcourse.configured():
            if IS_SERVERLESS:
                getcourse.gc_start(days=5)
                out["getcourse"] = getcourse.gc_run_steps(seconds=8 if fast else 45)
            else:
                getcourse.sync_getcourse(days=5)
                out["getcourse"] = "ok"
    except Exception as e:
        out["getcourse"] = f"error: {e}"
    return jsonify(out)


# ---------------- планировщик ----------------

def start_scheduler():
    """Ежедневный сбор; недельный отчёт в понедельник 06:00; месячный 1-го числа 07:00."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        sched = BackgroundScheduler(daemon=True)
        with app.app_context():
            pass
        def daily():
            with app.app_context():
                connectors.run_daily_collection()
        def weekly():
            with app.app_context():
                reports.generate_report("weekly")
        def monthly():
            with app.app_context():
                reports.generate_report("monthly")
        def gc_daily():
            with app.app_context():
                # на хостингах без прямого доступа к ГК (прокси PA) синхронизацию
                # выполняет релей GitHub Actions — здесь только если сеть доступна
                if getcourse.configured() and getcourse.can_reach():
                    getcourse.sync_getcourse(days=5)
        warm_fn = prewarm()
        for h in (2, 8, 14, 20):
            sched.add_job(warm_fn, CronTrigger(hour=h, minute=12))
        sched.add_job(daily, CronTrigger(hour=3))
        sched.add_job(gc_daily, CronTrigger(hour=4))
        sched.add_job(weekly, CronTrigger(day_of_week="mon", hour=6))
        sched.add_job(monthly, CronTrigger(day=1, hour=7))
        sched.start()
    except Exception as e:
        app.logger.warning(f"Планировщик не запущен: {e}")


app.config["SQLALCHEMY_RECORD_QUERIES"] = False

@app.route("/login", methods=["GET", "POST"])
def login():
    pw = get_setting("app_password") or os.environ.get("APP_PASSWORD", "")
    if request.method == "POST":
        if request.form.get("password") and request.form["password"] == pw:
            session["authed"] = True
            return redirect(url_for("overview"))
        flash("Неверный пароль")
    return render_template("login.html"), (401 if request.method == "POST" else 200)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.before_request
def _auth_guard():
    """Закрываем интерфейс паролем; API защищены собственными токенами."""
    pw = get_setting("app_password") or os.environ.get("APP_PASSWORD", "")
    if not pw or session.get("authed"):
        return None
    if request.path.startswith(("/login", "/api/", "/static/", "/cron")):
        return None
    return redirect(url_for("login"))


@app.before_request
def _start_timer():
    from flask import g as _g
    import time as _t
    _g._t0 = _t.time()


@app.after_request
def _gzip_response(resp):
    """Сжатие HTML/JSON — страницы грузятся в 3-5 раз быстрее по сети."""
    try:
        if (resp.mimetype and resp.mimetype.startswith("text/") and len(resp.data) > 1024
                and "gzip" in request.headers.get("Accept-Encoding", "")):
            import zlib
            resp.data = zlib.compress(resp.data, 5)
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Vary"] = "Accept-Encoding"
    except Exception:
        pass
    return resp


@app.after_request
def _static_cache(resp):
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.after_request
def _report_timer(resp):
    import time as _t
    try:
        from flask import g as _g
        dt = (_t.time() - _g._t0) * 1000
        resp.headers["X-Render-Time-ms"] = str(int(dt))
    except Exception:
        pass
    return resp


with app.app_context():
    db.create_all()
    # составные индексы под тяжёлые GROUP BY (идемпотентно, SQLite/Postgres)
    from sqlalchemy import text as _text
    for ddl in (
        "CREATE INDEX IF NOT EXISTS ix_ms_ch_date_metric ON metric_snapshots (channel_id, date, metric, fetched_at)",
        "CREATE INDEX IF NOT EXISTS ix_ms_date_metric ON metric_snapshots (date, metric)",
        "CREATE INDEX IF NOT EXISTS ix_cs_content_date ON content_stats (content_id, date)",
        "CREATE INDEX IF NOT EXISTS ix_reg_date_src ON registrations (date, utm_source)",
        "CREATE INDEX IF NOT EXISTS ix_gc_order_date ON gc_orders (date, utm_campaign)",
        "CREATE INDEX IF NOT EXISTS ix_comment_date ON comments (date)",
    ):
        try:
            db.session.execute(_text(ddl))
        except Exception:
            pass
    for ddl_a in (
        "ALTER TABLE notifications ADD COLUMN delivered BOOLEAN DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN is_competitor BOOLEAN DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN ld_account_id INTEGER",
        "ALTER TABLE registrations ADD COLUMN created_at TIMESTAMP",
    ):
        try:
            db.session.execute(_text(ddl_a))
        except Exception:
            pass
    db.session.commit()
    # значения по умолчанию из окружения (Render env vars)
    import os as _os
    if _os.environ.get("GC_API_KEY") and not get_setting("gc_api_key"):
        set_setting("gc_api_key", _os.environ["GC_API_KEY"])
    # одноразовая миграция: демо-регистрации старого сида помечаем demo_
    if MetricSnapshot.query.filter_by(source="demo").first() and not get_setting("demo_regfix"):
        from db import Registration as _R
        _R.query.filter(_R.gc_user_id.is_(None),
                        ~_R.utm_source.like("demo_%")).update(
            {_R.utm_source: "demo_" + _R.utm_source}, synchronize_session=False)
        db.session.commit()
        set_setting("demo_regfix", "1")
        db.session.commit()
    if _os.environ.get("YOUTUBE_API_KEY") and not get_setting("youtube_api_key"):
        set_setting("youtube_api_key", _os.environ["YOUTUBE_API_KEY"])
    if _os.environ.get("APP_PASSWORD") and not get_setting("app_password"):
        set_setting("app_password", _os.environ["APP_PASSWORD"])
    if _os.environ.get("LIVEDUNE_TOKEN") and not get_setting("livedune_token"):
        set_setting("livedune_token", _os.environ["LIVEDUNE_TOKEN"])
    for env_key, set_key in (("AI_API_KEY", "ai_api_key"), ("AI_BASE_URL", "ai_base_url"),
                             ("AI_MODEL", "ai_model")):
        if _os.environ.get(env_key) and not get_setting(set_key):
            set_setting(set_key, _os.environ[env_key])
    if _os.environ.get("GC_ACCOUNT") and not get_setting("gc_account"):
        set_setting("gc_account", _os.environ["GC_ACCOUNT"])
    db.session.commit()
    seed.seed()

_rewarm_scheduled = [False]


def schedule_rewarm(delay=90):
    """Данные обновились: страницы продолжают отдаваться из кэша мгновенно,
    пересчёт запускается в фоне через delay секунд (однократно)."""
    if _rewarm_scheduled[0]:
        return
    _rewarm_scheduled[0] = True

    def _later():
        import time as _t
        _t.sleep(delay)
        try:
            prewarm()()
        finally:
            _rewarm_scheduled[0] = False
    import threading as _th
    _th.Thread(target=_later, daemon=True).start()


def prewarm():
    """Прогрев кэшей тяжёлых расчётов после старта — первый посетитель не ждёт."""
    import threading
    def _warm():
        import time as _t
        _t.sleep(3)
        with app.app_context():
            try:
                d = calc.week_bounds(date.today())
                calc.weekly_series(8)
                calc.growth_points(*d)
                getcourse.funnel(*d)
                _chart_series(*d)
                _gc_daily_chart(d)
                import utm as utm_mod
                utm_mod.breakdown(*d)
                utm_mod.retention_cohorts(6)
                for ch in Channel.query.filter_by(is_active=True).all():
                    calc.period_report(*d, ch.id)
            except Exception:
                pass
    threading.Thread(target=_warm, daemon=True).start()
    return _warm


if not IS_SERVERLESS and (os.environ.get("RENDER") or os.environ.get("SMM_SCHEDULER", "1") == "1"):
    start_scheduler()
    prewarm()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
