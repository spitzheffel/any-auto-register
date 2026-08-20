"""Payment card pool endpoint tests."""
from __future__ import annotations


VALID_CARD = {
    "pan": "4111111111111111",
    "exp_month": 12,
    "exp_year": 2030,
    "cvv": "123",
    "holder_name": "Test User",
    "region": "US",
    "note": "unit",
}


def test_list_cards_empty(client):
    resp = client.get("/api/cards")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 0


def test_add_card_masks_secrets(client):
    resp = client.post("/api/cards", json=VALID_CARD)
    assert resp.status_code == 200
    data = resp.json()
    assert data["masked_pan"] == "**** **** **** 1111"
    assert data["last4"] == "1111"
    assert data["brand"] == "visa"
    assert data["region"] == "US"
    assert data["pan"] == "4111111111111111"
    assert "cvv" not in data


def test_add_and_list_card_omits_secrets(client):
    client.post("/api/cards", json=VALID_CARD)
    resp = client.get("/api/cards")
    data = resp.json()
    assert len(data) == 1
    assert data[0]["pan"] == "4111111111111111"
    assert "cvv" not in data[0]
    assert data[0]["masked_pan"].endswith("1111")


def test_add_invalid_luhn_card(client):
    resp = client.post(
        "/api/cards",
        json={**VALID_CARD, "pan": "4111111111111112"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "卡号不合法"


def test_add_duplicate_card(client):
    first = client.post("/api/cards", json=VALID_CARD)
    assert first.status_code == 200
    second = client.post("/api/cards", json={**VALID_CARD, "cvv": "999"})
    assert second.status_code == 400
    assert second.json()["detail"] == "支付卡已存在"


def test_toggle_card(client):
    create_resp = client.post("/api/cards", json=VALID_CARD)
    card_id = create_resp.json()["id"]
    toggle_resp = client.patch(f"/api/cards/{card_id}/toggle")
    assert toggle_resp.status_code == 200
    assert toggle_resp.json()["is_active"] is False
    listed = client.get("/api/cards").json()
    assert listed[0]["is_active"] is False


def test_delete_card(client):
    create_resp = client.post("/api/cards", json=VALID_CARD)
    card_id = create_resp.json()["id"]
    del_resp = client.delete(f"/api/cards/{card_id}")
    assert del_resp.status_code == 200
    list_resp = client.get("/api/cards")
    assert len(list_resp.json()) == 0


def test_get_card_reveals_secrets(client):
    create_resp = client.post("/api/cards", json=VALID_CARD)
    card_id = create_resp.json()["id"]
    resp = client.get(f"/api/cards/{card_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pan"] == "4111111111111111"
    assert data["cvv"] == "123"
    listed = client.get("/api/cards").json()[0]
    assert listed["pan"] == "4111111111111111"
    assert "cvv" not in listed


def test_update_card_pan_and_cvv(client):
    create_resp = client.post("/api/cards", json=VALID_CARD)
    card_id = create_resp.json()["id"]
    resp = client.patch(
        f"/api/cards/{card_id}",
        json={"pan": "5555555555554444", "cvv": "999", "holder_name": "New Name"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["last4"] == "4444"
    assert data["brand"] == "mastercard"
    assert data["pan"] == "5555555555554444"
    assert "cvv" not in data
    detail = client.get(f"/api/cards/{card_id}").json()
    assert detail["pan"] == "5555555555554444"
    assert detail["cvv"] == "999"
    assert detail["holder_name"] == "New Name"
