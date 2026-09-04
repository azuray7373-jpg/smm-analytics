# -*- coding: utf-8 -*-
"""Полный тест всего приложения."""
import sys, io, json, os, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from app import app
from db import db, set_setting, Channel, MetricSnapshot, Registration, GcOrder, GcPayment, \
    Comment, Report, Goal, Spend, Task, ContentItem, ContentStat, Hypothesis, Notification
import calc, connectors, livedune, comments as cm, intel, assistant, utm as utm_mod
from datetime import date, datetime, timedelta

print("═" * 50)
print("  ПОЛНЫЙ ТЕСТ ВСЕГО ПРИЛОЖЕНИЯ")
print("═" * 50)
passed = failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  ✅ {name}")
    else:
        failed += 1; print(f"  ❌ {name} {detail}")

# === SETUP ===
with app.app_context():
    set_setting("livedune_token", "aa6964995af11b08.12027558")
    set_setting("ingest_token", "test123")
    db.session.commit()
    livedune.ingest_packet(livedune.collect_packet(days=7))
    connectors.run_daily_collection()
    for i in range(100):
        d = date(2026, 9, 1 + (i % 3))
        db.session.add(GcOrder(id=9000+i, created_at=datetime(2026, 9, 1+i%3, 10+i%12), date=d,
                               user_id=i%30, email=f"u{i%30}@x.ru", amount=5000,
                               status="payed", utm_source=["insta-alexey","telegram","vkontakte"][i%3],
                               utm_medium=["post","story","bot"][i%3], utm_campaign="openlesson"))
    for i in range(50):
        db.session.add(GcPayment(id=8000+i, created_at=datetime(2026, 9, 2, 12), date=date(2026,9,2),
                                  user_id=i%30, amount=5000, status="accepted", deal_id=9000+i))
    csv_data = "date,channel_id,author,text,likes\n"
    for i, txt in enumerate(["Как зарегистрироваться?", "Спасибо!", "Не получается закваску",
                              "Сделайте про козий сыр", "Дорого"]):
        csv_data += f"2026-09-0{i+1},5,автор{i},\"{txt}\",{i*3}\n"
    cm.import_csv(type("F", (), {"read": lambda s: csv_data.encode()})())
    db.session.add(Goal(metric="reach", target=100000, start=date(2026,9,1), end=date(2026,9,7)))
    db.session.add(Spend(channel_id=1, date=date(2026,9,2), amount=5000))
    db.session.add(Task(week_start=date(2026,8,30), text="Тест задача"))
    db.session.commit()

c = app.test_client()
with c.session_transaction() as s: s['authed'] = True
os.environ['INGEST_TOKEN'] = 'test123'

# === 1. АВТОРИЗАЦИЯ ===
print("\n─── Авторизация ───")
c2 = app.test_client()
check("редирект без входа", c2.get('/').status_code == 302)
check("страница входа", c2.get('/login').status_code == 200)
check("неверный пароль → 401", c2.post('/login', data={'password':'x'}).status_code == 401)
check("верный пароль → 302", c2.post('/login', data={'password':'testpass'}).status_code == 302)

# === 2. МАРШРУТЫ ===
print("\n─── Маршруты ───")
for u in ['/', '/channels', '/content', '/registrations', '/getcourse', '/comments',
          '/ai', '/reports', '/guide', '/calendar', '/compare', '/goals', '/spends',
          '/tasks', '/competitors', '/hypotheses', '/utm', '/settings', '/manual',
          '/import', '/notifications', '/assistant', '/intel']:
    r = c.get(u)
    check(f"GET {u}", r.status_code == 200, f"got {r.status_code}")

# === 3. KPI ===
print("\n─── KPI карточки ───")
t = c.get('/').data.decode('utf-8','replace')
check("kpi-card", 'kpi-card' in t)
check("kpi-detail (раскрытие)", 'kpi-detail' in t)
check("breakdown строки", t.count('kpi-breakdown-row') > 20, f"{t.count('kpi-breakdown-row')}")

# === 4. КАНАЛЫ ===
print("\n─── Каналы ───")
t = c.get('/channels').data.decode('utf-8','replace')
check("подписались зелёный", 'var(--success)' in t)
check("отписались красный", 'var(--danger)' in t)
check("средний охват", 'Средний охват' in t)
r = c.get('/channel/1')
check("детальная страница канала", r.status_code == 200)

# === 5. UTM ===
print("\n─── Сборные метки ───")
t = c.get('/registrations').data.decode('utf-8','replace')
check("сборные метки", 'Сборные метки' in t)
check("наши метки присутствуют", 'Наши метки' in t)

# === 6. GETCOURSE ===
print("\n─── Продажи ───")
t = c.get('/getcourse').data.decode('utf-8','replace')
check("воронка", 'fstage' in t)
check("когорты", 'когорт' in t.lower() or 'Удержание' in t)
check("заказы", 'Последние заказы' in t)

# === 7. AI ===
print("\n─── AI ───")
r = c.post('/intel/predict', data={'format':'reels','title':'т','text':'т','duration':'30'})
check("Predictor", r.status_code == 200 and r.get_json().get('predicted_reach') is not None)
r = c.post('/assistant/ask', data={'question':'Какой канал хуже?'})
check("AI ассистент", r.status_code == 200 and r.get_json().get('answer'))

# === 8. РАСЧЁТЫ ===
print("\n─── Математика ───")
with app.app_context():
    p = calc.period_report(date(2026,9,1), date(2026,9,7))
    check("period_report", p["agg"].get("reach") is not None)
    check("ERR", p["ind"].get("ERR") is not None)
    check("CV", p["ind"].get("CV_reach") is not None)
    check("weekly_series", len(calc.weekly_series(4)) == 4)
    check("month_forecast", calc.month_forecast() is not None)
    check("growth_points", isinstance(calc.growth_points(date(2026,9,1), date(2026,9,7)), list))

# === 9. СТАТУСЫ ===
print("\n─── Статусы данных ───")
with app.app_context():
    rows = db.session.query(MetricSnapshot.channel_id, MetricSnapshot.metric,
                            MetricSnapshot.status, db.func.max(MetricSnapshot.fetched_at)).filter(
        MetricSnapshot.date == date.today()).group_by(MetricSnapshot.channel_id, MetricSnapshot.metric).all()
    missing = sum(1 for r in rows if r[2] == "MISSING")
    check(f"MISSING минимально ({missing})", missing <= 20)

# === 10. КОММЕНТАРИИ ===
print("\n─── Комментарии ───")
with app.app_context():
    dig = cm.digest(date(2026,9,1), date(2026,9,7))
    check("классифицированы", dig["total"] > 0)
    check("вопросы", len(dig["questions"]) > 0)

# === 11. ИНТЕЛЛЕКТ ===
print("\n─── Интеллект ───")
with app.app_context():
    scored = intel.score_all_content(30)
    check("performance_score", len(scored) > 0)
    check("баллы 0-100", all(0 <= s["score"] <= 100 for s in scored))
    check("trend_radar", isinstance(intel.trend_radar(8), list))
    brief = intel.team_brief()
    check("team_brief", "wins" in brief and "actions" in brief)

# === 12. ЦЕЛИ/ГИПОТЕЗЫ ===
print("\n─── Цели и гипотезы ───")
check("цель", 'KPI-цели' in c.get('/goals').data.decode('utf-8','replace'))
r = c.post('/hypotheses', data={'text':'т','metric':'reach','expectation':'+5%',
                                'start':'2026-09-01','end':'2026-09-07'})
check("гипотеза создана (302)", r.status_code == 302)

# === 13. РАСХОДЫ ===
print("\n─── Расходы ───")
t = c.get('/spends').data.decode('utf-8','replace')
check("экран", 'Расходы' in t)
check("ROI", 'ROI' in t)

# === 14. ЗАДАЧИ ===
print("\n─── Задачи ───")
check("отображаются", 'Тест задача' in c.get('/tasks').data.decode('utf-8','replace'))

# === 15. КОНКУРЕНТЫ ===
print("\n─── Конкуренты ───")
r = c.post('/competitors/add', data={'name':'ТестК','platform':'instagram'}, follow_redirects=True)
check("добавление", 'ТестК' in r.data.decode('utf-8','replace'))

# === 16. API ===
print("\n─── API ───")
check("pending", c.get('/api/notifications/pending?token=test123').status_code == 200)
check("ld_map", c.get('/api/ld_map?token=test123').status_code == 200)
check("webhook auth=403", c.get('/api/gc-webhook?type=user').status_code == 403)
r = c.get('/api/gc-webhook?type=user&token=test123&gc_id=999&utm_source=telegram')
check("webhook", r.status_code == 200)

# === 17. ЭКСПОРТ ===
print("\n─── Экспорт CSV ───")
check("content", c.get('/content?export=csv').status_code == 200)
check("channels", c.get('/channels?export=csv').status_code == 200)
check("registrations", c.get('/registrations?export=csv').status_code == 200)

# === 18. СРАВНЕНИЕ ===
print("\n─── Прочее ───")
check("compare", 'Сравнение' in c.get('/compare').data.decode('utf-8','replace'))
check("calendar", c.get('/calendar').status_code == 200)

# === 19. НЕДЕЛЯ ВС-СБ ===
with app.app_context():
    s_, e_ = calc.week_bounds(date(2026, 9, 3))
    check("неделя начинается вс", s_.weekday() == 6, f"start {s_} ({s_.strftime('%a')})")
    check("неделя кончается сб", e_.weekday() == 5)

# === ИТОГ ===
print("\n" + "═" * 50)
print(f"  ИТОГО: {passed} ✅ | {failed} ❌")
print(f"  ПРОХОД: {passed/(passed+failed)*100:.0f}%")
print("═" * 50)
