import enum
import json
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, create_engine, event
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

DATABASE_URL = "sqlite:///cycling-alarm.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

# Enable WAL mode for SQLite to allow concurrent reads
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class FinishType(str, enum.Enum):
    sprint = "sprint"
    hill = "hill"
    mountain = "mountain"
    uphill_finish = "uphill_finish"
    tt = "tt"


class EventType(str, enum.Enum):
    attack = "attack"
    sprint = "sprint"
    crash = "crash"
    summit = "summit"
    gap = "gap"
    caught = "caught"
    other = "other"


class FeedbackType(str, enum.Enum):
    too_early = "too_early"
    good = "good"
    too_late = "too_late"
    unnecessary = "unnecessary"


class Race(Base):
    __tablename__ = "races"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    date = Column(String, nullable=False)  # YYYY-MM-DD
    ticker_url = Column(String, nullable=True)
    stage_number = Column(Integer, nullable=True)
    difficulty_score = Column(Float, nullable=True)
    finish_type = Column(Enum(FinishType), nullable=True)
    hbo_available = Column(Boolean, default=False)
    hbo_start_time = Column(String, nullable=True)  # HH:MM
    created_at = Column(DateTime, default=datetime.utcnow)

    stage_profile = relationship("StageProfile", back_populates="race", uselist=False)
    ticker_events = relationship("TickerEvent", back_populates="race")
    notifications = relationship("Notification", back_populates="race")


class StageProfile(Base):
    __tablename__ = "stage_profile"

    id = Column(Integer, primary_key=True, index=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    cols_json = Column(Text, nullable=True)  # JSON: [{name, km_position, category}]
    finish_km = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    race = relationship("Race", back_populates="stage_profile")

    @property
    def cols(self):
        if self.cols_json:
            return json.loads(self.cols_json)
        return []

    @cols.setter
    def cols(self, value):
        self.cols_json = json.dumps(value)


class TickerEvent(Base):
    __tablename__ = "ticker_events"

    id = Column(Integer, primary_key=True, index=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    timestamp = Column(DateTime, nullable=True)
    event_type = Column(Enum(EventType), default=EventType.other)
    description = Column(Text, nullable=True)
    km_remaining = Column(Float, nullable=True)
    raw_text = Column(Text, nullable=False)
    scraped_at = Column(DateTime, default=datetime.utcnow)

    race = relationship("Race", back_populates="ticker_events")
    notifications = relationship("Notification", back_populates="trigger_event")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    triggered_at = Column(DateTime, default=datetime.utcnow)
    score = Column(Float, nullable=False)
    event_summary = Column(Text, nullable=True)
    trigger_event_id = Column(Integer, ForeignKey("ticker_events.id"), nullable=True)
    telegram_message_id = Column(Integer, nullable=True)

    race = relationship("Race", back_populates="notifications")
    trigger_event = relationship("TickerEvent", back_populates="notifications")
    feedback = relationship("Feedback", back_populates="notification", uselist=False)


class Feedback(Base):
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, index=True)
    notification_id = Column(Integer, ForeignKey("notifications.id"), nullable=False)
    feedback = Column(Enum(FeedbackType), nullable=False)
    received_at = Column(DateTime, default=datetime.utcnow)

    notification = relationship("Notification", back_populates="feedback")


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db() -> Session:
    db = SessionLocal()
    try:
        return db
    except Exception:
        db.close()
        raise
