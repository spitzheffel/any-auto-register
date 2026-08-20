from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(slots=True)
class CardRecord:
    id: int
    pan: str
    last4: str
    brand: str
    exp_month: int
    exp_year: int
    cvv: str
    holder_name: str = ""
    region: str = ""
    note: str = ""
    is_active: bool = True
    success_count: int = 0
    fail_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(slots=True)
class CardSecret:
    id: int
    pan: str
    cvv: str
    exp_month: int
    exp_year: int
    holder_name: str = ""


@dataclass(slots=True)
class CardCreateCommand:
    pan: str
    exp_month: int
    exp_year: int
    cvv: str
    holder_name: str = ""
    region: str = ""
    note: str = ""


@dataclass(slots=True)
class CardUpdateCommand:
    pan: Optional[str] = None
    exp_month: Optional[int] = None
    exp_year: Optional[int] = None
    cvv: Optional[str] = None
    holder_name: Optional[str] = None
    region: Optional[str] = None
    note: Optional[str] = None
