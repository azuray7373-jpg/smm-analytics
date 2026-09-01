# -*- coding: utf-8 -*-
"""Развёртывание на PythonAnywhere через API (бесплатный тариф, без карты).

Использование:
    PA_TOKEN=... python deploy_pythonanywhere.py

Что делает:
  1. определяет username по токену;
  2. загружает файлы проекта в /home/<user>/smm/;
  3. создаёт virtualenv (python3.10);
  4. создаёт web app <user>.pythonanywhere.com с WSGI-обёрткой
     (пакеты ставятся при первом запуске, ключи ГК вшиты в WSGI-файл);
  5. подключает статику и перезагружает приложение.
"""
import json, os, sys, time, secrets
import urllib.request, urllib.error

TOKEN = os.environ.get("PA_TOKEN")
API = "https://www.pythonanywhere.com/api"
PROJ = "smm"
EXCLUDE_DIRS = {".git", "venv", "instance", "__pycache__", ".vercel", "node_modules"}
EXCLUDE_FILES = {".gitignore", ".cron_token", "server.log", "deploy_render.py",
                 "deploy_pythonanywhere.py", "render.yaml", "Procfile", "vercel.json"}
PY_VER = "python3.10"


def call(method, path, body=None, raw=None, headers=None, timeout=120):
    h = {"Authorization": f"Token {TOKEN}"}
    if headers:
        h.update(headers)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None and "Content-Type" not in h:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt.strip().startswith(("{", "[")) else txt)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def project_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn in EXCLUDE_FILES:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            out.append((rel, full))
    return out


def main():
    if not TOKEN:
        sys.exit("Нужен PA_TOKEN")
    st, user = call("GET", "/api/v0/user/")
    if st != 200:
        sys.exit(f"Токен не принят: {st} {user}")
    username = user["username"] if isinstance(user, dict) else user.get("username")
    print("username:", username)

    home = f"/home/{username}/{PROJ}"

    # 1) файлы проекта
    st, msg = call("POST", f"/api/v0/user/{username}/files/path{home}/", body={})
    for rel, full in project_files(os.path.dirname(os.path.abspath(__file__))):
        content = open(full, "rb").read()
        st, msg = call("PUT", f"/api/v0/user/{username}/files/path{home}/{rel}", raw=content,
                       headers={"Content-Type": "application/octet-stream"})
        if st not in (200, 201):
            print(f"  !! {rel}: {st} {msg}")
    print(f"файлы загружены: {len(project_files(os.path.dirname(os.path.abspath(__file__))))}")

    # 2) virtualenv
    st, msg = call("POST", f"/api/v0/user/{username}/virtualenvs/",
                   body={"python_version": PY_VER, "path": f"{home}/venv"})
    print("virtualenv:", st, str(msg)[:150])
    print("  (создаётся асинхронно ~30-60с; ждём)")
    time.sleep(60)

    # 3) web app
    domain = f"{username}.pythonanywhere.com"
    st, msg = call("POST", f"/api/v0/user/{username}/webapps/",
                   body={"domain": domain, "python_version": PY_VER})
    print("webapp:", st, str(msg)[:150])

    gc_key = os.environ.get("GC_API_KEY", "")
    secret = os.environ.get("SECRET_KEY") or secrets.token_hex(24)
    wsgi = f'''import os, sys
os.environ.setdefault("GC_ACCOUNT", "https://syrover.com")
os.environ.setdefault("GC_API_KEY", "{gc_key}")
os.environ.setdefault("SECRET_KEY", "{secret}")
os.environ.setdefault("SMM_DEMO", "1")
os.environ.setdefault("SMM_SCHEDULER", "1")

project = "{home}"
if project not in sys.path:
    sys.path.insert(0, project)

import subprocess
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", project + "/requirements.txt"])

from app import app as application
'''
    wsgi_path = f"/var/www/{username}_pythonanywhere_com_wsgi.py"
    st, msg = call("PUT", f"/api/v0/user/{username}/files/path{wsgi_path}",
                   raw=wsgi.encode(), headers={"Content-Type": "text/plain"})
    print("wsgi:", st, str(msg)[:150])

    # 4) конфиг webapp: virtualenv + статика
    st, msg = call("PATCH", f"/api/v0/user/{username}/webapps/{domain}/",
                   body={"virtualenv_path": f"{home}/venv",
                         "static_files": [{"url": "/static/", "path": f"{home}/static"}]})
    print("webapp config:", st, str(msg)[:200])

    # 5) reload
    st, msg = call("POST", f"/api/v0/user/{username}/webapps/{domain}/reload/", body={})
    print("reload:", st, str(msg)[:150])
    print(f"\nГотово: https://{domain} (первый запуск ~1-2 минуты: pip install в WSGI)")


if __name__ == "__main__":
    main()
