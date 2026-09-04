# -*- coding: utf-8 -*-
"""Наши UTM-метки (справочник клиента) и разрезы по ним.

utm_campaign (3 основные воронки):
  openlesson — БК (бесплатный курс), otvety — ВИО, autoweb — авто.
utm_medium: post, canal-anons, bot, story, taplink, gayd.
utm_source — 13 официальных источников (скриншот клиента) + маппинг на платформы.
Всё, что не входит в справочник, помечается «прочее» и не теряется.
"""
from datetime import date
from collections import defaultdict
from sqlalchemy import func
from db import db, Registration, GcOrder, GcPayment
from calc import ttl_cache as _ttl

CAMPAIGN_TYPES = {
    "openlesson": "БК (бесплатный курс)",
    "otvety": "ВИО",
    "autoweb": "Автовебинар",
}
MEDIUMS = ["post", "canal-anons", "bot", "botVL2", "story", "taplink", "gayd", "shapka"]
SOURCES = ["insta-alexey", "insta-alexeynew", "insta-dina", "insta-venera",
           "max-venera", "telegram", "max", "vkontakte", "youtube", "ticktock",
           "dzen", "vk_maya", "telegram_maya"]
SOURCE_TO_PLATFORM = {
    "insta-alexey": "instagram", "insta-alexeynew": "instagram", "insta-dina": "instagram",
    "insta-venera": "instagram", "max-venera": "max", "max": "max", "telegram": "telegram",
    "telegram_maya": "telegram", "vkontakte": "vk", "vk_maya": "vk", "youtube": "youtube",
    "ticktock": "tiktok", "dzen": "dzen",
}


def campaign_type(v):
    v = (v or "").strip().lower()
    for k, t in CAMPAIGN_TYPES.items():
        if v == k or v.startswith(k):
            return t
    return None


def known_source(v):
    v = (v or "").strip().lower()
    return v if v in SOURCES else None


def known_medium(v):
    v = (v or "").strip().lower()
    return v if v in MEDIUMS else None


def _group_registrations(start, end, sdt=None, edt=None):
    q = db.session.query(Registration.utm_source, Registration.utm_medium,
                         Registration.utm_campaign, func.sum(Registration.count)).filter(
        Registration.date >= start, Registration.date <= end,
        Registration.status == "OK",
        ~Registration.utm_source.like("demo_%"))
    if sdt and edt and Registration.query.filter(Registration.created_at.isnot(None)).count():
        q = q.filter(Registration.created_at >= sdt, Registration.created_at < edt)
    return q.group_by(Registration.utm_source, Registration.utm_medium,
                      Registration.utm_campaign).all()


def _group_orders(start, end):
    return db.session.query(
        GcOrder.utm_source, GcOrder.utm_medium, GcOrder.utm_campaign,
        func.count(GcOrder.id), func.coalesce(func.sum(GcOrder.amount), 0)).filter(
        GcOrder.date >= start, GcOrder.date <= end).group_by(
        GcOrder.utm_source, GcOrder.utm_medium, GcOrder.utm_campaign).all()


def _group_payments(start, end):
    return db.session.query(
        GcPayment.date, func.count(GcPayment.id), func.coalesce(func.sum(GcPayment.amount), 0)).filter(
        GcPayment.date >= start, GcPayment.date <= end,
        GcPayment.status == "accepted").group_by(GcPayment.date).all()


@_ttl(120)
def breakdown(start: date, end: date, sdt=None, edt=None):
    """Разрезы по нашим меткам + проверка наличия всех меток в данных.
    sdt/edt — точные границы времени (для отчётных периодов с часами)."""
    regs = _group_registrations(start, end, sdt, edt)
    orders = _group_orders(start, end)

    AD_HINTS = ("yandex", "ya_", "direct", "vkads", "vk_ads", "вк реклам", "fb_", "facebook",
                "mytarget", "sms", "сайт", "site", "partner", "lead_sv", "google", "директ")
    def classify(s, m, c):
        """(категория, источник-метка). Наша = есть наша campaign ИЛИ наш medium.
        Остальное: рекламные кабинеты отдельно, пустой source отдельно."""
        src = (s or "").strip().lower()
        if campaign_type(c) or known_medium(m):
            if not src:
                return "наши", "без метки (проверить ссылку)"
            return "наши", known_source(s) or src
        if any(h in src for h in AD_HINTS):
            return "реклама", src or "реклама"
        if not src:
            return "без метки", "без метки"
        return "реклама", src   # чужой source без наших меток = не соцсети

    by_funnel = defaultdict(lambda: {"regs": 0, "orders": 0, "sum": 0})
    by_medium = defaultdict(lambda: {"regs": 0, "orders": 0})
    by_source = defaultdict(lambda: {"regs": 0, "orders": 0})
    by_combo = defaultdict(lambda: {"regs": 0, "orders": 0})
    for s, m, c, n in regs:
        cat, srckey = classify(s, m, c)
        if cat == "наши":
            ft = campaign_type(c) or "без campaign"
            by_funnel[ft]["regs"] += n or 0
            mk = known_medium(m) or ((m or "").strip().lower() or "без medium")
            by_medium[mk]["regs"] += n or 0
            by_source[srckey]["regs"] += n or 0
            key = (srckey, mk, campaign_type(c) or "")
            by_combo[key]["regs"] += n or 0
        elif cat == "реклама":
            by_funnel["Реклама (не соцсети)"]["regs"] += n or 0
            by_source[srckey]["regs"] += n or 0
        else:
            by_funnel["Без метки"]["regs"] += n or 0
            by_source["без метки (проверить ссылку)"]["regs"] += n or 0
    for s, m, c, n, total in orders:
        cat, srckey = classify(s, m, c)
        if cat == "наши":
            ft = campaign_type(c) or "без campaign"
            by_funnel[ft]["orders"] += n
            by_funnel[ft]["sum"] += total or 0
            mk = known_medium(m) or ((m or "").strip().lower() or "без medium")
            by_medium[mk]["orders"] += n
            by_source[srckey]["orders"] += n
            key = (srckey, mk, campaign_type(c) or "")
            by_combo[key]["orders"] += n
        elif cat == "реклама":
            by_funnel["Реклама (не соцсети)"]["orders"] += n
        else:
            by_funnel["Без метки"]["orders"] += n

    # проверки наличия наших меток в данных
    seen_c = {c for _, _, c, _ in regs} | {c for _, _, c, _, _ in orders}
    seen_m = {m for _, m, _, _ in regs} | {m for _, m, _, _, _ in orders}
    seen_s = {s for s, _, _, _ in regs} | {s for s, _, _, _, _ in orders}
    norm = lambda xs: {str(x).lower().strip() for x in xs if x}
    nc, nm, ns = norm(seen_c), norm(seen_m), norm(seen_s)
    missing = {"campaigns": [c for c in CAMPAIGN_TYPES if c not in nc],
               "mediums": [m for m in MEDIUMS if m not in nm],
               "sources": [s for s in SOURCES if s not in ns]}
    combos = sorted(by_combo.items(), key=lambda x: -x[1]["regs"])
    return {"by_funnel": dict(by_funnel), "by_medium": dict(by_medium),
            "by_source": dict(by_source), "by_combo": combos, "missing": missing}


@_ttl(300)
def payments_by_platform(start, end):
    """Принятые оплаты по платформам (через utm_source заказов)."""
    from db import GcPayment, GcOrder
    from sqlalchemy import func as _f
    rows = (db.session.query(GcOrder.utm_source,
                             _f.coalesce(_f.sum(GcPayment.amount), 0))
            .join(GcPayment, GcPayment.deal_id == GcOrder.id)
            .filter(GcPayment.date >= start, GcPayment.date <= end,
                    GcPayment.status == "accepted")
            .group_by(GcOrder.utm_source).all())
    out = {}
    for src, amount in rows:
        plat = SOURCE_TO_PLATFORM.get((src or "").lower())
        key = plat or "без метки"
        out[key] = out.get(key, 0) + (amount or 0)
    return out


@_ttl(600)
def retention_cohorts(months=6):
    """Когорты новичков: % совершивших повторный заказ в течение 30 дней."""
    from db import GcOrder
    from sqlalchemy import func as _f
    from collections import defaultdict
    orders_by_user = defaultdict(list)
    rows = (db.session.query(GcOrder.user_id, GcOrder.email, GcOrder.created_at)
            .filter(GcOrder.created_at.isnot(None))
            .order_by(GcOrder.created_at).all())
    for uid, email, created in rows:
        key = uid or (email or "").lower()
        if key:
            orders_by_user[key].append(created)
    cohorts = {}
    for key, times in orders_by_user.items():
        times = sorted(times)
        m = (times[0].year, times[0].month)
        repeat30 = any(0 < (t - times[0]).total_seconds() <= 30 * 86400 for t in times[1:])
        c = cohorts.setdefault(m, {"new": 0, "repeated": 0})
        c["new"] += 1
        c["repeated"] += 1 if repeat30 else 0
    out = []
    from datetime import date as _date
    today = _date.today()
    y, mth = today.year, today.month
    for _ in range(months):
        label = f"{mth:02d}.{y}"
        c = cohorts.get((y, mth))
        out.append({"month": label, "new": c["new"] if c else 0,
                    "repeated": c["repeated"] if c else 0,
                    "pct": (c["repeated"] / c["new"] * 100) if c and c["new"] else None})
        mth -= 1
        if mth == 0:
            mth, y = 12, y - 1
    return list(reversed(out))
