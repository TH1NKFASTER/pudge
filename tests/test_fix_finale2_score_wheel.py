"""FINALE2: the score wheel only at the end of an anime, no explanation line, a longer first spin.

- The phone was asked to rate an airing title it had caught up with ("latest"): only the final
  episode (a movie / one-episode title) asks now, as on the Mac.
- "The wheel will choose a score…" is gone on the Mac window (and the phone).
- The Mac's first spin of each wheel display runs a second longer (Spin again keeps 1 s), and the
  next-entry lookup finishing mid-spin no longer replaces the wheel (it jumped to the end: the
  spin looked instant).
Invented titles and ids only.
"""

from __future__ import annotations

from pudge.config import AppConfig
from pudge.database import Database
from pudge.iphone_finale import finale
from tests.i18n_support import has_text


def _db(tmp_path) -> Database:
    cfg = AppConfig()
    db = Database(tmp_path / "library.sqlite3")
    cfg.library.database_path = tmp_path / "library.sqlite3"
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,episodes,media_status,next_airing_episode,updated_at)"
            " VALUES(911,'Invented Airing Known','CURRENT',5,12,'RELEASING',7,0)"
        )
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,media_status,next_airing_episode,updated_at)"
            " VALUES(912,'Invented Airing Open','CURRENT',5,'RELEASING',7,0)"
        )
    return db


def test_caught_up_with_an_airing_title_asks_nothing(tmp_path) -> None:
    db = _db(tmp_path)
    # The latest aired episode (6 aired, 7 next) of a title without a score: no finale.
    assert finale(db, 911, 6) is None
    assert finale(db, 912, 6) is None
    # Its final episode still asks.
    final = finale(db, 911, 12)
    assert final is not None and final["kind"] == "final" and final["episode"] == 12


def test_the_mac_window_has_no_wheel_explanation_line(page) -> None:
    assert not has_text("The wheel will choose a score")
    assert not has_text("Колесо выберет оценку", "ru")
    result = page.run(
        r"""
        showScoreModal({media_id: 7, title: 'Show'}, true, 12);
        return [...document.querySelectorAll('#modalBody .modal-copy')].map(p => p.textContent);
        """,
        api={"final_sequel_option": {"sequel": None}},
    )
    assert result.value == []


def test_the_first_spin_runs_a_second_longer_and_survives_the_next_entry_render(page) -> None:
    result = page.run(
        r"""
        let answer;
        __api.final_sequel_option = () => new Promise(resolve => { answer = resolve; });
        showScoreModal({media_id: 7, title: 'Show'}, true, 12);
        await __tick(40);
        const c = ui.ratingContext;
        const wheel = $('scoreWheel');
        const first = wheel.style.transitionDuration;
        // The next entry arrives mid-spin: the same wheel element keeps spinning.
        answer({sequel: null});
        await __flush();
        await __tick(10);
        const kept = $('scoreWheel') === wheel;
        await __tick(1400);
        const midway = {spinning: c.spinning, score: c.generatedScore};
        await __tick(700);
        const done = {spinning: c.spinning, score: c.generatedScore};
        // Spin again: the usual second.
        c.spinning = false;
        spinScoreWheel(c);
        await __tick(40);
        const again = $('scoreWheel').style.transitionDuration;
        await __tick(1000);
        return {first, kept, midway, done, again, againDone: !c.spinning && c.generatedScore != null};
        """
    )
    out = result.value
    assert out["first"] == "2000ms"
    assert out["kept"] is True
    assert out["midway"] == {"spinning": True, "score": None}
    assert out["done"]["spinning"] is False and 1 <= out["done"]["score"] <= 10
    assert out["again"] == "1000ms" and out["againDone"] is True
