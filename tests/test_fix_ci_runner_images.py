"""GitHub retired the macOS 14 runner image on 2026-11-02: the v0.8.1 CI and Release jobs on
`macos-14` were cancelled at once ("The configuration 'macos-14-arm64-us-default' is not
supported"). Every job runs on an image GitHub still offers."""

from __future__ import annotations

from source_policy import source
from t71g01_support import load_workflow

RETIRED = {"macos-12", "macos-13", "macos-14", "macos-14-large", "macos-14-xlarge"}


def test_no_job_runs_on_a_retired_image() -> None:
    for name in ("ci.yml", "release.yml"):
        # policy: the workflow files are GitHub configuration, read here as data.
        workflow = load_workflow(source(f".github/workflows/{name}"))
        labels = {job["runs-on"] for job in workflow["jobs"].values()}
        assert labels and not labels & RETIRED, (name, labels)
        assert {label for label in labels if label.startswith("macos")} <= {
            "macos-15",
            "macos-latest",
            "macos-26",
        }
