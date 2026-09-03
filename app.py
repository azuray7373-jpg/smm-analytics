# -*- coding: utf-8 -*-
import json, os, threading
from datetime import date, datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response

from db import db, Channel, MetricSnapshot, ContentItem, ContentStat, Registration, \
    ManualNote, Report, Notification, Setting, RunLog, get_setting, set_setting, \
    Comment, GcOrder, GcPayment
import calc, connectors, reports, seed, getcourse, comments as comments_mod

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
    except (TypeError, ValueError):
        return "н/д"


@app.template_filter("pctfmt")
def _pctfmt(v, nd=2):
    try:
        return (f"{float(v):.{nd}f}%").replace(".", ",")
    except (TypeError, ValueError):
        return "н/д"


@app.route("/reports/view/<int:id>")
def report_view(id):
    return render_template("report_view.html", r=Report.query.get_or_404(id))


@app.context_processor
def inject_globals():
    from flask import request as _req
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
    # панель здоровья системы: последние запуски сборов/релея + вчерашние MISSING
    health = {}
    try:
        yesterday = date.today() - __import__("datetime").timedelta(days=1)
        health["missing"] = MetricSnapshot.query.filter(
            MetricSnapshot.date == yesterday, MetricSnapshot.status == "MISSING").count()
        last_collect = RunLog.query.filter(RunLog.kind == "daily_collect") \
            .order_by(RunLog.started_at.desc()).first()
        health["collect"] = last_collect.started_at.strftime("%d.%m %H:%M") if last_collect else "нет"
        relay = RunLog.query.filter(RunLog.kind == "gc_relay") \
            .order_by(RunLog.started_at.desc()).first()
        health["relay"] = (relay.started_at.strftime("%d.%m %H:%M") + " (" + (relay.details or "") + ")") if relay else "нет"
        health["gc_orders"] = GcOrder.query.count()
        health["comments"] = Comment.query.count()
    except Exception:
        pass
    return {"channels": Channel.query.filter_by(is_active=True).all(),
            "unread_notifications": unread, "period": period, "health": health}


# ---------------- Дашборд: 5 экранов ----------------

@app.route("/")
def overview():
    """Экран 1. Всё вместе — основные KPI всей системы."""
    d = _period_from_args()
    p = calc.period_report(*d)
    chart = _chart_series(*d)
    return render_template("overview.html", p=p, period=d, chart=chart,
                           report=_latest_report("weekly"))


@app.route("/comments")
def comments_screen():
    """Экран 6. Комментарии: вопросы, боли, возражения, идеи (по ТЗ п.4)."""
    d = _period_from_args()
    dig = comments_mod.digest(*d)
    recent = Comment.query.order_by(Comment.date.desc(), Comment.id.desc()).limit(50).all()
    return render_template("comments.html", d=dig, recent=recent, period=d)


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
    for ch in Channel.query.filter_by(is_active=True).all():
        rows.append({"ch": ch, "p": calc.period_report(*d, ch.id)})
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
    return render_template("content.html", items=items[:200], period=d, sort=sort,
                           platforms=sorted({i["item"].channel.platform for i in items}),
                           formats=sorted({i["item"].format for i in items if i["item"].format}),
                           rubrics=rubrics, types=types, compare=compare_text,
                           preset=preset,
                           total_matched=len(items))


@app.route("/registrations")
def registrations_screen():
    """Экран 4. Регистрации: источник → канал → CV."""
    d = _period_from_args()
    q = Registration.query.filter(Registration.date >= d[0], Registration.date <= d[1], Registration.status == "OK")
    by_source = {}
    for r in q.all():
        s = by_source.setdefault(r.utm_source or "(нет)", {"count": 0, "landings": {}})
        s["count"] += r.count or 0
        s["landings"][r.landing or "—"] = s["landings"].get(r.landing or "—", 0) + (r.count or 0)
    total = sum(v["count"] for v in by_source.values())
    cv_rows = []
    agg, _ = calc.aggregate(*d)
    total_reach = agg.get("reach")
    for src, v in sorted(by_source.items(), key=lambda x: -x[1]["count"]):
        plat = calc.UTM_TO_PLATFORM.get(src.lower())
        ch_reach = None
        if plat:
            for ch in Channel.query.filter_by(platform=plat).all():
                a, _ = calc.aggregate(*d, ch.id)
                ch_reach = (ch_reach or 0) + (a.get("reach") or 0)
        cv_rows.append({"source": src, "platform": plat or "—", "count": v["count"],
                        "share": v["count"] / total * 100 if total else None,
                        "reach": ch_reach, "CV": (v["count"] / ch_reach * 100) if ch_reach else None})
    return render_template("registrations.html", rows=cv_rows, total=total,
                           total_reach=total_reach,
                           total_cv=total / total_reach * 100 if total_reach else None, period=d,
                           chart=_chart_series(*d))


@app.route("/ai")
def ai_screen():
    """Экран 5. AI-аналитика."""
    weekly = Report.query.filter_by(rtype="weekly").order_by(Report.end.desc()).all()
    monthly = Report.query.filter_by(rtype="monthly").order_by(Report.end.desc()).all()
    anomalies = [n for n in Notification.query.filter(Notification.level == "warn").order_by(Notification.created_at.desc()).limit(20)]
    return render_template("ai.html", weekly=weekly, monthly=monthly, anomalies=anomalies)


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


@app.route("/collect", methods=["POST"])
def collect():
    results = connectors.run_daily_collection()
    flash("Сбор данных выполнен: " + "; ".join(results))
    return redirect(request.referrer or url_for("overview"))


@app.route("/import", methods=["GET", "POST"])
def import_csv():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Файл не выбран")
        elif not f.filename.lower().endswith((".csv", ".txt", ".tsv")):
            flash("Нужен CSV/XLSX-экспорт в текстовом виде")
        else:
            n = connectors.import_csv_channel(f)
            flash(f"Импортировано строк: {n}")
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
    keys = ["ai_api_key", "ai_base_url", "ai_model", "youtube_api_key", "youtube_channel_id",
            "livedune_token", "gc_account", "gc_api_key"]
    if request.method == "POST":
        for k in keys:
            set_setting(k, request.form.get(k, ""))
        db.session.commit()
        flash("Настройки сохранены")
        return redirect(url_for("settings"))
    return render_template("settings.html", values={k: get_setting(k) for k in keys})


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
    # динамика по дням: заказы и оплаты
    from sqlalchemy import func as _f
    days = []
    cur = d[0]
    from datetime import timedelta as _td
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
    chart = {"labels": days,
             "orders": [o_by_day.get(x, 0) for x in days],
             "payments": [p_by_day.get(x, 0) for x in days],
             "sums": [s_by_day.get(x, 0) for x in days]}
    recent_orders = GcOrder.query.order_by(GcOrder.created_at.desc().nullslast()).limit(20).all()
    return render_template("getcourse.html", f=f, period=d, logs=logs, chart=chart,
                           recent_orders=recent_orders,
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
    default = date.today()
    ws = data.get("window_start")
    if ws:
        try:
            default = datetime.strptime(ws[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    counts = {"users": 0, "deals": 0, "payments": 0}
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
    ps = request.args.get("start"); pe = request.args.get("end")
    preset = request.args.get("preset", "week")
    anchor = datetime.strptime(ps, "%Y-%m-%d").date() if ps else date.today()
    if ps and pe:
        return datetime.strptime(ps, "%Y-%m-%d").date(), datetime.strptime(pe, "%Y-%m-%d").date()
    if preset == "month":
        return calc.month_bounds(anchor)
    return calc.week_bounds(anchor)


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
    regs = {}
    for r in Registration.query.filter(Registration.date >= start, Registration.date <= end).all():
        regs[r.date.isoformat()] = regs.get(r.date.isoformat(), 0) + (r.count or 0)
    return {"labels": days, "reach": [reach.get(x) for x in days], "regs": [regs.get(x) for x in days]}


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
        sched.add_job(daily, CronTrigger(hour=3))
        sched.add_job(gc_daily, CronTrigger(hour=4))
        sched.add_job(weekly, CronTrigger(day_of_week="mon", hour=6))
        sched.add_job(monthly, CronTrigger(day=1, hour=7))
        sched.start()
    except Exception as e:
        app.logger.warning(f"Планировщик не запущен: {e}")


with app.app_context():
    db.create_all()
    # значения по умолчанию из окружения (Render env vars)
    import os as _os
    if _os.environ.get("GC_API_KEY") and not get_setting("gc_api_key"):
        set_setting("gc_api_key", _os.environ["GC_API_KEY"])
    if _os.environ.get("GC_ACCOUNT") and not get_setting("gc_account"):
        set_setting("gc_account", _os.environ["GC_ACCOUNT"])
    db.session.commit()
    seed.seed()

if not IS_SERVERLESS and (os.environ.get("RENDER") or os.environ.get("SMM_SCHEDULER", "1") == "1"):
    start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
