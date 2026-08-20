from __future__ import annotations

from datetime import datetime, timezone

from domain.cards import CardCreateCommand, CardRecord, CardUpdateCommand
from infrastructure.cards_repository import CardsRepository


def normalize_pan(raw: str) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def mask_pan(pan: str) -> str:
    last4 = pan[-4:] if len(pan) >= 4 else pan
    return f"**** **** **** {last4}"


def luhn_valid(pan: str) -> bool:
    total = 0
    alt = False
    for ch in reversed(pan):
        digit = ord(ch) - 48
        if alt:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
        alt = not alt
    return total % 10 == 0


def _normalize_year(year: int) -> int:
    if year < 100:
        return 2000 + year
    return year


def validate_card_fields(
    *,
    pan: str,
    exp_month: int,
    exp_year: int,
    cvv: str,
) -> tuple[str, int, int, str]:
    pan_digits = normalize_pan(pan)
    if not (13 <= len(pan_digits) <= 19) or not luhn_valid(pan_digits):
        raise ValueError("卡号不合法")
    month = int(exp_month)
    year = _normalize_year(int(exp_year))
    if month < 1 or month > 12:
        raise ValueError("有效期不合法")
    now = datetime.now(timezone.utc)
    if year < now.year or (year == now.year and month < now.month):
        raise ValueError("有效期不合法")
    cvv_digits = "".join(ch for ch in str(cvv or "") if ch.isdigit())
    if len(cvv_digits) not in (3, 4):
        raise ValueError("CVV 不合法")
    return pan_digits, month, year, cvv_digits


class CardsService:
    def __init__(self, repository: CardsRepository | None = None):
        self.repository = repository or CardsRepository()

    def list_cards(self) -> list[dict]:
        return [self._serialize(item) for item in self.repository.list()]

    def get_card(self, card_id: int) -> dict | None:
        item = self.repository.get(card_id)
        if not item:
            return None
        return self._serialize(item, reveal=True)

    def create_card(self, command: CardCreateCommand) -> dict:
        pan, month, year, cvv = validate_card_fields(
            pan=command.pan,
            exp_month=command.exp_month,
            exp_year=command.exp_year,
            cvv=command.cvv,
        )
        item = self.repository.create(
            CardCreateCommand(
                pan=pan,
                exp_month=month,
                exp_year=year,
                cvv=cvv,
                holder_name=str(command.holder_name or "").strip(),
                region=str(command.region or "").strip(),
                note=str(command.note or "").strip(),
            )
        )
        if not item:
            raise ValueError("支付卡已存在")
        return self._serialize(item)

    def update_card(self, card_id: int, command: CardUpdateCommand) -> dict | None:
        existing = self.repository.get(card_id)
        if not existing:
            return None
        pan = existing.pan if command.pan is None else command.pan
        cvv = existing.cvv if command.cvv is None else command.cvv
        month = existing.exp_month if command.exp_month is None else int(command.exp_month)
        year = existing.exp_year if command.exp_year is None else int(command.exp_year)
        secrets_changed = command.pan is not None or command.cvv is not None
        expiry_changed = command.exp_month is not None or command.exp_year is not None
        if secrets_changed or expiry_changed:
            pan, month, year, cvv = validate_card_fields(
                pan=pan,
                exp_month=month,
                exp_year=year,
                cvv=cvv,
            )
        if command.pan is not None and self.repository.find_by_pan(pan, exclude_id=card_id):
            raise ValueError("支付卡已存在")
        item = self.repository.update(
            card_id,
            CardUpdateCommand(
                pan=None if command.pan is None else pan,
                exp_month=None if command.exp_month is None else month,
                exp_year=None if command.exp_year is None else year,
                cvv=None if command.cvv is None else cvv,
                holder_name=None if command.holder_name is None else str(command.holder_name).strip(),
                region=None if command.region is None else str(command.region).strip(),
                note=None if command.note is None else str(command.note).strip(),
            ),
        )
        return self._serialize(item) if item else None

    def delete_card(self, card_id: int) -> dict:
        return {"ok": self.repository.delete(card_id)}

    def toggle_card(self, card_id: int) -> dict | None:
        value = self.repository.toggle(card_id)
        if value is None:
            return None
        return {"is_active": value}

    @staticmethod
    def _serialize(item: CardRecord, *, reveal: bool = False) -> dict:
        payload = {
            "id": item.id,
            "pan": item.pan,
            "masked_pan": mask_pan(item.pan),
            "last4": item.last4,
            "brand": item.brand,
            "exp_month": item.exp_month,
            "exp_year": item.exp_year,
            "holder_name": item.holder_name,
            "region": item.region,
            "note": item.note,
            "is_active": item.is_active,
            "success_count": item.success_count,
            "fail_count": item.fail_count,
        }
        if reveal:
            payload["cvv"] = item.cvv
        return payload
