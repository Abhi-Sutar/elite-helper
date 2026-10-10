"""SQLAlchemy models: the single source of truth for the schema (Alembic autogenerates from these)."""

from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index,
                        Integer, MetaData, String, false, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite stores naive UTC; Postgres uses timestamptz)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


JsonType = JSON().with_variant(JSONB(), "postgresql")
BigId = BigInteger().with_variant(Integer(), "sqlite")  # SQLite INTEGER is already 64-bit


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Commodity(Base):
    """Dimension table: keeps the high-volume time-series rows narrow."""
    __tablename__ = "commodities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(80), unique=True)


class System(Base):
    __tablename__ = "systems"
    id64: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(120))
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    z: Mapped[float | None] = mapped_column(Float)
    population: Mapped[int | None] = mapped_column(BigInteger)
    allegiance: Mapped[str | None] = mapped_column(String(40))
    government: Mapped[str | None] = mapped_column(String(40))
    security: Mapped[str | None] = mapped_column(String(40))
    economy: Mapped[str | None] = mapped_column(String(40))
    second_economy: Mapped[str | None] = mapped_column(String(40))
    controlling_faction: Mapped[str | None] = mapped_column(String(120))
    conflicts: Mapped[list | None] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    factions_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(16), default="eddn", server_default="eddn")

    __table_args__ = (Index("ix_systems_name_lower", func.lower(name)),)


class Faction(Base):
    __tablename__ = "factions"
    system_id64: Mapped[int] = mapped_column(ForeignKey("systems.id64", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    government: Mapped[str | None] = mapped_column(String(40))
    allegiance: Mapped[str | None] = mapped_column(String(40))
    influence: Mapped[float | None] = mapped_column(Float)
    state: Mapped[str | None] = mapped_column(String(40))
    happiness: Mapped[str | None] = mapped_column(String(40))
    active_states: Mapped[list | None] = mapped_column(JsonType)
    pending_states: Mapped[list | None] = mapped_column(JsonType)
    recovering_states: Mapped[list | None] = mapped_column(JsonType)
    is_controlling: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(16), default="eddn", server_default="eddn")

    __table_args__ = (
        CheckConstraint("influence IS NULL OR (influence >= 0 AND influence <= 1)", name="influence_range"),
        Index("ix_factions_name_lower", func.lower(name)),
    )


class FactionHistory(Base):
    """Change-only time series of faction influence/state per system."""
    __tablename__ = "faction_history"
    system_id64: Mapped[int] = mapped_column(ForeignKey("systems.id64", ondelete="CASCADE"), primary_key=True)
    faction: Mapped[str] = mapped_column(String(120), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    influence: Mapped[float | None] = mapped_column(Float)
    state: Mapped[str | None] = mapped_column(String(40))

    __table_args__ = (Index("ix_faction_history_ts", "ts"),)


class Market(Base):
    __tablename__ = "markets"
    market_id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=False)
    station: Mapped[str] = mapped_column(String(120))
    system_name: Mapped[str] = mapped_column(String(120))
    system_id64: Mapped[int | None] = mapped_column(ForeignKey("systems.id64", ondelete="SET NULL"))
    station_type: Mapped[str | None] = mapped_column(String(40))
    is_carrier: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    economies: Mapped[list | None] = mapped_column(JsonType)
    prohibited: Mapped[list | None] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(16), default="eddn", server_default="eddn")

    __table_args__ = (
        Index("ix_markets_station_lower", func.lower(station)),
        Index("ix_markets_system_name_lower", func.lower(system_name)),
        Index("ix_markets_updated_at", "updated_at"),
    )


class CommodityPrice(Base):
    """Current snapshot per (market, commodity)."""
    __tablename__ = "commodity_prices"
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.market_id", ondelete="CASCADE"), primary_key=True)
    commodity_id: Mapped[int] = mapped_column(ForeignKey("commodities.id"), primary_key=True)
    buy_price: Mapped[int | None] = mapped_column(Integer)
    sell_price: Mapped[int | None] = mapped_column(Integer)
    mean_price: Mapped[int | None] = mapped_column(Integer)
    stock: Mapped[int | None] = mapped_column(BigInteger)
    demand: Mapped[int | None] = mapped_column(BigInteger)
    stock_bracket: Mapped[int | None] = mapped_column(Integer)
    demand_bracket: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (Index("ix_commodity_prices_commodity_id", "commodity_id"),)


class PriceHistory(Base):
    """Time series, one row per meaningful change. PK includes ts so it can become a hypertable/partitioned."""
    __tablename__ = "price_history"
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.market_id", ondelete="CASCADE"), primary_key=True)
    commodity_id: Mapped[int] = mapped_column(ForeignKey("commodities.id"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    buy_price: Mapped[int | None] = mapped_column(Integer)
    sell_price: Mapped[int | None] = mapped_column(Integer)
    stock: Mapped[int | None] = mapped_column(BigInteger)
    demand: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (Index("ix_price_history_ts", "ts"),)


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str | None] = mapped_column(String)
