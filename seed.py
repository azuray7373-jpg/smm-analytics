# -*- coding: utf-8 -*-
"""Каналы из ТЗ + демо-данные за 70 дней, чтобы дашборд и отчёты работали сразу.
Флаг SMM_DEMO=0 отключает генерацию демо-статистик (каналы создаются всегда)."""
import json, os, random
from datetime import date, datetime, timedelta
from db import db, Channel, MetricSnapshot, ContentItem, ContentStat, Registration, RunLog

CHANNELS = [
    ("instagram", "Алексей старый", "https://www.instagram.com/alexey_syroer"),
    ("instagram", "Дина", "https://www.instagram.com/syrover_school"),
    ("instagram", "ВЕНЕРА", "https://www.instagram.com/venera_vaxidova"),
    ("instagram", "Алексей новый", "https://www.instagram.com/alexeysyroer"),
    ("youtube", "YouTube", "https://www.youtube.com/@AlexeySyrover"),
    ("max", "MAX основной", "https://max.ru/syrover"),
    ("max", "MAX 2", "https://max.ru/id505601899850_biz"),
    ("telegram", "Telegram", "https://t.me/alexeysyrover"),
    ("tiktok", "TikTok", "https://www.tiktok.com/@alexey_syrover"),
    ("vk", "VK", "https://vk.com/syrover"),
    ("dzen", "Дзен", "https://dzen.ru/alexey_syrover"),
]

BASE_FOLLOWERS = {"instagram": [180000, 320000, 90000, 12000], "youtube": 240000,
                  "max": 15000, "telegram": 60000, "tiktok": 400000, "vk": 20000, "dzen": 35000}

RUBRICS = ["еда/продукты", "тренировки", "результаты", "мотивация", "разборы"]
FORMATS = {"instagram": ["reels", "post", "story"], "youtube": ["video", "shorts"],
           "max": ["post"], "telegram": ["post"], "tiktok": ["video"],
           "vk": ["post", "clip"], "dzen": ["article", "video"]}
CTAS = ["регистрируйся на вебинар", "ссылка в профиле", "напиши в комментариях", "нет CTA"]


def seed(db_uri=None):
    if Channel.query.count() == 0:
        for p, n, u in CHANNELS:
            db.session.add(Channel(platform=p, name=n, url=u))
        db.session.commit()

    days_total = int(os.environ.get("SMM_DEMO_DAYS", "70"))
    if os.environ.get("SMM_DEMO", "1") != "1" or MetricSnapshot.query.count() > 0:
        db.session.commit()
        return

    random.seed(42)
    run = RunLog(kind="demo_seed", status="OK")
    db.session.add(run)
    db.session.commit()
    chans = Channel.query.all()
    today = date.today()
    fol = {}
    for ch in chans:
        if ch.platform == "instagram":
            idx = [i for i, c in enumerate(CHANNELS) if c[1] == ch.name][0]
            base = BASE_FOLLOWERS["instagram"][idx % 4]
        else:
            base = BASE_FOLLOWERS.get(ch.platform, 10000)
        fol[ch.id] = base

    for day_n in range(days_total, -1, -1):
        d = today - timedelta(days=day_n)
        for ch in chans:
            trend = 1 + (70 - day_n) * 0.0012 * random.uniform(0.5, 1.5)
            mult = 1.0
            if ch.platform == "instagram" and ch.name == "Дина" and day_n < 10:
                mult = 1.4  # «два ролика дали рост»
            if ch.platform == "max" and day_n < 7:
                mult = 0.6  # «проблемы с уведомлениями»
            reach = int(base * 0.02 * trend * mult * random.uniform(0.7, 1.4))
            inter = int(reach * random.uniform(0.03, 0.07))
            subs = int(reach * random.uniform(0.002, 0.006))
            unsubs = int(subs * random.uniform(0.2, 0.5))
            fol[ch.id] += subs - unsubs
            vals = {
                "followers": fol[ch.id], "reach": reach, "views": int(reach * random.uniform(1.1, 1.8)),
                "impressions": int(reach * 1.05), "opens": int(reach * 0.8), "reads": int(reach * 0.45),
                "likes": int(inter * 0.6), "comments": int(inter * 0.1), "saves": int(inter * 0.15),
                "shares": int(inter * 0.08), "reactions": int(inter * 0.07),
                "subscribed": subs, "unsubscribed": unsubs,
            }
            for m, v in vals.items():
                db.session.add(MetricSnapshot(channel_id=ch.id, date=d, metric=m, value=v,
                                              status="OK", source="demo", run_id=run.id))
            # публикации
            if random.random() < 0.45:
                fmt = random.choice(FORMATS.get(ch.platform, ["post"]))
                rub = random.choice(RUBRICS)
                ci = ContentItem(
                    channel_id=ch.id, external_id=f"demo-{ch.id}-{d}-{random.randint(0,999)}",
                    link=f"https://example.com/{ch.slug}/{day_n}",
                    published_at=datetime.combine(d, datetime.min.time()) + timedelta(hours=random.randint(8, 21)),
                    format=fmt, title=f"{rub}: {random.choice(['как начать', 'ошибки', 'мой опыт', 'разбор', 'лайфхаки'])}",
                    text=f"Автоматический пост рубрики {rub}. Регистрируйся на вебинар, ссылка в профиле.",
                    duration_sec=random.choice([None, 20, 30, 45, 90, 300]),
                    cta=random.choice(CTAS))
                db.session.add(ci)
                db.session.flush()
                v = int(reach * random.uniform(0.5, 2.2) * (1.6 if rub == "еда/продукты" else 1.0))
                db.session.add(ContentStat(
                    content_id=ci.id, date=d, views=v, reach=int(v * 0.9),
                    likes=int(v * 0.04), comments=int(v * 0.005), saves=int(v * 0.01),
                    shares=int(v * 0.006), reactions=int(v * 0.004),
                    subs=int(v * 0.003), registrations=int(v * random.uniform(0.0005, 0.004))))
        # регистрации с UTM
        for src, w in (("instagram", 3), ("telegram", 1.2), ("youtube", 1), ("tiktok", 1.5), ("vk", 0.4), ("dzen", 0.3), ("max", 0.5)):
            if random.random() < 0.8:
                db.session.add(Registration(date=d, utm_source=src, utm_medium=random.choice(["post", "reels", "story", "cpc", "email"]),
                                            utm_campaign=random.choice(["webinar_sept", "challenge", "evergreen"]),
                                            landing=random.choice(["/reg", "/trial", "/webinar"]), count=random.randint(5, 90)))
    db.session.commit()
    # автоклассификация контента демо
    for ci in ContentItem.query.filter(ContentItem.ai_tags.is_(None)).limit(200):
        import ai_analyst
        ci.ai_tags = json.dumps(ai_analyst.classify_content_text(ci.text, ci.format), ensure_ascii=False)
    db.session.commit()
