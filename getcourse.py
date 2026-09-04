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
from calc import ttl_cache as calc_ttl

DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _base():
    return get_setting("gc_account", "https://syrover.com").rstrip("/") + "/pl/api/account"


def _key():
    return get_setting("gc_api_key")


def configured():
    return bool(_key())


def can_reach(timeout=8):
    """Быстрая проверка сетевой доступности аккаунта ГК (для хостингов с прокси)."""
    try:
        requests.get(_base() + "/groups", params={"key": _key()}, timeout=timeout)
        return True
    except Exception:
        return False


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
        info = js.get("info")
        if isinstance(info, dict) and "fields" in info and "items" in info:
            fields = info["fields"]
            return [dict(zip(fields, row)) for row in info["items"] if isinstance(row, list)]
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
    """Запуск экспорта с ретраями по 905 + опрос готовности. Возвращает список записей.
    Лимит ExportAPI — 100 запросов за 2 часа, поэтому опрашиваем редко (30с)."""
    js = None
    for attempt in range(4):
        js = _get(kind, params)
        if js.get("error_code") == 903:
            raise RuntimeError("GetCourse: лимит API (903), повторите через ~2 часа")
        if js.get("error_code") == 905 and attempt < 3:
            time.sleep(60)
            continue
        break
    if not js.get("success"):
        raise RuntimeError(f"GetCourse {kind}: {js.get('error_message') or js}")
    eid = _find_export_id(js)
    if not eid:
        raise RuntimeError(f"GetCourse {kind}: не найден export_id в {str(js)[:300]}")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(45)
        r = _get(f"exports/{eid}", {})
        if r.get("error_code") == 903:
            raise RuntimeError("GetCourse: лимит API (903), повторите через ~2 часа")
        if r.get("error_code") in (905, 906, 907, 908, 909):
            continue  # ещё формируется / файл не создан
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


def _fz(rec, *needles):
    """Первое поле, имя которого содержит подстроку (регистронезависимо) —
    для реальных русских названий полей выгрузок ГК."""
    rl = {str(k).lower().strip(): v for k, v in rec.items()}
    for n in needles:
        for k, v in rl.items():
            if n in k:
                return v
    return None


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


# -*- coding: utf-8 -*-
def _ingest_users(users, default_day):
    n = 0
    for u in users:
        uid = _num(_f(u, "id", "user_id", "ID"))
        if uid is None:
            continue
        created = _dt(_f(u, "created_at", "created", "date_created", "создан",
                          "дата создания") or _fz(u, "создан", "дата"))
        reg = Registration.query.filter_by(gc_user_id=int(uid)).first()
        if not reg:
            reg = Registration(count=1, gc_user_id=int(uid))
            db.session.add(reg)
        reg.date = (created.date() if created else default_day)
        if created:
            reg.created_at = created
        reg.utm_source = str(_f(u, "utm_source", "lm_utm_source", "gc_system_user_utm_source")
                             or _fz(u, "utm_source") or "")[:64]
        reg.utm_medium = str(_f(u, "utm_medium", "lm_utm_medium", "gc_system_user_utm_medium") or "")[:64]
        reg.utm_campaign = str(_f(u, "utm_campaign", "lm_utm_campaign",
                                  "gc_system_user_utm_campaign") or "")[:128]
        reg.landing = str(_f(u, "page_url", "landing", "referer", "landing_page",
                             "страница регистрации") or "")[:255]
        reg.status = "OK"
        _log_event("user", uid, u)
        n += 1
    db.session.commit()
    return n


def _ingest_deals(deals, default_day):
    n = 0
    for d in deals:
        did = _num(_f(d, "id", "deal_id") or _fz(d, "id заказа", "id сделки", "deal_id"))
        if did is None:
            continue
        created = _dt(_f(d, "created_at", "created", "создан", "дата создания")
                      or _fz(d, "созда"))
        o = GcOrder.query.get(int(did)) or GcOrder(id=int(did))
        db.session.add(o)
        o.deal_number = str(_f(d, "deal_number", "number", "номер заказа")
                            or _fz(d, "номер") or "")[:64]
        o.created_at = created
        o.date = created.date() if created else default_day
        o.user_id = _num(_f(d, "user_id", "userid") or _fz(d, "id пользователя", "id юзера"))
        o.email = str(_f(d, "email", "e-mail") or _fz(d, "email", "e-mail") or "")[:255]
        o.phone = str(_f(d, "phone", "телефон") or _fz(d, "телефон") or "")[:64]
        o.product = str(_f(d, "product_title", "product", "название продукта",
                           "product_name", "название предложения")
                        or _fz(d, "предложен", "продукт", "товар", "наименован") or "")[:255]
        o.amount = _num(_f(d, "price", "amount", "deal_price", "сумма заказа")
                        or _fz(d, "сумма", "цена", "стоимост", "price"))
        o.currency = str(_f(d, "currency", "валюта") or _fz(d, "валют") or "RUB")[:8]
        o.status = str(_f(d, "status", "deal_status", "статус") or "")[:32]
        o.status_title = str(_f(d, "status_title", "deal_status_name", "название статуса")
                             or _fz(d, "название статуса") or "")[:64]
        o.utm_source = str(_f(d, "utm_source", "lm_utm_source", "order_utm_source",
                              "gc_system_deal_utm_source") or _fz(d, "utm_source") or "")[:64]
        o.utm_medium = str(_f(d, "utm_medium", "lm_utm_medium", "order_utm_medium",
                              "gc_system_deal_utm_medium") or _fz(d, "utm_medium") or "")[:64]
        o.utm_campaign = str(_f(d, "utm_campaign", "lm_utm_campaign", "order_utm_campaign",
                                "gc_system_deal_utm_campaign") or _fz(d, "utm_campaign") or "")[:128]
        o.direction = _direction(o.utm_source)
        o.updated_at = datetime.utcnow()
        _log_event("deal", did, d)
        n += 1
    db.session.commit()
    _recompute_customer_status()
    db.session.commit()
    return n


def _ingest_payments(payments, default_day):
    n = 0
    for p in payments:
        pid = _num(_f(p, "id", "payment_id", "ID"))
        if pid is None:
            continue
        created = _dt(_f(p, "created_at", "created", "создан", "дата создания", "payed_at")
                      or _fz(p, "созда", "оплачен"))
        pay = GcPayment.query.get(int(pid)) or GcPayment(id=int(pid))
        db.session.add(pay)
        pay.created_at = created
        pay.date = created.date() if created else default_day
        pay.user_id = _num(_f(p, "user_id", "userid") or _fz(p, "id пользователя"))
        pay.email = str(_f(p, "email", "e-mail") or _fz(p, "email") or "")[:255]
        pay.amount = _num(_f(p, "amount", "sum", "сумма", "price", "сумма оплаты")
                          or _fz(p, "сумма", "price"))
        pay.currency = str(_f(p, "currency", "валюта") or _fz(p, "валют") or "RUB")[:8]
        pay.status = str(_f(p, "status", "статус") or "")[:32]
        pay.deal_id = _num(_f(p, "deal_id", "order_id") or _fz(p, "id заказа", "id сделки"))
        pay.product = str(_f(p, "product_title", "product", "название продукта")
                          or _fz(p, "предложен", "продукт") or "")[:255]
        pay.updated_at = datetime.utcnow()
        _log_event("payment", pid, p)
        n += 1
    db.session.commit()
    return n


def sync_window(start, end, run_id):
    """Синхронизирует пользователей, заказы и оплаты за окно дат (блокирующий режим для VM-хостов)."""
    params = {"created_at[from]": start.isoformat(), "created_at[to]": end.isoformat()}
    users = request_export("users", params, run_id)
    nu = _ingest_users(users, start)
    deals = request_export("deals", params, run_id)
    nd = _ingest_deals(deals, start)
    payments = request_export("payments", params, run_id)
    np_ = _ingest_payments(payments, start)
    return {"users": nu, "deals": nd, "payments": np_}


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


# --------- пошаговая синхронизация для serverless (Vercel) ---------
# Каждый вызов gc_step() делает ровно один HTTP-запрос к ГК; состояние хранится
# в базе и продолжается от вызова к вызову (пинг /cron раз в 5 минут).

def _sync_state():
    from db import GcSyncState
    st = db.session.get(GcSyncState, 1)
    if not st:
        st = GcSyncState(id=1)
        db.session.add(st)
        db.session.commit()
    return st


def gc_start(days=5):
    """Начать новый цикл синхронизации, если предыдущий завершён."""
    st = _sync_state()
    if st.phase != "idle":
        return False
    st.window_end = date.today()
    st.window_start = date.today() - timedelta(days=days)
    st.phase = "start_users"
    st.export_id = None
    st.stats = ""
    db.session.commit()
    return True


def _rate_limited_until():
    from db import get_setting
    try:
        return datetime.fromisoformat(get_setting("gc_rate_limited_until", ""))
    except ValueError:
        return None


def _set_rate_limit(minutes=120):
    from db import set_setting
    set_setting("gc_rate_limited_until", (datetime.utcnow() + timedelta(minutes=minutes)).isoformat())
    db.session.commit()


def gc_status():
    """Статус для экрана: шаг только если цикл уже идёт (не начинает новый)."""
    st = _sync_state()
    if st.phase == "idle":
        return "простой: синхронизация запустится по крону или кнопке"
    return gc_step()


def gc_step():
    """Один шаг автомата. Возвращает статус для ответа /cron."""
    st = _sync_state()
    until = _rate_limited_until()
    if until and datetime.utcnow() < until:
        return f"лимит API ГК, пауза до {until.strftime('%H:%M')} UTC"
    try:
        if st.phase == "idle":
            return "idle"
        params = {"created_at[from]": st.window_start.isoformat(),
                  "created_at[to]": st.window_end.isoformat()} if st.window_start else {}
        if st.phase.startswith("start_"):
            kind = st.phase.split("_", 1)[1]
            js = _get(kind, params)
            msg = str(js.get("error_message") or "")
            if js.get("error_code") == 903 or "подписан" in msg:
                _set_rate_limit(120)
                return f"лимит API ГК: пауза 2 часа ({msg[:60]})"
            if js.get("error_code") in (905, 906):
                return f"очередь экспортов ГК занята, повтор позже ({js.get('error_code')})"
            if not js.get("success"):
                st.phase = "idle"
                db.session.commit()
                raise RuntimeError(str(js)[:200])
            st.export_id = _find_export_id(js)
            st.phase = "wait_" + kind
            st.updated_at = datetime.utcnow()
            db.session.commit()
            return f"экспорт {kind} запущен (#{st.export_id})"
        if st.phase.startswith("wait_"):
            kind = st.phase.split("_", 1)[1]
            r = _get(f"exports/{st.export_id}", {})
            if r.get("error_code") == 903:
                _set_rate_limit(120)
                return "лимит API ГК: пауза 2 часа"
            if r.get("error_code") in (905, 906, 907, 908, 909):
                return f"файл {kind} ещё формируется ({r.get('error_code')})"
            recs = _records(r)
            if recs is None:
                raise RuntimeError(f"exports/{st.export_id}: {str(r)[:200]}")
            default = st.window_start or date.today()
            if kind == "users":
                n = _ingest_users(recs, default)
            elif kind == "deals":
                n = _ingest_deals(recs, default)
            else:
                n = _ingest_payments(recs, default)
            st.stats = (st.stats or "") + f"{kind}={n}; "
            nxt = {"users": "start_deals", "deals": "start_payments", "payments": "idle"}
            st.phase = nxt[kind]
            st.export_id = None
            st.updated_at = datetime.utcnow()
            db.session.commit()
            if st.phase == "idle":
                db.session.add(Notification(level="info",
                    message=f"GetCourse: синхронизация завершена ({st.stats})"))
                db.session.commit()
                return f"готово: {st.stats}"
            return f"загружено {kind}={n}, далее {st.phase}"
    except Exception as e:
        db.session.add(Notification(level="error",
            message=f"Ошибка шага синхронизации ГК: {e}"))
        db.session.commit()
        return f"ошибка: {e}"
    return "idle"


def gc_run_steps(seconds=35):
    """Выполнять шаги, пока есть время (кнопка/долгий вызов в пределах maxDuration)."""
    import time as _t
    deadline = _t.time() + seconds
    last = "idle"
    gc_start(days=5)
    while _t.time() < deadline:
        last = gc_step()
        if last.startswith("готово") or last.startswith("ошибка"):
            break
        _t.sleep(20)
    return last


def sync_getcourse(days=5, backfill_months=0, threaded=False):
    """Блокирующая синхронизация для VM-хостов (обычный режим с ожиданием готовности экспортов)."""
    def _run():
        # поток не наследует контекст Flask — создаём свой, иначе БД недоступна
        from app import app as _app
        with _app.app_context():
            try:
                r = RunLog(kind="gc_sync", status="OK")
                db.session.add(r)
                db.session.commit()
                run_id = r.id
                end = date.today()
                start = end - timedelta(days=days)
                stats = sync_window(start, end, run_id)
                r.details = f"users={stats['users']} deals={stats['deals']} payments={stats['payments']}"
                db.session.commit()
                db.session.add(Notification(level="info", message=(
                    f"GetCourse: синхронизировано пользователей {stats['users']}, "
                    f"заказов {stats['deals']}, оплат {stats['payments']}.")))
                db.session.commit()
            except Exception as e:
                db.session.add(Notification(level="error",
                    message=f"Ошибка синхронизации GetCourse: {e}"))
                db.session.commit()
                raise
    if threaded:
        threading.Thread(target=_run, daemon=True).start()
        return "запущена в фоне"
    _run()
    return "завершена"


@calc_ttl(120)
def funnel(start: date, end: date):
    """Воронка и разрезы за период: регистрации -> заказы -> оплаты."""
    regs = Registration.query.filter(Registration.date >= start, Registration.date <= end,
                                     ~Registration.utm_source.like("demo_%")).count()
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
