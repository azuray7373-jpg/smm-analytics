# -*- coding: utf-8 -*-
"""Создание сервиса на Render одной командой (после привязки карты в billing).

Использование:
    RENDER_API_KEY=rnd_... GC_API_KEY=jxCU... python deploy_render.py

Создаёт web service из репозитория GitHub, задаёт все env-переменные.
Если сервис уже существует — обновляет env-переменные и триггерит деплой.
"""
import json, os, secrets, sys, urllib.request

REPO = "https://github.com/azuray7373-jpg/smm-analytics"
RK = os.environ.get("RENDER_API_KEY")
GC_KEY = os.environ.get("GC_API_KEY", "")
GC_ACC = os.environ.get("GC_ACCOUNT", "https://syrover.com")
NAME = "smm-analytics"


def api(method, path, body=None):
    req = urllib.request.Request("https://api.render.com/v1" + path,
        data=json.dumps(body).encode() if body else None, method=method,
        headers={"Authorization": f"Bearer {RK}", "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw[:300]}


def main():
    if not RK:
        sys.exit("Нужен RENDER_API_KEY")
    st, owners = api("GET", "/owners?limit=10")
    owner = owners[0]["owner"]["id"]
    env_vars = [
        {"key": "PYTHON_VERSION", "value": "3.12.6"},
        {"key": "SECRET_KEY", "value": secrets.token_hex(24)},
        {"key": "SMM_DEMO", "value": os.environ.get("SMM_DEMO", "1")},
        {"key": "SMM_SCHEDULER", "value": "1"},
        {"key": "GC_ACCOUNT", "value": GC_ACC},
    ]
    if GC_KEY:
        env_vars.append({"key": "GC_API_KEY", "value": GC_KEY})
    st, services = api("GET", "/services?limit=100")
    existing = next((s["service"]["id"] for s in services
                     if s["service"]["name"] == NAME), None)
    if existing:
        api("PATCH", f"/services/{existing}", {"envVars": env_vars})
        api("POST", f"/services/{existing}/deploys", {})
        print(f"Сервис обновлён и деплоится: https://dashboard.render.com/web/{existing}")
        return
    body = {
        "type": "web_service", "name": NAME, "ownerId": owner,
        "repo": REPO, "branch": "main", "autoDeploy": "yes",
        "buildCommand": "pip install -r requirements.txt",
        "startCommand": "gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4",
        "serviceDetails": {
            "env": "python", "plan": "free", "region": "frankfurt",
            "healthCheckPath": "/",
            "envSpecificDetails": {
                "pythonVersion": "3.12.6",
                "buildCommand": "pip install -r requirements.txt",
                "startCommand": "gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4"}},
        "envVars": env_vars,
    }
    st, res = api("POST", "/services", body)
    if st >= 400:
        sys.exit(f"Ошибка {st}: {json.dumps(res, ensure_ascii=False)[:300]}\n"
                 "Если 402 — добавьте карту: https://dashboard.render.com/billing")
    sid = res["service"]["id"]
    print(f"Сервис создан: https://dashboard.render.com/web/{sid}")
    print("Через 3-5 минут приложение будет доступно по адресу вида "
          f"https://{NAME}.onrender.com")


if __name__ == "__main__":
    main()
