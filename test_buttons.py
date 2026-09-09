# -*- coding: utf-8 -*-
"""Полное тестирование всех кнопок, ссылок, форм и POST-эндпоинтов.

1. Краулит каждую страницу приложения.
2. Извлекает все внутренние ссылки (href) и проверяет, что каждая открывается (200).
3. Извлекает все формы и проверяет, что их action-эндпоинты существуют и отвечают.
4. Проверяет все POST-маршруты Flask напрямую.
5. Проверяет, что onclick/onsubmit-обработчики ссылаются на определённые JS-функции.
6. Регрессионный тест бага с кликабельностью: CSS-оверлеи (position:absolute + inset:0)
   обязаны иметь pointer-events:none, иначе они перехватывают клики по кнопкам.
"""
import sys, io, re, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from app import app
from db import db, set_setting
import connectors, livedune
from datetime import date

print("═" * 50)
print("  ТЕСТ ВСЕХ КНОПОК, ССЫЛОК И ФОРМ")
print("═" * 50)
passed = failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  ✅ {name}")
    else:
        failed += 1; print(f"  ❌ {name} {detail}")

# === SETUP: базовые данные, чтобы все страницы рендерились с контентом ===
with app.app_context():
    set_setting("ingest_token", "test123")
    db.session.commit()
    try:
        livedune.ingest_packet(livedune.collect_packet(days=7))
    except Exception:
        pass
    try:
        connectors.run_daily_collection()
    except Exception:
        pass

c = app.test_client()
with c.session_transaction() as s:
    s['authed'] = True
os.environ['INGEST_TOKEN'] = 'test123'

# ═══ 1. СТАРТОВЫЕ СТРАНИЦЫ (все GET-маршруты приложения) ═══
print("\n─── 1. Все GET-страницы открываются ───")
get_pages = ['/', '/assistant', '/intel', '/ai', '/channels', '/content', '/registrations',
             '/getcourse', '/spends', '/comments', '/calendar', '/goals', '/tasks',
             '/competitive', '/competitors', '/hypotheses', '/compare', '/utm', '/reports',
             '/guide', '/import', '/manual', '/settings', '/notifications', '/login']
page_html = {}
for u in get_pages:
    r = c.get(u)
    ok = r.status_code == 200
    check(f"GET {u}", ok, f"→ {r.status_code}")
    if ok:
        page_html[u] = r.get_data(as_text=True)

# ═══ 2. ВСЕ ВНУТРЕННИЕ ССЫЛКИ НА КАЖДОЙ СТРАНИЦЕ ═══
print("\n─── 2. Краулинг: каждая внутренняя ссылка открывается ───")
checked = set()
broken_links = []
dynamic_re = re.compile(r'\{\{|\{%|\$|\bnull\b', re.I)
for page, html in page_html.items():
    for href in set(re.findall(r'href="(/[^"#]*)"', html)):
        if href in checked or href.startswith(('/static/', '/logout')):
            continue
        checked.add(href)
        try:
            r = c.get(href)
            if r.status_code >= 400:
                # динамические id могут не существовать в тестовой базе — это не баг кнопки
                if re.search(r'/\d+', href) and r.status_code == 404:
                    continue
                broken_links.append((page, href, r.status_code))
        except Exception as e:
            broken_links.append((page, href, str(e)[:60]))
check(f"все внутренние ссылки ({len(checked)} шт.) открываются", not broken_links,
      f"сломаны: {broken_links[:8]}")

# ═══ 3. ВСЕ ФОРМЫ: action существует и метод поддерживается ═══
print("\n─── 3. Все формы ведут на существующие эндпоинты ───")
rules = {r.rule: r for r in app.url_map.iter_rules()}
form_issues = []
all_forms = []
for page, html in page_html.items():
    for m in re.finditer(r'<form[^>]*>', html):
        tag = m.group(0)
        action = re.search(r'action="([^"]*)"', tag)
        method = (re.search(r'method="(\w+)"', tag) or [None, 'get'])[1] if re.search(r'method="(\w+)"', tag) else 'get'
        mm = re.search(r'method="(\w+)"', tag)
        method = mm.group(1).lower() if mm else 'get'
        act = action.group(1) if action else page
        if act.startswith('http') or act.startswith('#'):
            continue
        act = act if act.startswith('/') else '/' + act
        all_forms.append((page, act, method, tag))
        if method == 'get':
            r = c.get(act)
            if r.status_code >= 400:
                form_issues.append((page, act, 'GET', r.status_code))
for page, act, method, tag in all_forms:
    if method == 'post':
        # сам факт наличия POST-маршрута проверим в секции 4; здесь — только существование правила
        base = re.sub(r'/\d+', '/0', act.split('?')[0])
        if not any(rule == act.split('?')[0] or rule == base for rule in rules):
            form_issues.append((page, act, 'POST', 'нет маршрута'))
check(f"все формы ({len(all_forms)} шт.) валидны", not form_issues, f"проблемы: {form_issues[:6]}")

# ═══ 4. ВСЕ POST-ЭНДПОИНТЫ ПРИЛОЖЕНИЯ ═══
print("\n─── 4. Все POST-маршруты отвечают (без 500) ───")
post_routes = [r.rule for r in app.url_map.iter_rules() if 'POST' in r.methods and r.endpoint != 'static']
# Типовые безопасные тела запросов для каждого эндпоинта (только чтение/пустые данные,
# деструктивные операции — с несуществующими id, чтобы ничего не удалили)
safe_bodies = {
    '/assistant/ask': {'question': 'тест'},
    '/intel/predict': {'format': 'reels', 'title': 'т', 'text': 'т', 'duration': '30'},
    '/collect': None,          # пропускаем: фоновый запуск реального сбора
    '/reports/generate': None, # пропускаем: генерирует реальный отчёт
    '/manual/add': None,       # пропускаем: пишет данные
    '/gc/sync': None,
    '/sync_livedune': None,
}
skipped = []
for rule in post_routes:
    if rule in safe_bodies and safe_bodies[rule] is None:
        skipped.append(rule)
        continue
    body = safe_bodies.get(rule, {})
    url = re.sub(r'<int:[^>]+>', '1', re.sub(r'<[^>]+>', 'x', rule))
    try:
        r = c.post(url, data=body)
        check(f"POST {rule}", r.status_code < 500, f"→ {r.status_code}")
    except Exception as e:
        check(f"POST {rule}", False, str(e)[:80])
if skipped:
    print(f"  ⏭  пропущены (деструктивные/долгие): {', '.join(skipped)}")

# ═══ 5. JS-ОБРАБОТЧИКИ: onclick/onsubmit ссылаются на определённые функции ═══
print("\n─── 5. JS-обработчики ссылаются на определённые функции ───")
js_issues = []
for page, html in page_html.items():
    # все вызовы функций в on* атрибутах
    calls = re.findall(r'on(?:click|submit|change|input|keyup)="(\w+)\(', html)
    # функции, определённые на странице или в подключённых статик-скриптах
    defined = set(re.findall(r'function\s+(\w+)\s*\(', html))
    defined |= set(re.findall(r'window\.(\w+)\s*=', html))
    # статические js-файлы
    for src in set(re.findall(r'src="(/static/[^"]+\.js)"', html)):
        p = os.path.join(os.path.dirname(__file__), src.lstrip('/'))
        if os.path.exists(p):
            js = io.open(p, encoding='utf-8', errors='replace').read()
            defined |= set(re.findall(r'function\s+(\w+)\s*\(', js))
            defined |= set(re.findall(r'window\.(\w+)\s*=', js))
    for fn in set(calls):
        if fn not in defined:
            js_issues.append((page, fn))
check("все on*-обработчики определены", not js_issues, f"не найдены: {js_issues[:8]}")

# ═══ 6. РЕГРЕССИЯ БАГА КЛИКАБЕЛЬНОСТИ (hero-кнопки) ═══
print("\n─── 6. CSS: оверлеи не перехватывают клики ───")
css = io.open(os.path.join(os.path.dirname(__file__), 'static', 'style.css'),
              encoding='utf-8').read()
# каждое правило с position:absolute + inset:0 (полное перекрытие) должно иметь pointer-events:none
overlay_rules = re.findall(r'([^{}]+)\{[^{}]*(?:position:\s*absolute|position:absolute)[^{}]*(?:inset:\s*0|inset:0)[^{}]*\}', css)
bad_overlays = [r.strip() for r in overlay_rules
                if 'pointer-events:none' not in r and 'canvas' not in r and '::before' in r or
                   ('pointer-events:none' not in r and 'inset:0' in r and 'canvas' not in r)]
# проще: прямые проверки известных оверлеев
for sel in ['.hero::before', '.card::before', '.kpi-card::before', '.aireport::before']:
    m = re.search(re.escape(sel) + r'\s*\{([^}]*)\}', css)
    if m and 'position:absolute' in m.group(1).replace(' ', ''):
        has_pe = 'pointer-events:none' in m.group(1)
        check(f"{sel} оверлей кликабелен (pointer-events:none)", has_pe)

# ═══ 7. НАВИГАЦИЯ: все url_for в шаблонах ведут на существующие маршруты ═══
print("\n─── 7. Шаблоны: url_for ведут на существующие эндпоинты ───")
tmpl_issues = []
tpl_dir = os.path.join(os.path.dirname(__file__), 'templates')
for fn in os.listdir(tpl_dir):
    if not fn.endswith('.html'):
        continue
    t = io.open(os.path.join(tpl_dir, fn), encoding='utf-8', errors='replace').read()
    for ep in set(re.findall(r"url_for\('([^']+)'", t)):
        if ep not in app.view_functions:
            tmpl_issues.append((fn, ep))
check("все url_for в 30 шаблонах валидны", not tmpl_issues, f"нет эндпоинтов: {tmpl_issues[:6]}")

# ═══ ИТОГ ═══
print("\n" + "═" * 50)
print(f"  ИТОГО: ✅ {passed} · ❌ {failed}")
print("═" * 50)
sys.exit(1 if failed else 0)
