"""First-run setup guide: AniList credentials in two steps, built-in downloads by default,
qBittorrent below 5.2 with a password (behaviour; audit 71Б rewrite)."""

from pathlib import Path


from pudge.config import load_config
from pudge.providers.qbittorrent import QBittorrentClient
from source_policy import source
from tests.i18n_support import en, ru

ROOT = Path(__file__).resolve().parents[1]

# The setup guide opens on a first start; mpv and ffmpeg are installed.
FIRST_START = {
    "state": {"settings": {"onboarding_completed": False}},
    "api": {
        "first_experience_dependencies": {
            "mpv": {"installed": True},
            "ffmpeg": {"installed": True},
            "manga_ocr": {"installed": False},
            "jiten_mpv": {"installed": False},
            "jpdb_mpv": {"installed": False},
        }
    },
    "before": "__api.complete_onboarding = async draft => ({settings: {...draft, onboarding_completed: true, light_novels: {}}});",
}
GUIDE = """
const next = async () => { await __click('#onboardingNext'); await __tick(20); };
const toStep = async wanted => { while (ui.onboardingStep < wanted) await next(); };
const anilist = () => ({
  clientStep: __visible('#o_anilist_client_step'), tokenStep: __visible('#o_anilist_token_step'),
  tokenEnabled: !$('o_anilist_token').disabled, canContinue: !document.querySelector('#o_anilist_flow [data-anilist-continue]').disabled});
"""


def test_anilist_key_field_opens_only_after_a_client_id(page) -> None:
    assert en("ui.getTheKeyAndPaste") == "Get the key and paste it into the new field"
    assert ru("ui.getTheKeyAndPaste") == "Получите ключ и вставьте его в появившееся поле"
    for lang in ("en", "ru"):
        result = page.run(
            GUIDE
            + """
            await toStep(3);
            await __input('#o_anilist_enabled', true);
            const out = {fields: __visible('#o_anilist_fields'), empty: anilist()};
            await __input('#o_anilist_client', '12345');
            out.typed = anilist();
            await __click('#o_anilist_flow [data-anilist-continue]');
            out.continued = anilist();
            out.focused = document.activeElement?.id;
            out.tokenType = $('o_anilist_token').type;
            await next();                                     // no key yet: the guide waits for it
            out.waiting = {step: ui.onboardingStep, toast: __text('#toast'), expected: PudgeI18n.t('ui.getTheKeyAndPaste')};
            await __click('#o_anilist_flow [data-anilist-copy]');
            out.copied = __clipboard.slice();
            await __click('#o_anilist_flow [data-anilist-back]');
            out.back = anilist();
            await __click('#o_anilist_flow [data-anilist-continue]');
            await __input('#o_anilist_token', 'anilist-key');
            await toStep(5);
            await next();
            return out;
            """,
            lang=lang,
            **FIRST_START,
        )
        out = result.value
        assert out["fields"] is True
        assert out["empty"]["tokenEnabled"] is False and out["empty"]["canContinue"] is False
        assert out["typed"]["tokenEnabled"] is False and out["typed"]["canContinue"] is True
        assert out["continued"]["tokenEnabled"] is True and out["focused"] == "o_anilist_token"
        assert out["tokenType"] == "password"
        assert out["waiting"]["step"] == 3 and out["waiting"]["toast"].endswith(out["waiting"]["expected"])
        assert out["copied"] == ["https://anilist.co/api/v2/oauth/pin"]
        assert out["back"]["tokenEnabled"] is False
        [[draft]] = result.calls_of("complete_onboarding")
        assert (draft["anilist_enabled"], draft["anilist_client_id"], draft["anilist_token"]) == (
            True,
            "12345",
            "anilist-key",
        )


def test_anilist_flow_shows_one_step_at_a_time(page) -> None:
    # The guide says "After it is accepted, Pudge will show the link and field for the key";
    # syncAniListCredentialFlow() toggles the class `hidden` on the steps, which no CSS rule styles.
    result = page.run(
        GUIDE
        + """
        await toStep(3);
        await __input('#o_anilist_enabled', true);
        const first = anilist();
        await __input('#o_anilist_client', '12345');
        await __click('#o_anilist_flow [data-anilist-continue]');
        return {first, second: anilist()};
        """,
        **FIRST_START,
    )
    assert result.value["first"]["clientStep"] is True and result.value["first"]["tokenStep"] is False
    assert result.value["second"]["clientStep"] is False and result.value["second"]["tokenStep"] is True


DOWNLOADS_STEP = (
    GUIDE
    + """
await __input('#o_profile', PROFILE);
await toStep(4);
const out = {text: __text('#onboardingBody'), qbtText: t('onboarding.qbtText'), help: t('help.qbt'),
             useQbt: PudgeI18n.t('ui.useQbittorrentInsteadAdvanced'),
             qbtChecked: $('o_qbt_enabled').checked, qbtFields: __visible('#o_qbt_fields')};
if (USE_QBT) {
  await __input('#o_qbt_enabled', true);
  out.qbtFieldsAfter = __visible('#o_qbt_fields');
  out.helpShown = __visible('#o_qbt_fields p.subtle') && __text('#o_qbt_fields p.subtle');
  out.fields = ['o_qbt_url', 'o_qbt_user', 'o_qbt_password', 'o_qbt_api'].map(id => $(id)?.type);
}
await toStep(5);
await next();
return out;
"""
)


def _downloads_step(page, lang: str, profile: str, use_qbt: bool):
    code = DOWNLOADS_STEP.replace("PROFILE", repr(profile)).replace("USE_QBT", "true" if use_qbt else "false")
    result = page.run(code, lang=lang, **FIRST_START)
    [[draft]] = result.calls_of("complete_onboarding")
    return result.value, draft


def test_first_experience_uses_managed_downloads_by_default(page) -> None:
    assert en("onboarding.qbtText") == "Automatic downloads work through Pudge with no torrent-client setup."
    assert en("ui.useQbittorrentInsteadAdvanced") == "Use qBittorrent instead (advanced)"
    for lang in ("en", "ru"):
        out, draft = _downloads_step(page, lang, "full", use_qbt=False)
        assert out["qbtText"] in out["text"] and out["useQbt"] in out["text"]
        assert out["qbtChecked"] is False and out["qbtFields"] is False
        assert (draft["aria2_enabled"], draft["qbt_enabled"]) == (True, False)
    # Without full automation there is no downloader at all.
    _out, draft = _downloads_step(page, "en", "subtitles", use_qbt=False)
    assert draft["aria2_enabled"] is False
    # A config written from the example uses the built-in downloader too.
    assert load_config(ROOT / "pudge" / "config.example.toml").aria2.enabled is True


def test_qbittorrent_below_52_uses_password_instead_of_api_key(page) -> None:
    assert "Versions below 5.2 use username and password" in en("help.qbt")
    assert "ниже 5.2 используйте логин и пароль" in ru("help.qbt")
    for lang in ("en", "ru"):
        out, draft = _downloads_step(page, lang, "full", use_qbt=True)
        assert out["qbtFieldsAfter"] is True
        assert out["helpShown"] == out["help"]
        assert out["fields"] == ["text", "text", "password", "password"]
        # qBittorrent replaces the built-in downloader.
        assert (draft["aria2_enabled"], draft["qbt_enabled"]) == (False, True)
    # policy: the README is documentation for people; there is no behaviour to run.
    assert "API-key authentication requires qBittorrent 5.2 or newer" in " ".join(source("README.md").split())


def test_qbittorrent_add_fields_follow_installed_version(monkeypatch) -> None:
    legacy = QBittorrentClient("http://qbt.local", username="admin", password="secret")
    modern = QBittorrentClient("http://qbt.local", api_key="secret")
    monkeypatch.setattr(legacy, "version", lambda: "v5.1.4")

    try:
        assert legacy._add_state_payload(paused=True, stop_at_metadata=False) == {"paused": "true"}
        assert modern._add_state_payload(paused=True, stop_at_metadata=False) == {
            "stopped": "true",
            "contentLayout": "Original",
        }
    finally:
        legacy.close()
        modern.close()
