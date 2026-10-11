"""Light novels: 第N部 ("part N") is read as the volume, like 巻 (behaviour).

Volume parsing knew only 巻, so 物語 第二部 got no volume and became a series of its own.
第N部 counts with ASCII, full-width and kanji numerals, unless a volume number follows it
(第2部 3 is volume 3 of part 2; 第三部「領主の養女Ⅲ」 keeps its own numbering). Words such
as 一部 are left alone, and the existing cases (2020 not cut, 上/中/下 …) stay as they were.
"""

from __future__ import annotations

import pytest
from test_patch_a_accounting_gate_library import _epub, _ln

from pudge.library_overrides import kanji_volume_digits
from pudge.light_novels import _series_title, _volume_from_text
from pudge.manga import _manga_series_title


@pytest.mark.parametrize(
    ("title", "volume", "series"),
    [
        ("物語 第2部", 2, "物語"),
        ("物語 第２部", 2, "物語"),
        ("物語 第二部", 2, "物語"),
        ("物語 第十二部", 12, "物語"),
        ("物語 第 3 部 (MF文庫J)", 3, "物語"),
        ("物語 第2部 3", 3, "物語 第2部"),
        ("物語 第二部 3巻", 3, "物語 第二部"),
        ("物語 第3部 2巻", 2, "物語 第3部"),
        ("本好きの下剋上 第三部「領主の養女Ⅲ」", None, "本好きの下剋上 第三部「領主の養女III」"),
        ("一部の物語", None, "一部の物語"),
        # Unchanged cases.
        ("俺の妹2020", None, "俺の妹2020"),
        ("タイトル 第十二巻", 12, "タイトル"),
        ("タイトル（上）", 1, "タイトル"),
        ("タイトル 中", 2, "タイトル"),
        ("86", None, "86"),
    ],
)
def test_part_numbers_are_volumes(title, volume, series) -> None:
    assert _volume_from_text(title) == volume
    assert _series_title(title) == series


def test_kanji_parts_become_digits_only_after_dai_and_only_when_asked() -> None:
    assert kanji_volume_digits("物語 第三部", parts=True) == "物語 第3部"
    assert kanji_volume_digits("一部の物語", parts=True) == "一部の物語"
    # Manga titles keep their 部 as written.
    assert kanji_volume_digits("物語 第三部") == "物語 第三部"
    assert _manga_series_title("物語 第三部") == "物語 第三部"


def test_imported_parts_are_volumes_of_one_series(tmp_path) -> None:
    service = _ln(tmp_path)
    folder = tmp_path / "import"
    first = service.import_file(_epub(folder / "a.epub", "異世界の物語 第一部"))
    second = service.import_file(_epub(folder / "b.epub", "異世界の物語 第２部"))
    books = {int(book["id"]): book for book in service.books()}
    assert (books[int(first["id"])]["volume"], books[int(second["id"])]["volume"]) == (1, 2)
    assert books[int(first["id"])]["series_key"] == books[int(second["id"])]["series_key"]
