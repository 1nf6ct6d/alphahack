"""Alfa Context backend: FastAPI, SQLite, ML integration and evidence storage."""

from .config import Settings
from .db import Database

__all__ = ["Database", "Settings"]

__version__ = "1.0.0"
