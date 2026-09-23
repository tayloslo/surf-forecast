"""
Persistence layer. Every surf spot a user creates, and every scoring
preference they configure, is saved here in SQLite so nothing is lost
across restarts. The DB file lives next to this module by default but
can be overridden with the SURF_DB_PATH env var (useful for deployment).
"""
import os
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timezone

DB_PATH = os.environ.get("SURF_DB_PATH", os.path.join(os.path.dirname(__file__), "surf_spots.db"))
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Spot(Base):
    """A user-defined surf break, with the buoy used to sanity-check
    forecasts and the wave/wind preferences used to score conditions."""
    __tablename__ = "spots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)

    # The beach's orientation, in degrees the beach FACES (i.e. the
    # direction swell needs to come FROM to hit the beach roughly head-on).
    # E.g. a south-facing California beach faces ~180 (or use the coastline
    # normal). Used to filter/score swell direction relevance.
    facing_direction = Column(Float, nullable=False, default=270.0)

    # Ideal swell window, in degrees off `facing_direction`, that this spot
    # can still receive well (e.g. a wide-open beach might tolerate 60-90
    # degrees off; a tucked-in point break might only want +/-20).
    swell_window_deg = Column(Float, nullable=False, default=45.0)

    # Ideal swell period range in seconds - longer period = more organized,
    # more powerful groundswell; shorter = weaker windswell.
    min_good_period_s = Column(Float, nullable=False, default=8.0)
    ideal_period_s = Column(Float, nullable=False, default=12.0)

    # Wave height sweet spot in meters (at the buoy/open-ocean, pre-shoaling)
    min_good_height_m = Column(Float, nullable=False, default=0.5)
    ideal_height_m = Column(Float, nullable=False, default=1.5)
    max_good_height_m = Column(Float, nullable=False, default=3.0)

    # Wind tolerance: onshore wind ruins surf, offshore/cross grooms it.
    # max_good_wind_kmh applies fully once wind is onshore (within
    # onshore_window_deg of blowing straight onto the beach, i.e. opposite
    # of facing_direction).
    max_good_wind_kmh = Column(Float, nullable=False, default=15.0)
    onshore_window_deg = Column(Float, nullable=False, default=90.0)

    # Nearest NOAA buoy station id, for showing live observed conditions
    # alongside the modeled forecast (sanity check / ground truth).
    nearest_buoy_id = Column(String, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def init_db():
    Base.metadata.create_all(engine)


def get_session():
    return SessionLocal()
