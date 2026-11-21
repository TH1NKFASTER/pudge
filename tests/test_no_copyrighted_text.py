"""The repository contains invented text only.

policy: no subtitle dialogue (Japanese or any translation), novel/light-novel/manga prose or
OCR text, song lyrics, audiobook narration or game script from real works is kept anywhere
in the repository, not even a short line. Tests and fixtures use invented text; titles and
character names are fine (title matching needs them), and so is public-domain text such as
Natsume Sōseki's 吾輩は猫である (1905).

Lines from real works that tests once carried were replaced by invented ones. This guard
holds only the SHA-256 of those lines (never the text) and fails when one of them comes
back in any file git tracks (gzip-compressed fixtures included). A line is compared
normalized: NFKC, case-folded, without spaces, punctuation and symbols; every quoted string
of a source line, every sentence and every clause of it is checked, so a line pasted into a
string literal or a subtitle cue is found. Very short or ordinary phrases (fewer than six
letters after normalization, or everyday expressions) are not listed: they are not lines of
a work.

The guard also holds the SHA-256 of the files of a replaced fixture corpus (a
glyph-substituted copy of real captions, now a synthetic corpus) and fails when one of
them is committed again.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import unicodedata
from collections.abc import Iterator

from repository_files import tracked_files
from source_policy import ROOT, source_bytes

MIN_LETTERS = 6
MAX_LETTERS = 120  # longer runs are code or several sentences, which are split
# Quotes and escaped line breaks separate the pieces of one source line; ASS override
# tags ({\\pos(…)}) are not text.
_PIECES = re.compile(r"""["'`]|\\+[nN]""")
_ASS_TAG = re.compile(r"\{\\+[^{}]*\}")
_SENTENCE_END = re.compile(r"(?<=[。．！？!?…➡➨])")
_CLAUSE = re.compile(r"[、，,]")

# sha256(normalized line) of every line from a real work that was removed.
REMOVED = frozenset(
    """
    0182d3d461a604bf6afcfb826e30ba9aed26e94073ca5f7a777e1a90b8370ed0
    047dc16e647ba5f127392e3828eb04d29f96bd330d2feb8293608f4a796c4053
    071adc611b58fb31a0cab0d783406ce4671189ddc3903b144900f2142b6944d0
    073358114f2c43e877abed4607a363fcf70351d5027f413762c02200137b7165
    08c654d41b31e15ca069ade58149b6c8fbc8654a7d2c47a7e8b1eb9746c23a56
    0ced758241ecd51045ee850004d1a7704e63f57466ebdeb0df8936c8a2c67742
    10b3fd6575673b58f6227e105cd4e56526edc7444c7dff5a04e501ef854b2da5
    1102e4ff86a365b8a0262c98fd93d87aedb33830234b215c0206b380bdc5207b
    11b519fba418c53ef13d3dda64bc72c400b00f5ba9de764f4c8927b8011644fc
    1c3248914a47c7ec510b1b799ae0a9a349ad4ad57d79fd125911ef6357a3f824
    1cef888370faa060905129a35b298e752051cd0f63bcc6a163f6ebd6bb148eee
    1da080b089c1c78f8165067c2325e5f7930d7f9b3911966b91bc00a88a8c27ca
    1f989bc9be7d3d5d65b1986f555d449575c28b571f4879f48b7df9836ccdaeb6
    29311153a4efd4e150db3dcd0caf9e61e15c5f5900df1b46dc71e9c43bcebf71
    2c2dd3f5e2ef85630058f4c550d4fa061c90d6b42e2ef5a2d02adc8fa6d1c131
    344d8ef1d47dcaaf61c54202fe888dbb16785f9dc2384d4bf6dbf7eb96bf59de
    34fb6a906e77150ad0bd8911d4acc271f4a5389b4a1390cce23083d46c4fddb1
    36d6e21e9a81812669766d96ca96c0edd24d33c9d46b5c00da8357d85724de51
    374ebf6bd77c7bc2d151b21699bb37b05f403b1201ddb8f2185b65f4972d4944
    3a54ffed476cafd7ce22a0aefac97bb0391e32ca5d06e3872e1b842415ef04e9
    3d0a6a81fbe35a81f03f2065f132f76090693f7c84fe7ab9f13bf4d6e1a847ec
    4291c49995323bbd9e3124b556f7f637fce09d964f9551a11bb6674c358ae305
    43875b1b5a35ded1ee1d878d74a3e14f05e67d08e8a07628a2d564795011eab3
    475eddea8a79dd1d45b4aa9fe547adb912a51b84f986c046163425eecf121446
    4cf119313b3122429f48ee8d40ea435d0b44e56e7da73eb0df11068b2d99017e
    525ca6357d2be48ee72d9901646df4d4b2f445d9e4a329dfa8e22b833cbaa4b7
    5594e6c08fbbc02eb4dda80f282a9f61c428087c2a40af67afca7aa4619dbc16
    55d85c0d5efffc9e30b8b1cf6b770dfc8549a844993f8d18563bab3aa37dc5aa
    57454ba0dedb510e0f0eabe215f9d12205d624f2e704d784ee02f293bf1e936d
    5832773b1c618daf875c94d60de57c997c16e2ca667f59c9ae1b26438fd1e585
    59584e577ddfeb86e670739802ced7ae6e9880b38902714f260cd8fc8caa0c4c
    620bcc9eda48404d27a2a24566850191d8313d530e06f024a2c9a93a95cb8a64
    62a09a26e8a69422f24ace6e899638203f45d36b28e1e0c96ccdbb2d3a52d752
    657515da24e7d4e36af3c98dcb7e73d18610bf91e6b22cab9435fc6326d02041
    681bb6f3464b9c1cb551bbec70ccdc2df0264ad50d4bddf7f7a2454833267211
    7309287234920d7f2ad893f350a8a6c1f9faea85e4f69b44028d4b29b6980f53
    747041c34ec402ff08901efbbf040e19e43e0b9bcd79302a9469250c465f2d2e
    74cb1ada206bc77f04fcbf350f6f2b89bf65eacfdd200503aca9764628aa1a14
    74cfb6df7dc7ec5e7ffacf497c46f66be3904199f048c259e42be940b02bff1a
    7655be7c67889933346510d9f8996219c1451bd3ff9a8e87c66308d7e88f2d85
    77a52c49e971368408931ac64bcde65a69aa27bc489cd60e1bad5051a597db53
    8f3c5c995be4f01c57dcae12d00a102645977b6147bf5ab321a4633977981ac8
    8f82527d4c61f3f2b15178c3f6c026366540287d0ed761edb83e50d53a65758f
    94731e5fe0a82a5084ede7b6149047201ee8f7a69eaa1deceb236dd880fdd6e9
    9915f1b02b1f1226727da20656340a240a9a651a6e89a2bdc89ba0e38b32fe98
    9ada3073716e81000cfadc40aa429b2f7bf08af181e33ee426bd98f82907f8e9
    9d13068998aa07c493a3f2c9fe245260f08cf72f5270803d31d087f480a7d49d
    a0fd843a212788b61369d0295685a492aaafa3eef0ee6bb8bef7b82b774c2565
    a372622808d07e9dbae38b8466e52fcd507ff662d99a6eb857180a90208d5907
    a3e854b10dd7a9f8658aa93f45675a8642f65db4fb2a978480e74c6ed1c04175
    af40af74418ccaef24dfc647f8a4369d6a4fc5b99df53929071d21101be884cf
    b0807ed817ea0e054b8fcec3c2630022a1a09e056f0efc98957bb174c9c69b4e
    b5b5c27a698d12ff16e7c9f4c2daea24fec650f9eaae72ba7514e014230e3bed
    beeb0372cac69a2d15e53ce105ffe0b1d27c43299768830ad87f26fc7c2c75a4
    c0a7b57f6cec9defb805907f553fe5e9b55bc1e72a6977a2d658506c78b47b27
    c2fbd8f72a033de58431c3280cfd3a66fb5a5c5e7592f34c2946a032caf3b9c1
    c6b16ccd1c2388220fe70a8d2f58aee5c1050c39f6721d11b329bf183d422c08
    c877fa4b975c1ace5c4e35c5fe0db9a5b710fbdf865cb829a27d21ad5e4dceda
    ce2aa6ef7a673a58e0b9382fb141b3deb9f2f32e176460aac40c6be986431d88
    cfbc29300a633336fb66318e5d2d22d2e8a56e2d33ebdcb8e4db6ce1e2b3285d
    d0e2c1f7eddc19fc19631675c8c885c442ab79899bef27806885852fc7ed3038
    d29c22a331888f56f99a8301ffcd089abc25b1039f4fc5abf04aa47969957426
    d90e47e22306e345ad5856e7a91f8cd8426c8a7aaba2c077512135bd4b823d33
    dc4bbbca5c2333bb44680d7b7afbe33db33513d2515f163128fc65a88c7b3679
    f4b217b7b6a0b0f0697d921b3301baee759cfe716ecbadf6a1e77a9ddcb71c80
    f56ba551341d44a26c546132ca0f7475b28ae7122e9cc609c59993ae9c758a84
    f5b6096b06c28214187c305ee93539af01a600dcbac7a62305c37e45246adccd
    f76fb2d1452c0fe63d851a49c7ac28257151dc149d09d4d4896018552837b7ec
    f8e61547a7f65628e92ecf5ca11fc027a470f31915a8b2df53f19a2a2f29a6d0
    fc548796cbcf6265412f975f147934087cf2023a09d87fa3a37b46ba5c8142fc
    ff3e56ac5a592d9521688ce28b167df657a6fada5c71f8d34c349fb42522902d
    ffb46cff0369f0ba5022c4887a6a27e2691a4d075690d322d731f11899b36361
    fff69fe94029f1266be152ce70ec91f3f0cc31dcdd03361fc45102ec1d7fa19e
""".split()
)


_NOT_LETTERS = re.compile(r"[\W_]+")
_SPLITS = re.compile(r"[。．！？!?…➡➨、，,]")

# sha256 of the files of the replaced caption corpus of tests/fixtures/asr_review_v19 (a
# glyph-substituted copy of real captions and ASR, and its snapshot), stored and unpacked.
REMOVED_FILES = frozenset(
    """
    03c861fd41d31beaa599c7a7bc5817ff7b0c5d972fa8f48a0d6c9b3761e4bc92
    089663777261b66825033fa0e4dff62332e1c2d51810ae95228970f93783c937
    3535bfdc29caea1aab7ce4611d468b2726c37c9bead79d215c40ac37fd941713
    441b33164f62a9bb538b4d8f06294c2a56c07179a266ad328b77bbede909228f
    52a84edc054fe2b43ba7851173a9f2ceb6ad9cfa9d6e10109c497b000fe9cab1
    5ebd4cc195f4762fbcfa1fe743f7cc44f0f7dca3fdb4734d70c9961d25c901ac
    6f6adbe87de6c83f9d7300e31b33ea9d7a6465251a2c93597a87d8bb7e54bbaf
    73f82eb79e1f6a4c0726eb9aa2274ff246880fa86eeb325f342d5ffd4b1fbafe
    7ebb7b62d62521059aaff9cecf5df27a4af812d705be35e7d165ded8d16a2722
    84c5546075e60e3ab54bee0bfde24821911a9125de3c87ca5c7ff0467b89f826
    8c2846e7f44a56080d838262b28cb77448f84aae50a55d7094575800e9d65ef1
    920309d9f8688e90f64efbb3b0494a7a9647724b0d37d96afa260f7d02323cc4
    b8ad24e93e443e7125bc77573d1d2f52298f0a7ef2e9de59d0e49509c9ce805a
    be45803e3c258b3c56b257771c050357deb3ef9faaf00244ab507091fb8958f7
    e292c70beab97495687ecb7d557f6bbbcb2ce8cb974d265ada00e0a62102f109
    fe5fc425e2b22c21e8dfd1c4f37c0922da4d80d8f5511cb8e2de901c6f3a0594
""".split()
)


def normalized(text: str) -> str:
    if text.isascii():
        return _NOT_LETTERS.sub("", text.lower())
    return _NOT_LETTERS.sub("", unicodedata.normalize("NFKC", text).casefold())


def candidates(line: str) -> Iterator[str]:
    """Each quoted piece of a line (the whole line when it has no quotes), each sentence and
    each clause of it, normalized."""
    if "{\\" in line:
        line = _ASS_TAG.sub(" ", line)
    seen = set()
    for piece in _PIECES.split(line):
        if len(piece) < MIN_LETTERS:
            continue
        parts = [piece]
        if _SPLITS.search(piece):
            for sentence in _SENTENCE_END.split(piece):
                parts.append(sentence)
                parts.extend(_CLAUSE.split(sentence))
        for part in parts:
            key = normalized(part)
            if MIN_LETTERS <= len(key) <= MAX_LETTERS and key not in seen:
                seen.add(key)
                yield key


def digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def text_files() -> Iterator[tuple[str, str]]:
    tracked = tracked_files(ROOT)
    names = sorted(tracked) if tracked is not None else []
    for name in names:
        try:
            data = source_bytes(name)
        except OSError:
            continue  # tracked but deleted in this checkout
        if name.endswith(".gz"):
            try:
                data = gzip.decompress(data)
            except (OSError, EOFError):
                continue
        try:
            yield name, data.decode("utf-8")
        except UnicodeDecodeError:
            continue  # images, models and other binary files


def findings(files) -> list[str]:
    found, clean = [], set()
    for name, text in files:
        for number, line in enumerate(text.splitlines(), 1):
            if len(line) < MIN_LETTERS or line in clean:
                continue
            if any(digest(key) in REMOVED for key in candidates(line)):
                found.append(f"{name}:{number}")
            else:
                clean.add(line)
    return found


def test_the_hash_list_is_well_formed() -> None:
    assert len(REMOVED) >= 60
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in REMOVED)


def test_no_tracked_file_holds_a_removed_line() -> None:
    assert tracked_files(ROOT) is not None, "run from a git checkout"
    found = findings(text_files())
    assert found == [], "lines from real works are back (use invented text): " + ", ".join(found[:20])


def test_no_tracked_file_is_a_file_of_the_replaced_caption_corpus() -> None:
    assert len(REMOVED_FILES) == 16
    tracked = tracked_files(ROOT)
    assert tracked is not None, "run from a git checkout"
    found = []
    for name in sorted(tracked):
        try:
            data = source_bytes(name)
        except OSError:
            continue
        digests = {hashlib.sha256(data).hexdigest()}
        if name.endswith(".gz"):
            try:
                digests.add(hashlib.sha256(gzip.decompress(data)).hexdigest())
            except (OSError, EOFError):
                pass
        if digests & REMOVED_FILES:
            found.append(name)
    assert found == [], "a file of the replaced caption corpus is back: " + ", ".join(found)


def test_a_listed_line_is_found_inside_code_and_subtitle_text() -> None:
    # The normalization and splitting find a line however it is quoted or laid out.
    invented = "雨の日の図書館はとても静かだった。"
    listed = digest(normalized(invented))
    for line in (
        f'    (12.5, 14.0, "{invented}"),',
        f"text = '1\\n00:00:01,000 --> 00:00:02,000\\n{invented}\\n'",
        f"前の文です。{invented.replace('。', '！')}次の文。",
        f"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{{\\pos(10,10)}}{invented}",
    ):
        assert listed in {digest(key) for key in candidates(line)}, line
    assert listed not in {digest(key) for key in candidates("雨の日の図書館")}
