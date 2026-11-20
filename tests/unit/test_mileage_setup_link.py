"""The tracker app set up from one tap (core/mileage/setup_link.py, chunk 10).

Traccar Client 10.2 (one Flutter codebase for iPhone and Android, read
2026-10-22): a tap on ``org.traccar.client://?url=..&id=..`` asks the person
to confirm, then takes ``url`` as the server; a QR scanned from the app's
own settings takes ``https://<box>/track?id=..`` with the origin and path as
the server. Both carry the same settings, under the app's own names."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from core.mileage.setup_link import (
    START_LINK,
    STORES,
    TRACKER_SETTINGS,
    app_link,
    qr_rows,
    qr_text,
)

ORIGIN = "https://practice.asktimothy.org"


def _query(link: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}


def test_the_tap_link_sets_the_server_the_id_and_the_settings():
    link = app_link(ORIGIN, "tok-123")
    parts = urlsplit(link)
    assert parts.scheme == "org.traccar.client"
    assert parts.netloc == ""  # "action" is the app's own start/stop host
    q = _query(link)
    assert q["url"] == ORIGIN + "/track"
    assert q["id"] == "tok-123"
    assert {k: q[k] for k in TRACKER_SETTINGS} == TRACKER_SETTINGS


def test_the_qr_holds_the_box_with_the_same_settings():
    text = qr_text(ORIGIN, "tok-123")
    parts = urlsplit(text)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == ORIGIN + "/track"
    q = _query(text)
    assert q["id"] == "tok-123"
    assert "url" not in q  # the app takes the origin and path as the server
    assert {k: q[k] for k in TRACKER_SETTINGS} == TRACKER_SETTINGS


def test_the_settings_are_the_apps_own_names_and_values():
    # lib/configuration_service.dart: accuracy is one of highest/high/medium/low;
    # distance, interval and angle are whole numbers; the rest true or false.
    assert TRACKER_SETTINGS["accuracy"] == "highest"
    assert TRACKER_SETTINGS["buffer"] == "true"
    assert TRACKER_SETTINGS["stop_detection"] == "true"
    for key in ("distance", "interval", "angle"):
        assert TRACKER_SETTINGS[key].isdigit()


def test_turning_it_on_is_the_apps_start_action():
    assert START_LINK == "org.traccar.client://action/start"


def test_the_stores_are_the_real_app():
    assert STORES["iphone"].endswith("/id843156974")
    assert STORES["android"].endswith("?id=org.traccar.client")


def test_the_qr_is_a_square_of_ones_and_zeros():
    rows = qr_rows(qr_text(ORIGIN, "x" * 32))
    assert len(rows) >= 21 and all(len(r) == len(rows) for r in rows)
    assert all(set(r) <= {"0", "1"} for r in rows)
    assert rows[0].startswith("1111111")  # a finder pattern in the corner
