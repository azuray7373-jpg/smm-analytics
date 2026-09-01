from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Channel(db.Model):
    __tablename__ = "channels"
    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(32), nullable=False)   # instagram/youtube/max/telegram/tiktok/vk/dzen
    name = db.Column(db.String(64), nullable=False)
    url = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True)

    @property
    def slug(self):
        import re
        return re.sub(r"\W+", "-", (self.platform + "-" + self.name).lower()).strip("-")


class RunLog(db.Model):
    __tablename__ = "run_logs"
    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    kind = db.Column(db.String(32))
    status = db.Column(db.String(16), default="OK")
    details = db.Column(db.Text)


class MetricSnapshot(db.Model):
    """История изменений: ничего не перезаписывается, только добавляются строки.
    Актуальное значение = последняя строка по (channel, date, metric)."""
    __tablename__ = "metric_snapshots"
    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(db.Integer, db.ForeignKey("channels.id"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    metric = db.Column(db.String(32), nullable=False, index=True)
    value = db.Column(db.Float)                 # NULL если данные не получены
    status = db.Column(db.String(16), default="OK")  # OK/MISSING/NOT_AVAILABLE/ERROR/MANUAL/ESTIMATED
    source = db.Column(db.String(64))           # youtube_api / manual / csv / livedune / ...
    run_id = db.Column(db.Integer, db.ForeignKey("run_logs.id"))
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)


class ContentItem(db.Model):
    __tablename__ = "content_items"
    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(db.Integer, db.ForeignKey("channels.id"), nullable=False, index=True)
    channel = db.relationship("Channel")
    external_id = db.Column(db.String(128))
    link = db.Column(db.String(512))
    published_at = db.Column(db.DateTime)
    format = db.Column(db.String(32))    # reels/post/story/short/video/article
    title = db.Column(db.String(512))
    text = db.Column(db.Text)
    duration_sec = db.Column(db.Integer)
    cta = db.Column(db.String(255))
    ai_tags = db.Column(db.Text)         # JSON: тема, рубрика, тип, боль, сегмент, триггер и т.д.


class ContentStat(db.Model):
    __tablename__ = "content_stats"
    id = db.Column(db.Integer, primary_key=True)
    content_id = db.Column(db.Integer, db.ForeignKey("content_items.id"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    views = db.Column(db.Float)
    reach = db.Column(db.Float)
    likes = db.Column(db.Float)
    comments = db.Column(db.Float)
    saves = db.Column(db.Float)
    shares = db.Column(db.Float)
    reactions = db.Column(db.Float)
    subs = db.Column(db.Float)
    registrations = db.Column(db.Float)


class Registration(db.Model):
    __tablename__ = "registrations"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    utm_source = db.Column(db.String(64), index=True)
    utm_medium = db.Column(db.String(64))
    utm_campaign = db.Column(db.String(128))
    landing = db.Column(db.String(255))
    count = db.Column(db.Float, default=0)
    status = db.Column(db.String(16), default="OK")
    gc_user_id = db.Column(db.Integer, index=True)   # если регистрация пришла из GetCourse


class GcOrder(db.Model):
    """Заказ (сделка) из GetCourse. Текущее состояние; вся история изменений — в GcEvent."""
    __tablename__ = "gc_orders"
    id = db.Column(db.Integer, primary_key=True)          # gc deal id
    deal_number = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, index=True)
    date = db.Column(db.Date, index=True)
    user_id = db.Column(db.Integer, index=True)
    email = db.Column(db.String(255))
    phone = db.Column(db.String(64))
    product = db.Column(db.String(255))
    amount = db.Column(db.Float)
    currency = db.Column(db.String(8), default="RUB")
    status = db.Column(db.String(32), index=True)         # new/in_work/payed/cancelled/...
    status_title = db.Column(db.String(64))
    direction = db.Column(db.String(16))                  # incoming/outgoing
    customer_status = db.Column(db.String(16), index=True)  # new/returning
    utm_source = db.Column(db.String(64), index=True)
    utm_medium = db.Column(db.String(64))
    utm_campaign = db.Column(db.String(128))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class GcPayment(db.Model):
    __tablename__ = "gc_payments"
    id = db.Column(db.Integer, primary_key=True)          # gc payment id
    created_at = db.Column(db.DateTime, index=True)
    date = db.Column(db.Date, index=True)
    user_id = db.Column(db.Integer, index=True)
    email = db.Column(db.String(255))
    amount = db.Column(db.Float)
    currency = db.Column(db.String(8), default="RUB")
    status = db.Column(db.String(32))                     # accepted/expected/returned
    deal_id = db.Column(db.Integer, index=True)
    product = db.Column(db.String(255))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class GcEvent(db.Model):
    """Append-only история: что пришло из GetCourse и когда (по ТЗ — ничего не терять)."""
    __tablename__ = "gc_events"
    id = db.Column(db.Integer, primary_key=True)
    entity = db.Column(db.String(16))                     # user/deal/payment
    entity_id = db.Column(db.String(64), index=True)
    payload = db.Column(db.Text)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class ManualNote(db.Model):
    """Ручной контекст: продукт, цель недели, KPI, события."""
    __tablename__ = "manual_notes"
    id = db.Column(db.Integer, primary_key=True)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    product = db.Column(db.String(255))
    goal = db.Column(db.Text)
    kpi = db.Column(db.Text)
    events = db.Column(db.Text)


class Report(db.Model):
    __tablename__ = "reports"
    id = db.Column(db.Integer, primary_key=True)
    rtype = db.Column(db.String(16))  # weekly / monthly
    start = db.Column(db.Date, nullable=False)
    end = db.Column(db.Date, nullable=False)
    payload = db.Column(db.Text)      # JSON с рассчитанными показателями
    ai_text = db.Column(db.Text)
    controller_text = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    level = db.Column(db.String(8))   # error/warn/info
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False)


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text)


def get_setting(key, default=""):
    s = Setting.query.get(key)
    return s.value if s else default


def set_setting(key, value):
    s = Setting.query.get(key)
    if not s:
        s = Setting(key=key)
        db.session.add(s)
    s.value = value
