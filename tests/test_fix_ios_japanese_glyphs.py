"""iPhone/iPad: kanji in their Japanese forms, not Chinese (Han unification).

The word card (and every other screen) drew 直 骨 角 化 次 in Chinese forms: iOS picks the
glyphs of an ideograph by the text's language, and without one SF falls back to PingFang (the
Chinese font) on a device whose languages do not include Japanese. JapaneseType
(ios/Pudge/UI/JapaneseText.swift) is the one helper: SwiftUI text is typeset as Japanese from
the app's root (`.japaneseTypesetting()`, iOS 17's typesettingLanguage), the large words use
Hiragino Sans at Dynamic Type sizes, inline navigation titles that may be Japanese are a
Japanese-typeset Text, and the CoreText strings (reader, subtitles, furigana) carry
kCTLanguageAttributeName "ja" and never fall back to the plain system font.
The Swift app builds only in Xcode on the Mac, so these are source rules.
"""

from __future__ import annotations

import re

from source_policy import source, sources

TEXT = "ios/Pudge/UI/JapaneseText.swift"
APP = "ios/Pudge/App/PudgeApp.swift"
STUDY = "ios/Pudge/UI/StudyViews.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def _helper() -> str:
    return _between(source(TEXT), "enum JapaneseType {", "\nextension View {")


def test_helper_tags_japanese_for_swiftui_and_coretext():
    text = source(TEXT)
    helper = _helper()
    assert 'static let languageCode = "ja"' in helper
    assert "Locale.Language(identifier: languageCode)" in helper
    assert "kCTLanguageAttributeName" in helper
    views = _between(text, "\nextension View {", "\n}\n")
    assert "func japaneseTypesetting() -> some View { typesettingLanguage(JapaneseType.language) }" in views
    title = _between(views, "func japaneseNavigationTitle", "\n    }\n")
    assert "ToolbarItem(placement: .principal)" in title and ".japaneseTypesetting()" in title
    assert "navigationTitle(title)" in title  # the back button and VoiceOver keep it


def test_dynamic_type_size_is_the_styles_default_size():
    # Font.custom(size:relativeTo:) scales `size` itself: the current (already scaled) size would
    # be scaled twice at large text sizes.
    helper = _helper()
    styled = _between(helper, "static func font(_ style: Font.TextStyle", "\n    }\n")
    assert "UITraitCollection(preferredContentSizeCategory: .large)" in styled
    assert "relativeTo: style" in styled
    assert "HiraginoSans-W3" in styled and "HiraginoSans-W6" in styled


def test_app_root_typesets_japanese_for_every_screen_sheet_and_cover():
    app = source(APP)
    group = _between(app, "WindowGroup {", "\n    }\n}")
    # Outside the root view: sheets and covers attached anywhere inside inherit it.
    assert re.search(
        r"Group \{.*PudgeRoot\(model: model\).*\}\s*(//[^\n]*\n\s*)*\.japaneseTypesetting\(\)", group, re.S
    )


def test_coretext_strings_and_furigana_are_japanese():
    text = source(TEXT)
    attributed = _between(
        text, "private func attributed(outline: Bool = false) -> NSAttributedString {", "\n    }\n"
    )
    assert "JapaneseType.tag(result)" in attributed
    ruby = _between(attributed, "let attributes: [String: Any] = [", "]")
    assert "kCTLanguageAttributeName as String: JapaneseType.languageCode" in ruby


def test_reader_font_never_falls_back_to_the_plain_system_font():
    text = source(TEXT)
    ui_font = _between(text, "func uiFont(_ size: CGFloat, bold: Bool = false) -> UIFont {", "\n    }\n")
    assert "UIFont.systemFont" not in ui_font
    assert "JapaneseType.uiFont(names, size: size)" in ui_font
    fallback = _between(
        _helper(), "static func uiFont(_ names: [String], size: CGFloat) -> UIFont {", "\n    }\n"
    )
    assert fallback.index('UIFont(name: "HiraginoSans-W3", size: size)') < fallback.index("UIFont.systemFont")
    # Elsewhere in the app no Japanese text is drawn with the plain system UIFont.
    for path, body in sources("ios/Pudge/**/*.swift").items():
        if path == TEXT:
            body = body.replace(_helper(), "")
        assert "UIFont.systemFont" not in body, path


def test_subtitles_keep_the_themes_font_family_with_a_japanese_fallback():
    text = source(TEXT)
    ui_font = _between(text, "func uiFont(_ size: CGFloat, bold: Bool = false) -> UIFont {", "\n    }\n")
    assert 'font == "subtitle", let family = subtitleLook?.fontFamily' in ui_font
    family = _between(_helper(), "static func uiFont(family: String", "\n    }\n")
    assert "UIFont.familyNames" in family and "return nil" in family  # not on this device: Hiragino


def test_word_card_and_review_card_use_japanese_fonts():
    study = source(STUDY)
    card = _between(study, "struct WordDetailView: View {", "\n    private func load(")
    assert ".font(JapaneseType.font(.largeTitle, bold: true))" in card
    assert "Text(reading).font(JapaneseType.font(.title3))" in card
    assert ".japaneseTypesetting()" in card
    lookup = _between(study, "struct TextLookupSheet: View {", "private func explainAction")
    assert ".japaneseTypesetting()" in lookup
    panel = _between(study, "struct BottomPanel<Content: View>: View {", "private var header: some View")
    assert ".japaneseTypesetting()" in panel
    watch = source(WATCH)
    assert "Text(card.word).font(JapaneseType.font(size: 46, bold: true))" in watch
    assert "Text(cue.text).font(JapaneseType.font(size: subtitleFontSize, bold: true))" in watch
    assert "Text(card.word).font(.system(" not in watch and "Text(cue.text).font(.system(" not in watch


def test_titles_that_may_be_japanese_are_japanese_typeset():
    # UIKit draws a navigation bar's own title without the SwiftUI typesetting language.
    allowed = {"MangaSpreads.title("}  # page numbers
    for path, body in sources("ios/Pudge/**/*.swift").items():
        for match in re.finditer(r"\.navigationTitle\(([^)\n]*)", body):
            argument = match.group(1).strip()
            if argument.startswith('"') or any(argument.startswith(a) for a in allowed):
                continue
            raise AssertionError(f"{path}: .navigationTitle({argument}) — use .japaneseNavigationTitle")
    assert ".japaneseNavigationTitle(value.title)" in source(WATCH)


def test_uikit_text_views_are_japanese_tagged():
    pattern = re.compile(
        r"\b(UILabel|UITextView|UITextField|NSMutableAttributedString|CTFramesetterCreateWithAttributedString)\("
    )
    for path, body in sources("ios/Pudge/**/*.swift").items():
        if pattern.search(body):
            assert "JapaneseType." in body, path
