"""The receipt page looks like Tim and says what happened (the 10/14 phone run).

The page wears the doorkeeper's style (a copy: the box's two programs share
no code across the sign-in boundary). On a catch-up link it shows each
file's name before anything is sent, since the photo picker once sent a
real photo by mistake; it says the box's own reason when a send fails,
where it said only "didn't go through"; it tells a person how to find a
folder in iCloud Drive; and a link the box no longer knows says the box may
have restarted, where it said "This isn't a link Tim made."
"""

from __future__ import annotations

import re

from core.tools.receipts import _GONE, page


def test_the_page_wears_tims_mark_colors_and_the_ministrys_name():
    for html in (page("n", ministry="Grace"), page("n", many=True, ministry="Grace")):
        assert '<svg class="mark"' in html and "#16233F" in html and "#D3A24B" in html
        assert 'class="who">for Grace<' in html
        assert "prefers-color-scheme:dark" in html
        assert not re.search(r"(src|href)=[\"']?https?:", html)
        assert len(html.encode()) < 12_000


def test_a_pile_shows_each_files_name_before_it_sends():
    html = page("n", many=True)
    assert 'id="files"' in html and 'id="send"' in html
    assert "f.name" in html  # each chosen file is listed by name
    assert "Send these" in html


def test_one_receipt_still_sends_the_moment_the_photo_is_taken():
    html = page("n")
    assert 'capture="environment"' in html and 'id="send"' not in html


def test_a_failed_send_says_the_boxs_own_reason():
    html = page("n", many=True)
    assert "reasons" in html and "d.say" in html


def test_the_pile_page_says_how_to_find_a_folder_in_icloud():
    assert "search for the folder" in page("n", many=True)


def test_a_link_the_box_no_longer_knows_says_it_may_have_restarted():
    assert "restarted" in _GONE["unknown"] and "new one" in _GONE["unknown"]
    assert "isn't a link Tim made" not in _GONE["unknown"]
