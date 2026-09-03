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

CAMPAIGN_TYPES = {
    "openlesson": "БК (бесплатный курс)",
    "otvety": "ВИО",
    "autoweb": "Автовебинар",
}
MEDIUMS = ["post", "canal-anons", "bot", "story", "taplink", "gayd"]
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


def _group_registrations(start, end):
    rows = db.session.query(Registration.utm_source, Registration.utm_medium,
                            Registration.utm_campaign, func.sum(Registration.count)).filter(
        Registration.date >= start, Registration.date <= end,
        Registration.status == "OK",
        ~Registration.utm_source.like("demo_%")).group_by(
        Registration.utm_source, Registration.utm_medium, Registration.utm_campaign).all()
    return rows


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


def breakdown(start: date, end: date):
    """Разрезы по нашим меткам + проверка наличия всех меток в данных."""
    regs = _group_registrations(start, end)
    orders = _group_orders(start, end)

    by_funnel = defaultdict(lambda: {"regs": 0, "orders": 0, "sum": 0})
    by_medium = defaultdict(lambda: {"regs": 0, "orders": 0})
    by_source = defaultdict(lambda: {"regs": 0, "orders": 0})
    for s, m, c, n in regs:
        ft = campaign_type(c) or "Прочее"
        by_funnel[ft]["regs"] += n or 0
        mk = known_medium(m) or "прочее"
        by_medium[mk]["regs"] += n or 0
        sk = known_source(s) or ("прочее" if (s or "").strip() else "без метки")
        by_source[sk]["regs"] += n or 0
    for s, m, c, n, total in orders:
        ft = campaign_type(c) or "Прочее"
        by_funnel[ft]["orders"] += n
        by_funnel[ft]["sum"] += total or 0
        mk = known_medium(m) or "прочее"
        by_medium[mk]["orders"] += n
        sk = known_source(s) or ("прочее" if (s or "").strip() else "без метки")
        by_source[sk]["orders"] += n

    # проверки наличия наших меток в данных
    seen_c = {c for _, _, c, _ in regs} | {c for _, _, c, _, _ in orders}
    seen_m = {m for _, m, _, _ in regs} | {m for _, m, _, _, _ in orders}
    seen_s = {s for s, _, _, _ in regs} | {s for s, _, _, _, _ in orders}
    norm = lambda xs: {str(x).lower().strip() for x in xs if x}
    nc, nm, ns = norm(seen_c), norm(seen_m), norm(seen_s)
    missing = {"campaigns": [c for c in CAMPAIGN_TYPES if c not in nc],
               "mediums": [m for m in MEDIUMS if m not in nm],
               "sources": [s for s in SOURCES if s not in ns]}
    return {"by_funnel": dict(by_funnel), "by_medium": dict(by_medium),
            "by_source": dict(by_source), "missing": missing}
