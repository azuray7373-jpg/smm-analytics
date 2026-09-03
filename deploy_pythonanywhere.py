# -*- coding: utf-8 -*-
"""Развёртывание на PythonAnywhere через API (free, без карты).

PA_TOKEN=... PA_USERNAME=... GC_API_KEY=... python deploy_pythonanywhere.py
"""
import json, os, sys, time, secrets
import requests

API = "https://www.pythonanywhere.com/api/v0/user/{u}"
TOKEN = os.environ.get("PA_TOKEN")
USER = os.environ.get("PA_USERNAME", "anazanaxus")
DOMAIN = f"{USER}.pythonanywhere.com"
HOME = f"/home/{USER}"
PROJ = f"{HOME}/smm"
GC_KEY = os.environ.get("GC_API_KEY", "")
SECRET = os.environ.get("SECRET_KEY") or secrets.token_hex(24)

EXCLUDE_DIRS = {".git", "venv", "instance", "__pycache__", ".vercel", "node_modules"}
EXCLUDE_FILES = {".gitignore", ".cron_token", "server.log", "deploy_render.py",
                 "deploy_pythonanywhere.py", "render.yaml", "Procfile", "vercel.json", "api/index.py"}

S = requests.Session()
S.headers["Authorization"] = f"Token {TOKEN}"


def api(method, path, **kw):
    r = S.request(method, f"https://www.pythonanywhere.com/api/v0/user/{USER}/{path}",
                  timeout=180, **kw)
    return r.status_code, r.text[:300]


def project_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn in EXCLUDE_FILES:
                continue
            full = os.path.join(dirpath, fn)
            out.append((os.path.relpath(full, root).replace("\\", "/"), full))
    return out


WSGI = f'''# -*- coding: utf-8 -*-
import os, sys, subprocess

os.environ["GC_ACCOUNT"] = "https://syrover.com"
os.environ["GC_API_KEY"] = "{GC_KEY}"
os.environ["SECRET_KEY"] = "{SECRET}"
os.environ["SMM_DEMO"] = "1"
os.environ["SMM_SCHEDULER"] = "1"
os.environ["INGEST_TOKEN"] = "f86fa68fa6350576b50e7767551f849eabb19abb3a26d625"
os.environ["AI_API_KEY"] = os.environ.get("AI_API_KEY", "")
os.environ["AI_BASE_URL"] = "https://generativelanguage.googleapis.com/v1beta/openai"
os.environ["AI_MODEL"] = "gemini-3.8-flash"

project = "{PROJ}"
if project not in sys.path:
    sys.path.insert(0, project)

marker = "{HOME}/.smm_deps_installed"
if not os.path.exists(marker):
    subprocess.check_call(["pip3.10", "install", "--user", "-q",
                           "-r", project + "/requirements.txt"])
    open(marker, "w").write("ok")

from app import app as application
'''


def main():
    files = project_files(os.path.dirname(os.path.abspath(__file__)))
    print(f"файлов к загрузке: {len(files)}")

    # 1) web app (Manual configuration, python310)
    st, msg = api("GET", "webapps/")
    existing = any(w.get("domain") == DOMAIN for w in json.loads(S.get(
        f"https://www.pythonanywhere.com/api/v0/user/{USER}/webapps/").text))
    if not existing:
        r = S.post(f"https://www.pythonanywhere.com/api/v0/user/{USER}/webapps/",
                   data={"domain_name": DOMAIN, "python_version": "python310"}, timeout=180)
        print("webapp create:", r.status_code, r.text[:200])
    else:
        print("webapp уже существует")

    # 2) файлы проекта (multipart POST, поле content)
    for rel, full in files:
        with open(full, "rb") as f:
            r = S.post(f"https://www.pythonanywhere.com/api/v0/user/{USER}/files/path{PROJ}/{rel}",
                       files={"content": (os.path.basename(full), f)}, timeout=180)
        if r.status_code not in (200, 201):
            print(f"  !! {rel}: {r.status_code} {r.text[:150]}")
    print("файлы загружены")

    # 3) WSGI-файл
    r = S.post(f"https://www.pythonanywhere.com/api/v0/user/{USER}/files/path"
               f"/var/www/{USER}_pythonanywhere_com_wsgi.py",
               files={"content": ("wsgi.py", WSGI.encode())}, timeout=60)
    print("wsgi:", r.status_code, r.text[:150])

    # 4) статика
    try:
        lst = S.get(f"https://www.pythonanywhere.com/api/v0/user/{USER}/webapps/{DOMAIN}/static_files/").json()
        have_static = isinstance(lst, list) and any(isinstance(s, dict) and s.get("url") == "/static/" for s in lst)
    except Exception:
        have_static = False
    if not have_static:
        st, msg = api("POST", f"webapps/{DOMAIN}/static_files/",
                      json={"url": "/static/", "path": f"{PROJ}/static"})
        print("static:", st, msg)
    else:
        print("static: уже есть")

    # 5) reload
    st, msg = api("POST", f"webapps/{DOMAIN}/reload/")
    print("reload:", st, msg)
    print(f"\nURL: https://{DOMAIN} (первый запуск ставит пакеты ~1-2 мин)")


if __name__ == "__main__":
    main()
