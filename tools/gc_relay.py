# -*- coding: utf-8 -*-
"""Релей синхронизации GetCourse -> приложение.

Запускается там, где есть открытый доступ в интернет (GitHub Actions, локальный ПК):
  1. забирает пользователей/заказы/оплаты из Export API GetCourse за окно;
  2. отправляет их в приложение (PythonAnywhere) на POST /api/ingest.

Запуск: python tools/gc_relay.py
Env: GC_ACCOUNT (default https://syrover.com), GC_API_KEY, PA_URL, PA_INGEST_TOKEN, DAYS (default 5)
"""
import json, os, sys, time
from datetime import date, timedelta
import requests

GC_ACCOUNT = os.environ.get("GC_ACCOUNT", "https://syrover.com").rstrip("/")
KEY = os.environ.get("GC_API_KEY")
PA_URL = os.environ.get("PA_URL", "").rstrip("/")
TOKEN = os.environ.get("PA_INGEST_TOKEN")
DAYS = int(os.environ.get("DAYS", "5"))


def gc_get(path, params):
    r = requests.get(f"{GC_ACCOUNT}/pl/api/account/{path}",
                     params={"key": KEY, **params}, timeout=90)
    return r.json()


def find_export_id(js):
    if isinstance(js, dict):
        if isinstance(js.get("info"), dict) and js["info"].get("export_id"):
            return js["info"]["export_id"]
        for k in ("export_id", "exportId"):
            if js.get(k):
                return js[k]
    return None


def request_export(kind, params):
    for attempt in range(3):
        js = gc_get(kind, params)
        code = js.get("error_code")
        if code == 903:
            raise _RateLimited(f"{kind}: лимит API (903)")
        if code == 905 and attempt < 2:
            time.sleep(90); continue
        break
    if js.get("error_code") == 905:
        raise _RateLimited(f"{kind}: очередь экспортов ГК занята (905) дольше 3 минут")
    if not js.get("success"):
        raise RuntimeError(f"{kind}: {js.get('error_message')}")
    eid = find_export_id(js)
    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(45)
        r = gc_get(f"exports/{eid}", {})
        code = r.get("error_code")
        if code == 903:
            raise _RateLimited(f"{kind}: лимит API (903) при опросе")
        if code in (905, 906, 907, 908, 909):
            continue
        info = r.get("info")
        if isinstance(info, dict) and "fields" in info and "items" in info:
            return [dict(zip(info["fields"], row)) for row in info["items"] if isinstance(row, list)]
        if isinstance(info, list) and (not info or isinstance(info[0], dict)):
            return info
        if isinstance(info, str) and info.startswith("["):
            return json.loads(info)
        raise RuntimeError(f"exports/{eid}: {str(r)[:200]}")
    raise RuntimeError(f"exports/{eid}: превышено время ожидания")


def main():
    if not (KEY and PA_URL and TOKEN):
        sys.exit("Нужны GC_API_KEY, PA_URL, PA_INGEST_TOKEN")
    attempts = int(os.environ.get("ATTEMPTS", "5"))
    pause = int(os.environ.get("RETRY_PAUSE", "900"))
    import traceback
    for i in range(attempts):
        try:
            return _run_once()
        except _RateLimited as e:
            if i == attempts - 1:
                print(f"лимит/очередь API ГК держится {attempts} попыток; сдаёмся до следующего запуска")
                sys.exit(2)
            print(f"попытка {i+1}: {e}; пауза {pause}с и повтор")
            time.sleep(pause)
        except Exception as e:
            if i == attempts - 1:
                traceback.print_exc()
                sys.exit(1)
            print(f"попытка {i+1}: ошибка {e}; пауза {pause}с и повтор")
            time.sleep(pause)


class _RateLimited(Exception):
    pass


def _run_once():
    end = date.today()
    start = end - timedelta(days=DAYS)
    params = {"created_at[from]": start.isoformat(), "created_at[to]": end.isoformat()}
    def slim(rows, keys):
        out = []
        for r in rows:
            out.append({k: r.get(k) for k in keys if r.get(k) not in (None, "")})
        return out
    users = request_export("users", params)
    deals = request_export("deals", params)
    payments = request_export("payments", params)
    # PA ограничивает размер запроса (~10МБ): полная книга сделок не влезает,
    # поэтому шлём пользователей, затем сделки чанками, затем оплаты
    slim_users = slim(users, ["id", "Создан", "created_at", "utm_source", "LM_utm_source",
                              "gc_system_user_utm_source", "utm_medium", "utm_campaign"])
    r = requests.post(f"{PA_URL}/api/ingest",
                      json={"window_start": start.isoformat(), "users": slim_users},
                      headers={"X-Ingest-Token": TOKEN}, timeout=600)
    print("ingest users:", r.status_code, r.text[:120])
    r.raise_for_status()
    for i in range(0, len(deals), 1500):
        r = requests.post(f"{PA_URL}/api/ingest",
                          json={"window_start": start.isoformat(), "deals": deals[i:i + 1500]},
                          headers={"X-Ingest-Token": TOKEN}, timeout=600)
        print(f"ingest deals[{i // 1500}]:", r.status_code, r.text[:120])
        r.raise_for_status()
    r = requests.post(f"{PA_URL}/api/ingest",
                      json={"window_start": start.isoformat(), "payments": payments},
                      headers={"X-Ingest-Token": TOKEN}, timeout=600)
    print("ingest payments:", r.status_code, r.text[:120])
    r.raise_for_status()
    return
    print(f"получено: users={len(users)} deals={len(deals)} payments={len(payments)}")


if __name__ == "__main__":
    main()
