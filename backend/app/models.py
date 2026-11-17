import uuid
from datetime import datetime, time

from geoalchemy2 import Geometry
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Float, ForeignKey, Integer, String, Text, Time, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .events import ALL_EVENTS

SEVERITY_RANK = {"MODERATE": 1, "SEVERE": 2, "EXTREME": 3}


def severity_for_dbz(dbz: float) -> str | None:
    if dbz >= 55:
        return "EXTREME"
    if dbz >= 45:
        return "SEVERE"
    if dbz >= 35:
        return "MODERATE"
    return None


# ---------------------------------------------------------------- ORM
class Base(DeclarativeBase):
    pass


class UserSubscription(Base):
    __tablename__ = "user_subscriptions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    endpoint: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    location = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    min_severity_dbz: Mapped[int] = mapped_column(Integer, server_default=text("40"))
    quiet_hours_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    timezone: Mapped[str] = mapped_column(Text, server_default=text("'Europe/Rome'"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("NOW()"))


class ForecastPolygon(Base):
    __tablename__ = "forecast_polygons"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_timestamp: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    lead_time_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    severity_level: Mapped[str] = mapped_column(String(16), nullable=False)
    max_dbz: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_rain_mmh: Mapped[float] = mapped_column(Float, nullable=False)
    geom = mapped_column(Geometry("MULTIPOLYGON", srid=4326), nullable=False)


class AlertHistory(Base):
    __tablename__ = "alert_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("user_subscriptions.id", ondelete="CASCADE"))
    severity_level: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(8), server_default=text("'RADAR'"))
    ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("NOW()"))


# ---------------------------------------------------------------- API schemas
class PushKeys(BaseModel):
    p256dh: str = Field(min_length=10, max_length=512)
    auth: str = Field(min_length=4, max_length=256)


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2048)
    keys: PushKeys

    @field_validator("endpoint")
    @classmethod
    def https_only(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("endpoint must be https")
        return v


class Coordinates(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class AlertPrefs(BaseModel):
    min_severity_dbz: int | None = Field(default=None, ge=15, le=75)  # legacy, unused by matching
    alert_types: list[str] | None = Field(default=None, max_length=32)

    @field_validator("alert_types")
    @classmethod
    def known_types(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        bad = sorted(set(v) - ALL_EVENTS)
        if bad:
            raise ValueError(f"unknown alert types: {bad}")
        return sorted(set(v))
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    timezone: str | None = Field(default=None, max_length=64)


class SubscribeIn(Coordinates, AlertPrefs):
    subscription: PushSubscriptionIn


class LocationIn(Coordinates):
    endpoint: str


class SettingsIn(AlertPrefs):
    endpoint: str
    clear_quiet_hours: bool = False


class ResubscribeIn(BaseModel):
    old_endpoint: str
    subscription: PushSubscriptionIn


class UnsubscribeIn(BaseModel):
    endpoint: str


class SubscriptionOut(BaseModel):
    id: uuid.UUID
    lat: float
    lon: float
    min_severity_dbz: int
    quiet_hours_start: time | None
    quiet_hours_end: time | None
    timezone: str
    alert_types: list[str]
