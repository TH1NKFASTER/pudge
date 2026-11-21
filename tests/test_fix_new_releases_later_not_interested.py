"""The "New releases" popup: Later vs Not interested (review SCEN-8).

"Later" used to dismiss the offer for good (a tombstone that is never cleared, the entry
"ignored"), so the reminder the button promises never came. Now "Later" only closes the
popup: the offer stays and comes back the next time Pudge opens. Each row has its own
"Not interested", which is what drops the offer. Pages run in the node harness."""

from __future__ import annotations

from pudge.manager_models import LibraryAnime
from test_web_app import make_api
from tests.i18n_support import en, ru

OFFERS = r"""
__api.poll_planned_release_discovery = async () => ({offers: [{media_id: 3, title: 'Synthetic release'},
                                                               {media_id: 4, title: 'Other release'}]});
"""

POPUP = r"""
const popup = () => ({open: $('plannedReleaseBackdrop').classList.contains('open') && __visible('#plannedReleaseBackdrop'),
                      titles: [...document.querySelectorAll('#plannedReleaseList strong')].map(n => n.innerText),
                      buttons: [...document.querySelectorAll('#plannedReleaseList button')].map(n => n.innerText)});
"""


def test_later_closes_without_dismissing_and_the_offers_come_back_next_launch(page) -> None:
    for lang in ("en", "ru"):
        first = page.run(
            POPUP
            + r"""
            const out = {shown: popup(), later: __text('#plannedReleaseClose')};
            await __click('#plannedReleaseClose'); await __flush();
            out.closed = popup();
            await __tick(60000); await __flush();                // same offers: not shown again now
            out.nextPoll = popup();
            out.expected = {later: PudgeI18n.t('ui.later'), watching: PudgeI18n.t('ui.watching'),
                            notInterested: PudgeI18n.t('ui.notInterested')};
            return out;
            """,
            lang=lang,
            before=OFFERS,
        )
        out = first.value
        expected = out["expected"]
        assert out["shown"]["open"] is True
        assert out["shown"]["titles"] == ["Synthetic release", "Other release"]
        assert out["shown"]["buttons"] == [expected["watching"], expected["notInterested"]] * 2
        assert out["later"] == expected["later"]
        assert out["closed"]["open"] is False and out["nextPoll"]["open"] is False
        assert first.calls_of("dismiss_planned_release_offers") == []
        # The next start of Pudge: the same offers are there again.
        again = page.run(POPUP + "return popup();", lang=lang, before=OFFERS)
        assert again.value["open"] is True and again.value["titles"] == ["Synthetic release", "Other release"]
    assert (en("ui.notInterested"), ru("ui.notInterested")) == ("Not interested", "Не интересно")


def test_not_interested_drops_only_its_row(page) -> None:
    result = page.run(
        POPUP
        + r"""
        await __click('[data-planned-release-dismiss="3"]'); await __flush();
        const out = {afterOne: popup()};
        await __click('[data-planned-release-dismiss="4"]'); await __flush();
        out.afterTwo = popup();
        return out;
        """,
        before=OFFERS + "__api.dismiss_planned_release_offers = async () => ({ok: true, dismissed: 1});",
    )
    out = result.value
    assert out["afterOne"]["open"] is True and out["afterOne"]["titles"] == ["Other release"]
    assert out["afterTwo"]["open"] is False
    assert result.calls_of("dismiss_planned_release_offers") == [[[3]], [[4]]]


def test_a_found_offer_stays_until_not_interested(tmp_path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(LibraryAnime(3, "Synthetic 3", status="PLANNING"))
    discovery = api.manager.planned_release_discovery()
    discovery._save(3, {"phase": "found", "found_at": 100})
    # Later makes no backend call: the offer is still there for the next start.
    assert [row["media_id"] for row in make_api(tmp_path).poll_planned_release_discovery()["offers"]] == [3]
    assert api.dismiss_planned_release_offers([3]) == {"ok": True, "dismissed": 1}
    assert make_api(tmp_path).poll_planned_release_discovery()["offers"] == []
