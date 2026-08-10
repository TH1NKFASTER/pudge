from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers.nyaa import NyaaError, search_ranked


def _anime() -> LibraryAnime:
    return LibraryAnime(
        media_id=135865,
        title="Youjo Senki II",
        titles=["Youjo Senki II", "Saga of Tanya the Evil Season 2", "幼女戦記Ⅱ"],
        synonyms=["Saga of Tanya the Evil II", "Youjo Senki 2"],
        episodes=12,
        format="TV",
    )


def _release() -> NyaaRelease:
    return NyaaRelease(
        title="[Erai-raws] Youjo Senki II - 05 [1080p CR WEB-DL AVC AAC][MultiSub]",
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash="abc123",
        size_text="1.4 GiB",
        size_bytes=int(1.4 * 1024**3),
        seeders=25,
        leechers=2,
        downloads=100,
        trusted=True,
        remake=False,
        group="Erai-raws",
    )


def _search(client):
    return search_ranked(
        client,
        _anime(),
        episode=5,
        batch=False,
        trusted_groups=["Erai-raws"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=100 * 1024**2,
        target_episode_max_bytes=4 * 1024**3,
    )


def test_canonical_title_is_searched_first_and_timeout_does_not_abort():
    class Client:
        def __init__(self):
            self.queries = []

        def search(self, query):
            self.queries.append(query)
            if len(self.queries) == 1:
                raise NyaaError("504 Gateway Timeout")
            if query == "Youjo Senki II 5":
                return [_release()]
            return []

    client = Client()
    releases = _search(client)

    assert client.queries[:2] == ["Youjo Senki II 05", "Youjo Senki II 5"]
    assert releases and releases[0].info_hash == "abc123"


def test_search_tries_multiple_aliases_before_reporting_nyaa_failure():
    class Client:
        def __init__(self):
            self.queries = []

        def search(self, query):
            self.queries.append(query)
            raise NyaaError("504 Gateway Timeout")

    client = Client()
    try:
        _search(client)
    except NyaaError as exc:
        message = str(exc)
    else:
        raise AssertionError("NyaaError was expected")

    assert len(client.queries) > 2
    assert client.queries[0] == "Youjo Senki II 05"
    assert "Saga of Tanya the Evil Season 2" in message
