# -*- coding: utf-8 -*-
"""Конкуренты школы сыроделия — данные и анализ."""
import json, requests
from datetime import date, datetime
from db import db, Channel, MetricSnapshot, RunLog, Notification, get_setting

COMPETITORS = [
    {
        "name": "Школа Куртинских",
        "site": "cheeseandmeat.ru",
        "description": "Крупнейшая школа сыроделия РФ и СНГ. 500+ технологий. Павел и Елена Куртинские, практика в Италии/Швейцарии. Москва (Зеленоград).",
        "platforms": {
            "instagram": {"url": "https://www.instagram.com/cheese_making.ru/", "followers": 11000},
            "youtube": {"url": "https://www.youtube.com/@cheese_making"},
            "vk": {"url": "https://vk.com/kurtschool"},
            "rutube": {"url": "https://rutube.ru/channel/14501724"},
        },
        "students": "десятки тысяч",
        "strengths": ["500+ технологий", "очная + онлайн", "5.0 на Яндексе (159 отзывов)"],
        "weaknesses": ["маленький Instagram (11K)", "нет прозрачного прайса"],
    },
    {
        "name": "Cheese Lab (Ивлева)",
        "site": "cheese-lab.ru",
        "description": "Онлайн-школа с 2018 г. Светлана Ивлева — 10 лет опыта, владелец сыроварни. 3500+ учеников, 11 курсов, 20+ видов сыров.",
        "platforms": {
            "vk": {"url": "https://vk.com/cheeselab", "followers": 39830},
            "youtube": {"url": "https://www.youtube.com/channel/UCpbuGhj6zEL0p2_Cs8hTOyw"},
            "telegram": {"url": "https://t.me/cheese_lab"},
        },
        "students": "3500+",
        "strengths": ["39.8K в VK — лидер", "бесплатный мини-курс", "технологические карты"],
        "weaknesses": ["нет MAX и Дзен", "маленький YouTube"],
    },
    {
        "name": "Appetissimo (сыродел.рф)",
        "site": "appetissimo.tilda.ws",
        "description": "Медиабренд сыроделия. Курс «4 дня — 9 сыров». 243K подписчиков на YouTube — крупнейший канал о сыроделии в СНГ.",
        "platforms": {
            "youtube": {"url": "https://www.youtube.com/@appetissimo", "followers": 243000},
            "telegram": {"url": "https://t.me/appetissimo_cheese"},
            "rutube": {"url": "https://rutube.ru/channel/24600736"},
            "dzen": {"url": "https://dzen.ru/appetissimo"},
            "vk": {"url": "https://vk.com/cheesevillage"},
        },
        "students": "не публично",
        "strengths": ["243K YouTube — лидер СНГ", "практический курс 4 дня", "90+ бесплатных видео"],
        "weaknesses": ["нет Instagram", "закрытые цены", "только очная школа"],
    },
    {
        "name": "Cheese Village",
        "site": "vk.com/cheesevillage",
        "description": "Школа сыроделов в Москве. «Научим варить за 4 дня 9 сыров на профессиональном оборудовании».",
        "platforms": {
            "vk": {"url": "https://vk.com/cheesevillage"},
        },
        "students": "не публично",
        "strengths": ["профессиональное оборудование"],
        "weaknesses": ["слабое онлайн-присутствие"],
    },
]


def setup_competitors():
    added = 0
    for comp in COMPETITORS:
        plat_key = "youtube" if "youtube" in comp["platforms"] else "vk"
        plat = comp["platforms"].get(plat_key, {})
        ch = Channel.query.filter_by(name=comp["name"]).first()
        if not ch:
            ch = Channel(platform=plat_key, name=comp["name"],
                        url=comp.get("site", plat.get("url", "")),
                        is_competitor=True)
            db.session.add(ch)
            db.session.flush()
            added += 1
        if plat.get("followers"):
            today = date.today()
            existing = MetricSnapshot.query.filter_by(
                channel_id=ch.id, date=today, metric="followers").first()
            if not existing:
                db.session.add(MetricSnapshot(
                    channel_id=ch.id, date=today, metric="followers",
                    value=plat["followers"], status="OK", source="competitor_research"))
    db.session.commit()
    return added


def competitive_analysis():
    our_channels = Channel.query.filter_by(is_active=True, is_competitor=False).all()
    comp_channels = Channel.query.filter_by(is_competitor=True, is_active=True).all()

    def total_followers(channels):
        total = 0
        for ch in channels:
            snap = MetricSnapshot.query.filter_by(channel_id=ch.id, metric="followers") \
                .order_by(MetricSnapshot.date.desc()).first()
            if snap and snap.value:
                total += snap.value
        return total

    our_total = total_followers(our_channels)
    comp_total = total_followers(comp_channels)

    platforms = {}
    for ch in our_channels:
        snap = MetricSnapshot.query.filter_by(channel_id=ch.id, metric="followers") \
            .order_by(MetricSnapshot.date.desc()).first()
        if snap and snap.value:
            p = platforms.setdefault(ch.platform, {"ours": 0, "competitors": {}, "max_comp": 0, "max_comp_name": "", "position": "lead"})
            p["ours"] += snap.value
    for ch in comp_channels:
        snap = MetricSnapshot.query.filter_by(channel_id=ch.id, metric="followers") \
            .order_by(MetricSnapshot.date.desc()).first()
        if snap and snap.value:
            p = platforms.setdefault(ch.platform, {"ours": 0, "competitors": {}, "max_comp": 0, "max_comp_name": "", "position": "lead"})
            p["competitors"][ch.name] = snap.value

    for plat, data in platforms.items():
        if data["competitors"]:
            data["max_comp"] = max(data["competitors"].values())
            data["max_comp_name"] = max(data["competitors"], key=data["competitors"].get)
            if data["ours"] > data["max_comp"]:
                data["position"] = "lead"
            elif data["ours"] > data["max_comp"] * 0.5:
                data["position"] = "close"
            else:
                data["position"] = "behind"
        else:
            data["max_comp"] = 0
            data["max_comp_name"] = ""
            data["position"] = "lead"

    insights = []
    if our_total > 0:
        for comp in COMPETITORS:
            comp_followers = 0
            for plat_data in comp["platforms"].values():
                if plat_data.get("followers"):
                    comp_followers += plat_data["followers"]
            if comp_followers > 0:
                ratio = our_total / comp_followers
                if ratio > 1:
                    insights.append("Мы превосходим «{}» в {:.1f}x по подписчикам".format(comp["name"], ratio))
                else:
                    insights.append("«{}» превосходит нас в {:.1f}x по подписчикам".format(comp["name"], 1 / ratio))

    market_share = round(our_total / max(our_total + comp_total, 1) * 100, 1)
    return {
        "our_total": our_total,
        "market_share": market_share,
        "competitor_total": comp_total,
        "platforms": platforms,
        "competitors": COMPETITORS,
        "insights": insights,
    }
