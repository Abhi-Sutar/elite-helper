"""SQLAlchemy models: the single source of truth for the schema (Alembic autogenerates from these)."""

from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index,
                        Integer, MetaData, SmallInteger, String, false, func, true)
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
    logged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # when a faction_history row was last written

    __table_args__ = (
        CheckConstraint("influence IS NULL OR (influence >= 0 AND influence <= 1)", name="influence_range"),
        Index("ix_factions_name_lower", func.lower(name)),
    )


class FactionInfo(Base):
    """Per-faction facts EDDN does not carry (currently whether it is a player faction, from EDSM). No row = unknown."""
    __tablename__ = "faction_info"
    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    is_player: Mapped[bool] = mapped_column(Boolean)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(16), default="edsm", server_default="edsm")

    __table_args__ = (Index("ix_faction_info_name_lower", func.lower(name), unique=True),)


class FactionHistory(Base):
    """
    Time series of one faction in one system: one row per observation in full mode (only meaningful changes in
    thresholded mode). States are lists of {"state", "trend"}; present=False marks that the faction left the system.
    """
    __tablename__ = "faction_history"
    system_id64: Mapped[int] = mapped_column(ForeignKey("systems.id64", ondelete="CASCADE"), primary_key=True)
    faction: Mapped[str] = mapped_column(String(120), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    influence: Mapped[float | None] = mapped_column(Float)
    state: Mapped[str | None] = mapped_column(String(40))
    happiness: Mapped[str | None] = mapped_column(String(40))
    active_states: Mapped[list | None] = mapped_column(JsonType)
    pending_states: Mapped[list | None] = mapped_column(JsonType)
    recovering_states: Mapped[list | None] = mapped_column(JsonType)
    present: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

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
    logged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # when a price_history row was last written
    seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)    # last message that contained this commodity

    __table_args__ = (Index("ix_commodity_prices_commodity_id", "commodity_id"),)


class PriceHistory(Base):
    """
    Time series of one commodity at one market: one row per market update in full mode (only meaningful changes in
    thresholded mode). PK includes ts so it can be a hypertable. buy_price is what the station sells at (needs
    stock); sell_price is what it pays (needs demand); a side that is inactive reports 0.
    """
    __tablename__ = "price_history"
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.market_id", ondelete="CASCADE"), primary_key=True)
    commodity_id: Mapped[int] = mapped_column(ForeignKey("commodities.id"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    buy_price: Mapped[int | None] = mapped_column(Integer)
    sell_price: Mapped[int | None] = mapped_column(Integer)
    stock: Mapped[int | None] = mapped_column(BigInteger)
    demand: Mapped[int | None] = mapped_column(BigInteger)
    mean_price: Mapped[int | None] = mapped_column(Integer)
    stock_bracket: Mapped[int | None] = mapped_column(SmallInteger)
    demand_bracket: Mapped[int | None] = mapped_column(SmallInteger)

    __table_args__ = (Index("ix_price_history_ts", "ts"),)


class MarketUpdate(Base):
    """Event log: one row per accepted market message - the exact update rate ("traffic") of a station."""
    __tablename__ = "market_update"
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.market_id", ondelete="CASCADE"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    uploader: Mapped[str] = mapped_column(String(64), primary_key=True, default="", server_default="")
    software: Mapped[str] = mapped_column(String(40), default="", server_default="")
    commodities: Mapped[int | None] = mapped_column(SmallInteger)   # commodities in the message
    changed: Mapped[int | None] = mapped_column(SmallInteger)       # price_history rows this message produced

    __table_args__ = (Index("ix_market_update_ts", "ts"),)


class SystemUpdate(Base):
    """Event log: one row per accepted journal message that carried factions."""
    __tablename__ = "system_update"
    system_id64: Mapped[int] = mapped_column(ForeignKey("systems.id64", ondelete="CASCADE"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    uploader: Mapped[str] = mapped_column(String(64), primary_key=True, default="", server_default="")
    software: Mapped[str] = mapped_column(String(40), default="", server_default="")
    factions: Mapped[int | None] = mapped_column(SmallInteger)
    changed: Mapped[int | None] = mapped_column(SmallInteger)       # faction_history rows this message produced

    __table_args__ = (Index("ix_system_update_ts", "ts"),)


class Station(Base):
    """
    A station, outpost or surface settlement as listed by EDSM, with its controlling faction (fleet carriers are not
    stored). Replaced as a whole per system by `store.replace_stations`; `station_sync` records that it was fetched.
    """
    __tablename__ = "stations"
    system_id64: Mapped[int] = mapped_column(ForeignKey("systems.id64", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    market_id: Mapped[int | None] = mapped_column(BigId)
    station_type: Mapped[str | None] = mapped_column(String(40))
    body: Mapped[str | None] = mapped_column(String(120))
    distance_to_arrival_ls: Mapped[float | None] = mapped_column(Float)
    economy: Mapped[str | None] = mapped_column(String(40))
    second_economy: Mapped[str | None] = mapped_column(String(40))
    allegiance: Mapped[str | None] = mapped_column(String(40))
    government: Mapped[str | None] = mapped_column(String(40))
    controlling_faction: Mapped[str | None] = mapped_column(String(120))
    has_market: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    has_shipyard: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    has_outfitting: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    services: Mapped[list | None] = mapped_column(JsonType)
    info_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)   # EDSM's information timestamp
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(16), default="edsm", server_default="edsm")

    __table_args__ = (
        Index("ix_stations_market_id", "market_id"),
        Index("ix_stations_controlling_faction_lower", func.lower(controlling_faction)),
    )


class StationSync(Base):
    """We asked EDSM for this system's stations at `fetched_at` - tells "no stations" apart from "not fetched yet"."""
    __tablename__ = "station_sync"
    system_id64: Mapped[int] = mapped_column(ForeignKey("systems.id64", ondelete="CASCADE"), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    station_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    source: Mapped[str] = mapped_column(String(16), default="edsm", server_default="edsm")


class JournalCommander(Base):
    """A commander seen in the local journal files (FID is the stable id; the name can change)."""
    __tablename__ = "journal_commander"
    fid: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(80))
    first_seen: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime)


class JournalFile(Base):
    """
    One Journal.*.log file and how far it has been ingested: `size_bytes` is what was read, `lines_read` the last
    physical line known to be final (a half-written last line is not counted until it completes).
    """
    __tablename__ = "journal_file"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    fid: Mapped[str | None] = mapped_column(String(40))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    lines_read: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    first_ts: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_ts: Mapped[datetime | None] = mapped_column(UTCDateTime)
    complete: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime)


class JournalEvent(Base):
    """
    One journal line, raw JSON kept in `data` so views can be rebuilt by replaying. The system/body/station columns
    are the commander's location *after* the event (stamped during ingest), which makes per-system queries cheap.
    """
    __tablename__ = "journal_event"
    file_id: Mapped[int] = mapped_column(ForeignKey("journal_file.id", ondelete="CASCADE"), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    fid: Mapped[str | None] = mapped_column(String(40))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    event: Mapped[str] = mapped_column(String(60))
    system_id64: Mapped[int | None] = mapped_column(BigId)
    system_name: Mapped[str | None] = mapped_column(String(120))
    body: Mapped[str | None] = mapped_column(String(120))
    station: Mapped[str | None] = mapped_column(String(120))
    market_id: Mapped[int | None] = mapped_column(BigId)
    data: Mapped[dict | None] = mapped_column(JsonType)

    __table_args__ = (
        Index("ix_journal_event_fid_ts", "fid", "ts"),
        Index("ix_journal_event_fid_event_ts", "fid", "event", "ts"),
        Index("ix_journal_event_system_id64_ts", "system_id64", "ts"),
    )


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str | None] = mapped_column(String)
