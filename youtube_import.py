# -*- coding: utf-8 -*-
"""Импорт YouTube видео с ссылками и статистикой.
Обрабатывает оба канала: Школа Сыроделия + личный канал Алексея."""
import json, requests
from datetime import date, datetime
from db import db, Channel, ContentItem, ContentStat, RunLog, Notification, get_setting

YOUTUBE_CHANNELS = {
    "UCeysJxi5N0KRXr4m8blv9JA": ("YouTube", "https://www.youtube.com/@AlexeySyrover"),
    "UCXxcN92PX4rJG1wzTU7GygQ": ("YouTube 2", "https://www.youtube.com/channel/UCXxcN92PX4rJG1wzTU7GygQ"),
}


def _yt(path, params, key):
    params["key"] = key
    r = requests.get(f"https://www.googleapis.com/youtube/v3/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def import_youtube_videos(max_per_channel=50):
    """Импортирует видео с обоих YouTube каналов: название, ссылка, просмотры, лайки, комментарии."""
    key = get_setting("youtube_api_key")
    if not key:
        return {"error": "нет ключа YouTube API"}

    run = RunLog(kind="youtube_import", status="OK")
    db.session.add(run)
    db.session.commit()

    stats = {"videos": 0, "updated": 0, "channels": []}

    for ch_id, (ch_name, ch_url) in YOUTUBE_CHANNELS.items():
        # Найти или создать канал
        channel = Channel.query.filter_by(name=ch_name).first()
        if not channel:
            channel = Channel(platform="youtube", name=ch_name, url=ch_url)
            db.session.add(channel)
            db.session.commit()

        # Статистика канала
        try:
            info = _yt("channels", {"part": "statistics", "id": ch_id}, key)
            items = info.get("items", [])
            if items:
                st = items[0].get("statistics", {})
                from connectors import save_metric
                today = date.today()
                save_metric(run.id, channel.id, today, "followers",
                           int(st.get("subscriberCount", 0)), "youtube_api")
                db.session.commit()
                stats["channels"].append(f"{ch_name}: {st.get('subscriberCount','?')} подписчиков")
        except Exception as e:
            stats["channels"].append(f"{ch_name}: ERROR {e}")
            continue

        # Получить видео (search → video IDs → statistics)
        try:
            search = _yt("search", {"part": "snippet", "channelId": ch_id,
                                     "order": "date", "type": "video",
                                     "maxResults": max_per_channel}, key)
        except Exception:
            # try older param name
            try:
                search = _yt("search", {"part": "snippet", "channelId": ch_id,
                                          "order": "date", "type": "video",
                                          "maxResults": max_per_channel}, key)
            except Exception as e:
                stats["channels"].append(f"{ch_name}: SEARCH ERROR {e}")
                continue

        video_ids = [it["id"]["videoId"] for it in search.get("items", []) if it.get("id", {}).get("videoId")]
        if not video_ids:
            continue

        # Получить статистику по видео (пакетами по 50)
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i+50]
            try:
                vids = _yt("videos", {"part": "statistics,snippet,contentDetails",
                            "id": ",".join(batch)}, key)
            except Exception:
                continue

            for v in vids.get("items", []):
                vid_id = v["id"]
                sn = v.get("snippet", {})
                vs = v.get("statistics", {})
                pub = sn.get("publishedAt", "")[:19].replace("T", " ")
                try:
                    pub_dt = datetime.strptime(pub, "%Y-%m-%d %H:%M:%S")
                    pub_date = pub_dt.date()
                except ValueError:
                    pub_date = date.today()

                link = f"https://www.youtube.com/watch?v={vid_id}"

                # Найти или создать ContentItem
                ci = ContentItem.query.filter_by(channel_id=channel.id, external_id=vid_id).first()
                if not ci:
                    ci = ContentItem(
                        channel_id=channel.id, external_id=vid_id, link=link,
                        published_at=pub_dt, format="video",
                        title=sn.get("title", "")[:500],
                        text=sn.get("description", "")[:2000])
                    db.session.add(ci)
                    db.session.flush()
                    stats["videos"] += 1
                else:
                    stats["updated"] += 1

                # Статистика видео
                views = int(vs.get("viewCount", 0))
                likes = int(vs.get("likeCount", 0))
                comments = int(vs.get("commentCount", 0))

                # Обновить или создать ContentStat
                cs = ContentStat.query.filter_by(content_id=ci.id, date=pub_date).first()
                if not cs:
                    cs = ContentStat(content_id=ci.id, date=pub_date)
                    db.session.add(cs)
                cs.views = views
                cs.reach = views  # для YouTube views = reach
                cs.likes = likes
                cs.comments = comments
                # Сохранения и репосты недоступны через базовый API

                db.session.commit()

    run.details = f"videos={stats['videos']} updated={stats['updated']}"
    db.session.commit()
    return stats


# Fix for snippet field name
