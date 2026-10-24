from __future__ import annotations

from scripts.run_live_smoke import build_sandbox_probe, parse_probe


def test_parse_probe_reads_marker_line() -> None:
    stdout = 'noise\nLIVE_SMOKE_RESULT={"status": 200, "shop_title": "ShopDemo"}\n'
    assert parse_probe(stdout) == {"status": 200, "shop_title": "ShopDemo"}


def test_parse_probe_rejects_missing_or_invalid_marker() -> None:
    assert parse_probe("no marker") is None
    assert parse_probe("LIVE_SMOKE_RESULT={not-json}") is None


def test_probe_targets_public_shop_and_records_screenshot() -> None:
    probe = build_sandbox_probe("https://example.test")
    assert "https://example.test/shop/" in probe
    assert "ShopDemo" in probe
    assert "/workspace/flowscout/live-smoke.png" in probe
