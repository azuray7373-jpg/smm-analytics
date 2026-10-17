# -*- coding: utf-8 -*-
"""Импорт официальных выгрузок TikTok (зип-архивы из TikTok Studio → Аналитика → Экспорт).

Понимает архивы вида Overview_*.zip (Video Views, Profile Views, Likes, Comments,
Shares по дням) и Followers_*.zip (FollowerHistory.csv — подписчики и прирост,
FollowerActivity.csv — активность по часам, FollowerGender / FollowerTopTerritories).

Даты в выгрузках без года («2 августа») — год подставляется из имени архива или
выбирается так, чтобы дата не была в будущем.
"""
import csv
import io
import re
import zipfile
from datetime import date, datetime

from db import db, Channel, ContentItem, ContentStat, RunLog, Notification, get_setting

TIKTOK_CHANNEL_NAME = "TikTok"

RU_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "ма": 5,
             "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
             "ноябр": 11, "декабр": 12}


def _parse_ru_date(s, year_hint=None):
    """«2 августа» [2026] / «2026-08-02» → date. Год берётся из подсказки."""
    s = (s or "").strip()
    m = re.match(r"(\d{1,2})\s+([а-яё]+)", s.lower())
    if m:
        mon = next((n for k, n in RU_MONTHS.items() if s.lower().find(k) >= 0), None)
        if not mon:
            for k, n in RU_MONTHS.items():
                if k in s.lower():
                    mon = n
                    break
        if not mon:
            return None
        day = int(m.group(1))
        year = year_hint or date.today().year
        try:
            return date(year, mon, day)
        except ValueError:
            return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _year_from_name(name):
    m = re.search(r"(20\d{2})", name or "")
    return int(m.group(1)) if m else None


def _get_channel():
    ch = Channel.query.filter_by(name=TIKTOK_CHANNEL_NAME).first()
    if not ch:
        ch = Channel(platform="tiktok", name=TIKTOK_CHANNEL_NAME,
                     url="https://www.tiktok.com/@alexey_syrover")
        db.session.add(ch)
        db.session.commit()
    return ch


def import_tiktok_zip(fileobj):
    """Главный вход: файл .zip из TikTok Studio. Возвращает сводку."""
    run = RunLog(kind="tiktok_import", status="OK")
    db.session.add(run)
    db.session.commit()

    ch = _get_channel()
    from connectors import save_metric

    zf = zipfile.ZipFile(io.BytesIO(fileobj.read()))
    names = zf.namelist()
    year = _year_from_name(getattr(fileobj, "filename", "") or "")
    stats = {"days": 0, "followers_days": 0, "hours": 0, "files": []}

    def read_csv(z, name):
        raw = z.read(name).decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(raw)))

    for name in names:
        low = name.lower()
        rows = None
        if "overview" in low and low.endswith(".csv"):
            rows = read_csv(zf, name)
            for r in rows:
                d = _parse_ru_date(list(r.values())[0], year)
                if not d:
                    continue
                def num(k):
                    v = (r.get(k) or "").replace(" ", "").replace(",", ".")
                    try:
                        return float(v) if v not in ("", "-") else None
                    except ValueError:
                        return None
                save_metric(run.id, ch.id, d, "views", num("Video Views"), "tiktok_csv")
                save_metric(run.id, ch.id, d, "likes", num("Likes"), "tiktok_csv")
                save_metric(run.id, ch.id, d, "comments", num("Comments"), "tiktok_csv")
                save_metric(run.id, ch.id, d, "shares", num("Shares"), "tiktok_csv")
                # Охват у TikTok = уникальные зрители; выгрузка его не содержит,
                # просмотры сохраняем отдельной метрикой, reach не выдумываем.
                stats["days"] += 1
            stats["files"].append(name)
            db.session.commit()

        elif "followerhistory" in low:
            rows = read_csv(zf, name)
            prev = None
            for r in rows:
                d = _parse_ru_date(list(r.values())[0], year)
                if not d:
                    continue
                def num2(k):
                    v = (r.get(k) or "").replace(" ", "").replace(",", ".")
                    try:
                        return float(v) if v not in ("", "-") else None
                    except ValueError:
                        return None
                followers = num2("Followers")
                diff = num2("Difference in followers from previous day")
                save_metric(run.id, ch.id, d, "followers", followers, "tiktok_csv")
                if diff is not None:
                    save_metric(run.id, ch.id, d, "subscribed", max(diff, 0), "tiktok_csv")
                    save_metric(run.id, ch.id, d, "unsubscribed", max(-diff, 0), "tiktok_csv")
                elif prev is not None and followers is not None:
                    delta = followers - prev
                    save_metric(run.id, ch.id, d, "subscribed", max(delta, 0), "tiktok_csv")
                    save_metric(run.id, ch.id, d, "unsubscribed", max(-delta, 0), "tiktok_csv")
                if followers is not None:
                    prev = followers
                stats["followers_days"] += 1
            stats["files"].append(name)
            db.session.commit()

        elif "followeractivity" in low:
            # Активность подписчиков по часам — идёт в «лучшее время публикации»
            # как подписчики-активность (готовая таблица час × день).
            rows = read_csv(zf, name)
            grid = {}
            for r in rows:
                d = _parse_ru_date(list(r.values())[0], year)
                hour = r.get("Hour")
                act = (r.get("Active followers") or "").replace(" ", "")
                if not d or hour is None:
                    continue
                try:
                    grid.setdefault(d.strftime("%Y-%m-%d"), {})[int(hour)] = float(act)
                except ValueError:
                    continue
            if grid:
                import json
                old = get_setting("tiktok_activity_grid", "null")
                try:
                    merged = json.loads(old) if old and old != "null" else {}
                except Exception:
                    merged = {}
                merged.update(grid)
                # держим только последние 120 дней активности
                keep = sorted(merged)[-120:]
                merged = {k: merged[k] for k in keep}
                from db import set_setting
                set_setting("tiktok_activity_grid", json.dumps(merged))
                stats["hours"] = sum(len(v) for v in grid.values())
            stats["files"].append(name)
            db.session.commit()

        elif "followergender" in low or "territories" in low:
            # Демография: сохраняем как справочный JSON для карточки канала
            import json
            rows = read_csv(zf, name)
            payload = [{k: v for k, v in r.items()} for r in rows]
            key = "tiktok_gender" if "gender" in low else "tiktok_geo"
            from db import set_setting
            set_setting(key, json.dumps(payload, ensure_ascii=False))
            stats["files"].append(name)
            db.session.commit()

    msg = (f"TikTok: дней статистики {stats['days']}, дней подписчиков {stats['followers_days']}, "
           f"часов активности {stats['hours']}")
    run.message = msg
    db.session.add(Notification(level="ok", message=msg))
    db.session.commit()
    return stats
