from fastapi.testclient import TestClient

from snowball.dashboard import create_app
from snowball.halt import halt_active, write_halt
from snowball.snapshot import build_snapshot
from snowball.state import AppState


def test_snapshot_json_shape(app_state: AppState) -> None:
    snap = build_snapshot(app_state)
    assert snap["status"]["mode"] == "paper"
    assert snap["status"]["paper"] is True
    assert snap["status"]["live_orders_permitted"] is False
    assert snap["status"]["halt_active"] is False
    assert snap["watcher"]["name"] == "The Watcher"
    assert snap["yolo_demon"]["name"] == "Yolo Demon"
    assert "DOES NOT TRADE" in snap["yolo_demon"]["label"]
    risk = snap["risk"]
    assert risk["bankroll_usd"] == 1000.0
    assert risk["equity_usd"] == 1000.0
    assert risk["daily_loss_kill_usd"] == 25.0
    assert risk["max_positions_per_pair"] == 5
    assert risk["max_position_notional_usd"] == 100.0
    assert risk["max_book_positions"] == 20
    products = [p["product"] for p in snap["pairs"]]
    assert products == ["BTC-USD", "SOL-USD", "ETH-USD", "DOGE-USD"]
    assert snap["status"]["strategies"] == ["sma_15m", "sma_5m"]
    for p in snap["pairs"]:
        assert p["max_open"] == 5
        assert p["open_count"] == 0
        assert "sma_fast_5m" in p
        assert "sma_slow_5m" in p
        assert "signal_5m" in p
        assert "sma_fast_15m" in p
        assert p["signal_15m"] == "hold"
        assert p["signal_5m"] == "hold"
    assert snap["positions"] == []
    assert "scorecard" in snap
    assert "by_strategy" in snap["scorecard"]
    assert "by_product" in snap["scorecard"]
    assert "pair_pauses" in snap


def test_dashboard_mounts_and_health(app_state: AppState) -> None:
    app = create_app(app_state)
    client = TestClient(app)
    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["ok"] is True
    page = client.get("/")
    assert page.status_code == 200
    assert b"Snowball" in page.content
    assert b"The Watcher" in page.content
    assert b"Yolo Demon" in page.content
    assert b"DOES NOT TRADE" in page.content
    js = client.get("/api/snapshot")
    assert js.status_code == 200
    body = js.json()
    assert body["status"]["paper"] is True
    assert body["risk"]["bankroll_usd"] == 1000.0
    assert {p["product"] for p in body["pairs"]} == {
        "BTC-USD",
        "SOL-USD",
        "ETH-USD",
        "DOGE-USD",
    }


def test_dashboard_halt_resume_paper_only(app_state: AppState) -> None:
    app = create_app(app_state)
    client = TestClient(app)
    r = client.post("/api/halt")
    assert r.status_code == 200
    assert halt_active(app_state.settings.halt_file)
    snap = client.get("/api/snapshot").json()
    assert snap["status"]["halt_active"] is True
    r = client.post("/api/resume")
    assert r.status_code == 200
    assert not halt_active(app_state.settings.halt_file)


def test_engine_respects_halt_and_pair_cap(app_state: AppState) -> None:
    from snowball.engine import Engine
    from snowball.models import PairSnapshot
    from tests.conftest import FakeMarket

    golden = [100.0] * 50 + [200.0]
    flat = [100.0] * 60
    market = FakeMarket(
        {
            "BTC-USD": golden,
            "SOL-USD": golden,
            "ETH-USD": flat,
            "DOGE-USD": flat,
        },
        last={"BTC-USD": 200.0, "SOL-USD": 200.0, "ETH-USD": 100.0, "DOGE-USD": 100.0},
    )
    app_state.settings = app_state.settings.model_copy(update={"strategies": "sma_15m"})
    engine = Engine(app_state, market)
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 1
    assert app_state.ledger.open_count("SOL-USD") == 1

    write_halt(app_state.settings.halt_file)
    # HALT emergency-flattens open lots, then blocks new entries.
    app_state.pairs["BTC-USD"] = PairSnapshot(
        product="BTC-USD", last=200.0, sma_fast=120.0, sma_slow=100.0, signal="enter", max_open=2
    )
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 0
    assert app_state.ledger.open_count("SOL-USD") == 0
    assert halt_active(app_state.settings.halt_file)
    # Still halted: golden cross must not re-enter.
    engine.tick()
    assert app_state.ledger.open_count("BTC-USD") == 0
