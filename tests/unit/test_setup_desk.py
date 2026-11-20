"""The engine asks the doorkeeper about a person's setup (chunk 10): who a
setup token belongs to, what a one-use grant said yes to, to send the daily
email's hello, and whether its link was opened. Each answer is believed
only in the exact shape the doorkeeper gives; anything else is nothing."""

from __future__ import annotations

import pytest

from tests.unit.test_door_presence import SECRET, _desk, _Doorkeeper, doorkeeper  # noqa: F401


def test_a_setup_token_names_its_person(doorkeeper):  # noqa: F811
    _Doorkeeper.replies["/setup/who"] = (200, {"person": "daniel-reyes"})
    assert _desk(doorkeeper).setup_person("tok") == "daniel-reyes"
    (sent,) = _Doorkeeper.seen
    assert sent["token"] == "tok" and sent["auth"] == f"Bearer {SECRET}"


def test_a_grant_names_the_person_and_the_step(doorkeeper):  # noqa: F811
    _Doorkeeper.replies["/setup/grant"] = (200, {"person": "daniel-reyes", "step": "mileage"})
    assert _desk(doorkeeper).spend_setup("g") == ("daniel-reyes", "mileage")


def test_the_hello_and_whether_it_was_opened(doorkeeper):  # noqa: F811
    _Doorkeeper.replies["/setup/hello"] = (200, {"sent": True})
    _Doorkeeper.replies["/setup/heard"] = (200, {"heard": True})
    assert _desk(doorkeeper).hello("daniel-reyes") is True
    assert _desk(doorkeeper).heard("daniel-reyes") is True
    assert [s["person"] for s in _Doorkeeper.seen] == ["daniel-reyes", "daniel-reyes"]


@pytest.mark.parametrize(
    ("path", "reply"),
    [
        ("/setup/who", (400, {"error": "unknown"})),
        ("/setup/who", (200, {"person": ""})),
        ("/setup/who", (200, {"person": ["daniel-reyes"]})),
        ("/setup/grant", (400, {"error": "unknown"})),
        ("/setup/grant", (200, {"person": "daniel-reyes"})),
        ("/setup/hello", (200, {"sent": "yes"})),
        ("/setup/heard", (500, b"oops")),
    ],
)
def test_anything_but_a_clean_answer_is_nothing(doorkeeper, path, reply):  # noqa: F811
    _Doorkeeper.replies[path] = reply
    desk = _desk(doorkeeper)
    asked = {
        "/setup/who": lambda: desk.setup_person("tok"),
        "/setup/grant": lambda: desk.spend_setup("g"),
        "/setup/hello": lambda: desk.hello("daniel-reyes"),
        "/setup/heard": lambda: desk.heard("daniel-reyes"),
    }[path]()
    assert asked in (None, False)


def test_an_unreachable_doorkeeper_is_nothing(tmp_path, doorkeeper):  # noqa: F811
    gone = _desk(doorkeeper, url="http://127.0.0.1:9")
    assert gone.setup_person("tok") is None
    assert gone.spend_setup("g") is None
    assert gone.hello("daniel-reyes") is False
    assert gone.heard("daniel-reyes") is False
