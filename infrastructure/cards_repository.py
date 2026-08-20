from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from core.db import PaymentCardModel, engine
from core.datetime_utils import ensure_utc_datetime
from domain.cards import CardCreateCommand, CardRecord, CardSecret, CardUpdateCommand


def _to_record(model: PaymentCardModel) -> CardRecord:
    return CardRecord(
        id=int(model.id or 0),
        pan=model.pan,
        last4=model.last4,
        brand=model.brand,
        exp_month=model.exp_month,
        exp_year=model.exp_year,
        cvv=model.cvv,
        holder_name=model.holder_name,
        region=model.region,
        note=model.note,
        is_active=bool(model.is_active),
        success_count=model.success_count,
        fail_count=model.fail_count,
        created_at=ensure_utc_datetime(model.created_at),
        updated_at=ensure_utc_datetime(model.updated_at),
    )


class CardsRepository:
    def list(self) -> list[CardRecord]:
        with Session(engine) as session:
            items = session.exec(select(PaymentCardModel).order_by(PaymentCardModel.id)).all()
        return [_to_record(item) for item in items]

    def get(self, card_id: int) -> CardRecord | None:
        with Session(engine) as session:
            model = session.get(PaymentCardModel, card_id)
            if not model:
                return None
            return _to_record(model)

    def get_secret(self, card_id: int) -> CardSecret | None:
        """二期绑卡用：返回完整卡号与 CVV，不走 HTTP。"""
        with Session(engine) as session:
            model = session.get(PaymentCardModel, card_id)
            if not model:
                return None
            return CardSecret(
                id=int(model.id or 0),
                pan=model.pan,
                cvv=model.cvv,
                exp_month=model.exp_month,
                exp_year=model.exp_year,
                holder_name=model.holder_name,
            )

    def create(self, command: CardCreateCommand) -> CardRecord | None:
        with Session(engine) as session:
            existing = session.exec(
                select(PaymentCardModel).where(PaymentCardModel.pan == command.pan)
            ).first()
            if existing:
                return None
            model = PaymentCardModel(
                pan=command.pan,
                last4=command.pan[-4:],
                brand=_detect_brand(command.pan),
                exp_month=command.exp_month,
                exp_year=command.exp_year,
                cvv=command.cvv,
                holder_name=command.holder_name,
                region=command.region,
                note=command.note,
            )
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def find_by_pan(self, pan: str, *, exclude_id: int | None = None) -> CardRecord | None:
        with Session(engine) as session:
            query = select(PaymentCardModel).where(PaymentCardModel.pan == pan)
            if exclude_id is not None:
                query = query.where(PaymentCardModel.id != exclude_id)
            model = session.exec(query).first()
            return _to_record(model) if model else None

    def update(self, card_id: int, command: CardUpdateCommand) -> CardRecord | None:
        with Session(engine) as session:
            model = session.get(PaymentCardModel, card_id)
            if not model:
                return None
            if command.pan is not None:
                model.pan = command.pan
                model.last4 = command.pan[-4:]
                model.brand = _detect_brand(command.pan)
            if command.exp_month is not None:
                model.exp_month = command.exp_month
            if command.exp_year is not None:
                model.exp_year = command.exp_year
            if command.cvv is not None:
                model.cvv = command.cvv
            if command.holder_name is not None:
                model.holder_name = command.holder_name
            if command.region is not None:
                model.region = command.region
            if command.note is not None:
                model.note = command.note
            model.updated_at = datetime.now(timezone.utc)
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def delete(self, card_id: int) -> bool:
        with Session(engine) as session:
            model = session.get(PaymentCardModel, card_id)
            if not model:
                return False
            session.delete(model)
            session.commit()
            return True

    def toggle(self, card_id: int) -> bool | None:
        with Session(engine) as session:
            model = session.get(PaymentCardModel, card_id)
            if not model:
                return None
            model.is_active = not model.is_active
            model.updated_at = datetime.now(timezone.utc)
            session.add(model)
            session.commit()
            session.refresh(model)
            return bool(model.is_active)


def _detect_brand(pan: str) -> str:
    if pan.startswith("4"):
        return "visa"
    if pan.startswith("5"):
        return "mastercard"
    if pan.startswith("3"):
        return "amex"
    return "unknown"
