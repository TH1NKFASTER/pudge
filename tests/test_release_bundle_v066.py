from pathlib import Path


def test_release_builder_preserves_sources_and_full_test_suite() -> None:
    script = Path("build_release.sh").read_text(encoding="utf-8")
    assert "cp -R anime_mpv tests" in script
    assert "python -m pytest -q" in script
    assert 'cp "dist/anime_mpv-${VERSION}-py3-none-any.whl"' in script


def test_restored_suite_has_expected_breadth() -> None:
    tests = list(Path("tests").glob("test_*.py"))
    assert len(tests) >= 60
