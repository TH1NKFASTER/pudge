"""Audiobook volumes of one series stack on the phone (invented titles).

An audiobook linked to a light-novel volume took that volume's store title for its series:
``作品 ～副題～ 2 (文庫)`` kept its imprint, so the volume number was not at the end and every
volume was a series of its own; a wave dash 〜 in one volume and a tilde ～ in the next were two
series too. The phone groups by the Mac's series title, so nothing stacked there. Books the Mac
prepares only on request now come with their cover, for the phone's series cards."""

from __future__ import annotations

from pathlib import Path

from test_fix_iphone_lazy_catalog import _book, service  # noqa: F401 - service is a fixture
from test_fix_rv5_ios import _assert_decodes

from pudge.audiobooks import _audiobook_series_title


def test_linked_volumes_with_an_imprint_and_either_tilde_are_one_series() -> None:
    titles = [("作品 〜副題〜 1 (文庫)", 1), ("作品 ～副題～ 2 (文庫)", 2), ("[03巻] 作品 ~副題~", 3)]
    assert {_audiobook_series_title(title, volume=volume) for title, volume in titles} == {"作品 ~副題~"}
    assert _audiobook_series_title("作品 (上)", volume=1) == "作品"
    assert _audiobook_series_title("別の作品 2", volume=2) == "別の作品"


def test_a_book_still_to_prepare_comes_with_its_cover(service, tmp_path, monkeypatch) -> None:  # noqa: F811
    _book(service, tmp_path, "one.flac")
    cover = {
        "resourceId": "c0ffee00-0000-4000-8000-000000000001",
        "sha256": "a" * 64,
        "sizeBytes": 10,
        "fileName": "cover.jpg",
    }
    seen: list[Path] = []
    monkeypatch.setattr(service, "_book_cover", lambda book_id, first: seen.append(Path(first)) or cover)
    service.refresh_catalog(lazy=True)
    (entry,) = service.catalog(lazy=True)["pending"]
    assert entry["cover"] == cover and seen and seen[0].name == "one.flac"
    assert service.prepared_calls == []  # still not prepared
    _assert_decodes("PendingBook", entry)
