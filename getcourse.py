# -*- coding: utf-8 -*-
"""Коннектор GetCourse Export API (syrover.com).

Схема по документации ГК: запуск задачи экспорта -> опрос exports/{id} -> данные.
Лимит: 100 запросов за 2 часа; экспорты однопоточные (ошибка 905 -> ретраи).
Каждая сущность хранит текущее состояние + полная история в gc_events.

Приписки:
- входящая/исходящая: заказ с заполненным utm_source считается входящим
  (пришёл из трафика), без utm — исходящим (менеджер/рассылка/вручную);
  логику можно поменять в _direction, raw сохраняется полностью;
- новичок/старичок: первый заказ контакта в истории ГК — новичок,
  повторный (по user_id или email) — старичок; считается по загруженным заказам.
"""
import json, re, time, threading
from datetime import date, datetime, timedelta
import requests
from db import (db, get_setting, Notification, RunLog, Registration, GcOrder, GcPayment, GcEvent)

DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _base():
    return get_setting("gc_account", "https://syrover.com").rstrip("/") + "/pl/api/account"


def _key():
    return get_setting("gc_api_key")


def configured():
    return bool(_key())


def _get(path, params, timeout=90):
    r = requests.get(f"{_base()}/{path}", params={"key": _key(), **params}, timeout=timeout)
    try:
        return r.json()
    except Exception:
        raise RuntimeError(f"GetCourse вернул не JSON (HTTP {r.status_code}): {r.text[:200]}")


def _find_export_id(js):
    """Достаём id экспорта из ответа любого формата."""
    if not isinstance(js, dict):
        return None
    for k in ("export_id", "exportId", "id_export"):
        if js.get(k):
            return js[k]
    for sub in ("result", "info", "data"):
        v = js.get(sub)
        if isinstance(v, dict):
            r = _find_export_id(v)
            if r:
                return r
        if isinstance(v, (int, str)) and re.fullmatch(r"\d+", str(v)):
            return v
    return None


def _records(js):
    """Достаём список записей из ответа exports/{id} (формат 'поток данных в JSON-строке')."""
    if isinstance(js, dict):
        for k in ("info", "data", "result", "users", "deals", "payments"):
            v = js.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
            if isinstance(v, str) and v.startswith("["):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return parsed
                except Exception:
                    pass
            if isinstance(v, dict):
                r = _records(v)
                if r:
                    return r
        # возможно, сам ответ — список в строке
        for v in js.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    if isinstance(js, list) and js and isinstance(js[0], dict):
        return js
    return None


def request_export(kind, params, run_id, max_wait=600):
    """Запуск экспорта с ретраями по 905 + опрос готовности. Возвращает список записей."""
    js = None
    for attempt in range(4):
        js = _get(kind, params)
        if js.get("error_code") == 905 and attempt < 3:
            time.sleep(45)
            continue
        break
    if not js.get("success"):
        raise RuntimeError(f"GetCourse {kind}: {js.get('error_message') or js}")
    eid = _find_export_id(js)
    if not eid:
        raise RuntimeError(f"GetCourse {kind}: не найден export_id в {str(js)[:300]}")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(12)
        r = _get(f"exports/{eid}", {})
        if r.get("error_code") in (906, 907, 908) or (r.get("success") is False and not r.get("error_message")):
            continue  # ещё формируется
        recs = _records(r)
        if recs is not None:
            return recs
        if r.get("success") is False and r.get("error_message"):
            raise RuntimeError(f"GetCourse exports/{eid}: {r.get('error_message')}")
    raise RuntimeError(f"GetCourse exports/{eid}: превышено время ожидания")


# --------- разбор полей (защищённо: несколько вариантов имён) ---------

def _f(rec, *names, default=None):
    rl = {str(k).lower().strip(): v for k, v in rec.items()}
    for n in names:
        for k, v in rl.items():
            if k == n.lower():
                if v in ("", None):
                    return default
                return v
    return default


def _dt(v):
    if not v:
        return None
    if isinstance(v, dict) and "date" in v:
        v = v["date"]
    s = str(v).strip().replace("T", " ")
    s = re.sub(r"\.\d+$", "", s)
    for fmt in (DATE_FMT, "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.\-]", "", str(v)).replace(",", ".")
    if s in ("", "-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _direction(utm_source):
    return "incoming" if (utm_source or "").strip() else "outgoing"


# --------- синхронизация ---------

def _log_event(entity, entity_id, payload):
    db.session.add(GcEvent(entity=entity, entity_id=str(entity_id),
                           payload=json.dumps(payload, ensure_ascii=False)))


def sync_window(start: date, end: date, run_id):
    """Синхронизирует пользователей, заказы и оплаты за окно дат."""
    stats = {"users": 0, "deals": 0, "payments": 0}
    params = {"created_at[from]": start.isoformat(), "created_at[to]": end.isoformat()}

    users = request_export("users", params, run_id)
    for u in users:
        uid = _num(_f(u, "id", "user_id", "ID"))
        if uid is None:
            continue
        created = _dt(_f(u, "created_at", "created", "date_created", "Дата создания"))
        reg = Registration.query.filter_by(gc_user_id=int(uid)).first()
        if not reg:
            reg = Registration(count=1, gc_user_id=int(uid))
            db.session.add(reg)
        reg.date = (created.date() if created else start)
        reg.utm_source = str(_f(u, "utm_source", "utm_source_text") or "")[:64]
        reg.utm_medium = str(_f(u, "utm_medium") or "")[:64]
        reg.utm_campaign = str(_f(u, "utm_campaign") or "")[:128]
        reg.landing = str(_f(u, "page_url", "landing", "referer", "landing_page") or "")[:255]
        reg.status = "OK"
        _log_event("user", uid, u)
        stats["users"] += 1
    db.session.flush()

    deals = request_export("deals", params, run_id)
    for d in deals:
        did = _num(_f(d, "id", "deal_id", "ID"))
        if did is None:
            continue
        created = _dt(_f(d, "created_at", "created", "Дата создания"))
        o = GcOrder.query.get(int(did)) or GcOrder(id=int(did))
        db.session.add(o)
        o.deal_number = str(_f(d, "deal_number", "number", "Номер заказа") or "")[:64]
        o.created_at = created
        o.date = created.date() if created else start
        o.user_id = _num(_f(d, "user_id", "userID"))
        o.email = str(_f(d, "email", "E-mail", "user_email") or "")[:255]
        o.phone = str(_f(d, "phone", "Телефон", "user_phone") or "")[:64]
        o.product = str(_f(d, "product_title", "product", "offer_name", "Название продукта",
                           "product_name", "Название предложения") or "")[:255]
        o.amount = _num(_f(d, "price", "amount", "deal_price", "Сумма заказа", "Сумма"))
        o.currency = str(_f(d, "currency", "Валюта") or "RUB")[:8]
        o.status = str(_f(d, "status", "deal_status", "Статус") or "")[:32]
        o.status_title = str(_f(d, "status_title", "deal_status_name", "Название статуса") or "")[:64]
        o.utm_source = str(_f(d, "utm_source") or "")[:64]
        o.utm_medium = str(_f(d, "utm_medium") or "")[:64]
        o.utm_campaign = str(_f(d, "utm_campaign") or "")[:128]
        o.direction = _direction(o.utm_source)
        o.updated_at = datetime.utcnow()
        _log_event("deal", did, d)
        stats["deals"] += 1
    db.session.flush()
    _recompute_customer_status()

    payments = request_export("payments", params, run_id)
    for p in payments:
        pid = _num(_f(p, "id", "payment_id", "ID"))
        if pid is None:
            continue
        created = _dt(_f(p, "created_at", "created", "Дата создания", "payed_at"))
        pay = GcPayment.query.get(int(pid)) or GcPayment(id=int(pid))
        db.session.add(pay)
        pay.created_at = created
        pay.date = created.date() if created else start
        pay.user_id = _num(_f(p, "user_id", "userID"))
        pay.email = str(_f(p, "email", "E-mail", "user_email") or "")[:255]
        pay.amount = _num(_f(p, "amount", "sum", "Сумма", "price", "Сумма оплаты"))
        pay.currency = str(_f(p, "currency", "Валюта") or "RUB")[:8]
        pay.status = str(_f(p, "status", "Статус") or "")[:32]
        pay.deal_id = _num(_f(p, "deal_id", "order_id"))
        pay.product = str(_f(p, "product_title", "product", "Название продукта") or "")[:255]
        pay.updated_at = datetime.utcnow()
        _log_event("payment", pid, p)
        stats["payments"] += 1
    db.session.commit()
    return stats


def _recompute_customer_status():
    """Новичок = первый заказ контакта; старичок = были более ранние заказы."""
    orders = GcOrder.query.order_by(GcOrder.created_at).all()
    seen = set()
    for o in orders:
        key = o.user_id or (o.email or "").lower() or None
        if key is None:
            o.customer_status = None
            continue
        o.customer_status = "returning" if key in seen else "new"
        seen.add(key)


def sync_getcourse(days=5, backfill_months=0, threaded=False):
    """Ежедневная синхронизация: последние N дней (запаздывающие статусы пересобираются).
    backfill_months: догрузка истории помесячно (кнопка на экране GetCourse)."""
    def _run():
        run_id = None
        try:
            r = RunLog(kind="gc_sync", status="OK")
            db.session.add(r)
            db.session.commit()
            run_id = r.id
            end = date.today()
            start = end - timedelta(days=days)
            stats = sync_window(start, end, run_id)
            for m in range(backfill_months):
                me = (end.replace(day=1) - timedelta(days=1)) if m == 0 else \
                     ((end.replace(day=1) - timedelta(days=1)).replace(day=1) - timedelta(days=1))
                ms = me.replace(day=1)
                stats_m = sync_window(ms, me, run_id)
                for k in stats_m:
                    stats[k] = stats.get(k, 0) + stats_m[k]
                end = ms - timedelta(days=1)
                start = end - timedelta(days=days)
            r.details = f"users={stats['users']} deals={stats['deals']} payments={stats['payments']}"
            db.session.commit()
            db.session.add(Notification(level="info", message=(
                f"GetCourse: синхронизировано пользователей {stats['users']}, "
                f"заказов {stats['deals']}, оплат {stats['payments']}.")))
            db.session.commit()
        except Exception as e:
            db.session.add(Notification(level="error", message=f"Ошибка синхронизации GetCourse: {e}"))
            db.session.commit()
            raise
    if threaded:
        threading.Thread(target=_run, daemon=True).start()
        return "запущена в фоне"
    _run()
    return "завершена"


# --------- агрегаты для дашборда и отчётов ---------

def funnel(start: date, end: date):
    """Воронка и разрезы за период: регистрации -> заказы -> оплаты."""
    regs = Registration.query.filter(Registration.date >= start, Registration.date <= end).count()
    orders = GcOrder.query.filter(GcOrder.date >= start, GcOrder.date <= end)
    pays = GcPayment.query.filter(GcPayment.date >= start, GcPayment.date <= end)
    out = {
        "registrations": regs,
        "orders": orders.count(),
        "payments": pays.count(),
        "orders_sum": db.session.query(db.func.coalesce(db.func.sum(GcPayment.amount), 0)).filter(
            GcPayment.date >= start, GcPayment.date <= end, GcPayment.status == "accepted").scalar(),
    }
    if regs:
        out["cr_reg_order"] = out["orders"] / regs * 100
    if out["orders"]:
        out["cr_order_pay"] = out["payments"] / out["orders"] * 100
    out["by_customer"] = dict(db.session.query(
        GcOrder.customer_status, db.func.count(GcOrder.id)).filter(
        GcOrder.date >= start, GcOrder.date <= end).group_by(GcOrder.customer_status).all())
    out["by_direction"] = dict(db.session.query(
        GcOrder.direction, db.func.count(GcOrder.id)).filter(
        GcOrder.date >= start, GcOrder.date <= end).group_by(GcOrder.direction).all())
    out["by_status"] = {k or "—": v for k, v in db.session.query(
        GcOrder.status_title, db.func.count(GcOrder.id)).filter(
        GcOrder.date >= start, GcOrder.date <= end).group_by(GcOrder.status_title).all()}
    out["top_products"] = db.session.query(
        GcOrder.product, db.func.count(GcOrder.id), db.func.coalesce(db.func.sum(GcOrder.amount), 0)).filter(
        GcOrder.date >= start, GcOrder.date <= end).group_by(GcOrder.product).order_by(
        db.func.count(GcOrder.id).desc()).limit(10).all()
    out["top_sources"] = db.session.query(
        GcOrder.utm_source, db.func.count(GcOrder.id), db.func.coalesce(db.func.sum(GcOrder.amount), 0)).filter(
        GcOrder.date >= start, GcOrder.date <= end).group_by(GcOrder.utm_source).order_by(
        db.func.count(GcOrder.id).desc()).limit(10).all()
    return out
