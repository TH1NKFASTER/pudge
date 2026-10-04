"""Keep private page dependencies out of a source checkout and installation."""

import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
policy = runpy.run_path(str(ROOT / "pudge/ocr_fixture_policy.py"))
gate = runpy.run_path(str(ROOT / "pudge/install_checks.py"))


def test_public_checkout_has_only_generated_ocr_inputs():
    assert policy["check"](ROOT) == []


@pytest.mark.parametrize(
    "path,content",
    [
        ("tests/page.png", b"private pixels"),
        ("tests/golden/acceptance.json", b"{}"),
        ("tests/fixtures/v96p28/fresh/page.json", b"{}"),
        ("tests/fixtures/ocr.json", b'{"data":{"regions":[]}}'),
        ("tests/test_private.py", b'import os\nROOT = os.environ.get("PUDGE_V96P99_FIXTURES")\n'),
        (
            "tests/test_old.py",
            b'from pudge import manga_ocr_worker\nfrom PIL import Image\nimage = Image.open("/private/page.png")\n',
        ),
    ],
)
def test_gate_rejects_private_materials_before_starting_pytest(tmp_path, path, content):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    assert policy["check"](tmp_path)
    logs = tmp_path / "logs"
    with pytest.raises(RuntimeError, match="OCR fixture policy failed"):
        gate["run_suite"](tmp_path, Path(sys.executable), logs)
    assert not logs.exists()


def test_gate_rejects_partial_success_with_missing_ocr_fixture_skip(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_manga_ocr.py").write_text(
        "import pytest\ndef test_generated():\n    assert True\n"
        'def test_private():\n    pytest.skip("real-image fixtures absent")\n'
    )
    with pytest.raises(RuntimeError, match="OCR test skipped for missing materials"):
        gate["run_suite"](tmp_path, Path(sys.executable), tmp_path / "logs")


def test_policy_accepts_generated_pages_and_declared_synthetic_regions(tmp_path):
    (tmp_path / "tests/fixtures").mkdir(parents=True)
    (tmp_path / "tests/fixtures/synthetic.json").write_text('{"synthetic":true,"regions":[]}')
    (tmp_path / "tests/test_generated.py").write_text(
        "from pudge import manga_ocr_worker\nfrom PIL import Image\n"
        'def test_ink():\n    image = Image.new("RGB", (760,1200), "white")\n'
    )
    assert policy["check"](tmp_path) == []
