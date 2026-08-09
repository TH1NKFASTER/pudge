from anime_mpv.filename import fold_search_title, normalize_title, parse_anime_filename, title_similarity


def test_erai_filename():
    result = parse_anime_filename(
        "[Erai-raws] Youjo Senki II - 03 [1080p CR WEB-DL AVC AAC][MultiSub][6A71D0B4].mkv"
    )
    assert result.title == "Youjo Senki II"
    assert result.episode == 3


def test_season_episode_filename():
    result = parse_anime_filename("Anime.Name.S02E07.1080p.WEB-DL.mkv")
    assert result.title == "Anime Name"
    assert result.season == 2
    assert result.episode == 7


def test_title_similarity():
    assert title_similarity("Sousou no Frieren", "Sousou no Frieren - 03") > 85


def test_compound_subtitle_archive_suffix_is_removed_from_movie_title():
    result = parse_anime_filename(
        "Demon.Slayer.Kimetsu.no.Yaiba.Infinity.Castle.2025.1080p.BDRip.x265-Rapta.sup.7z"
    )
    assert not result.title.casefold().endswith("sup")
    assert result.episode is None


def test_search_title_folds_decorative_latin_unicode_without_damaging_japanese():
    assert fold_search_title("Otome Kaijuu Caraméliser") == "Otome Kaijuu Carameliser"
    assert normalize_title("Ōkami × Café Æther") == "okami cafe aether"
    assert fold_search_title("かがみ") == "かがみ"


def test_parse_bracket_only_episode_used_by_jimaku_bluray_releases():
    parsed = parse_anime_filename("[Nekomoe kissaten] ODDTAXI [02][BDRip].JPSC.ass")

    assert parsed.title == "ODDTAXI JPSC"
    assert parsed.episode == 2
