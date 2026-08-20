from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from application.cards import CardsService
from domain.cards import CardCreateCommand, CardUpdateCommand

router = APIRouter(prefix="/cards", tags=["cards"])
service = CardsService()


class CardCreateRequest(BaseModel):
    pan: str
    exp_month: int
    exp_year: int
    cvv: str
    holder_name: str = ""
    region: str = ""
    note: str = ""


class CardUpdateRequest(BaseModel):
    pan: str | None = None
    exp_month: int | None = None
    exp_year: int | None = None
    cvv: str | None = None
    holder_name: str | None = None
    region: str | None = None
    note: str | None = None


@router.get("")
def list_cards():
    return service.list_cards()


@router.get("/{card_id}")
def get_card(card_id: int):
    item = service.get_card(card_id)
    if not item:
        raise HTTPException(404, "支付卡不存在")
    return item


@router.post("")
def create_card(body: CardCreateRequest):
    try:
        return service.create_card(
            CardCreateCommand(
                pan=body.pan,
                exp_month=body.exp_month,
                exp_year=body.exp_year,
                cvv=body.cvv,
                holder_name=body.holder_name,
                region=body.region,
                note=body.note,
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/{card_id}")
def update_card(card_id: int, body: CardUpdateRequest):
    try:
        item = service.update_card(card_id, CardUpdateCommand(**body.model_dump()))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not item:
        raise HTTPException(404, "支付卡不存在")
    return item


@router.delete("/{card_id}")
def delete_card(card_id: int):
    result = service.delete_card(card_id)
    if not result["ok"]:
        raise HTTPException(404, "支付卡不存在")
    return result


@router.patch("/{card_id}/toggle")
def toggle_card(card_id: int):
    result = service.toggle_card(card_id)
    if not result:
        raise HTTPException(404, "支付卡不存在")
    return result
