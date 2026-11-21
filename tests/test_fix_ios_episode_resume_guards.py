"""Episode downloads on the iPhone/iPad resume after the Mac app restarts (Swift side).

policy: the app cannot be built here (Xcode on the Mac compiles it and runs PudgeCore's
RangedResumeTests: offsets, Range/If-Range headers, the Mac's answers, retry patience, storage
cleanup). These guards keep the app on that path: bytes kept on disk, not URLSession's opaque
resume data that was lost after six quick attempts.
"""

from __future__ import annotations

import re

from source_policy import source

MEDIA_SWIFT = "ios/Pudge/Features/Media/MediaModel.swift"
CORE = "ios/PudgeCore/Sources/PudgeCore/RangedResume.swift"


def _function(text: str, signature: str) -> str:
    start = text.index(signature)
    following = re.search(r"\n    (?:private |func |// MARK)", text[start + len(signature) :])
    return text[start : start + len(signature) + (following.start() if following else len(text))]


def test_downloads_ask_for_the_rest_of_the_file_they_have() -> None:
    media = source(MEDIA_SWIFT)
    assert "downloadTask(withResumeData" not in media
    transfer = _function(media, "    private func transfer(")
    assert "RangedResume.resumeOffset(" in transfer
    assert "TransferRetry.shouldRetry(error, attempt: attempt, stalledFor:" in transfer
    attempt = _function(media, "    private func rangedAttempt(")
    assert "RangedResume.headers(offset: offset, sha256: sha256)" in attempt
    assert "session.dataTask(with: request)" in attempt
    assert "RangedResume.decide(" in media
    assert '"If-Range"' in source(CORE)


def test_a_cut_episode_keeps_its_bytes_and_goes_on_when_the_mac_answers() -> None:
    media = source(MEDIA_SWIFT)
    download = _function(
        media, "    func download(_ episode: EpisodeItem, quality chosen: VideoQuality? = nil) async {"
    )
    assert "let resumable = TransferRetry.isTransient(error)" in download
    assert "if !resumable {" in download
    assert "markInterrupted(episode.id, true" in download
    reconnected = _function(media, "    func reconnected() async {")
    assert "await resumeInterruptedDownloads()" in reconnected
    assert "media.resumeInterruptedDownloads()" in source("ios/Pudge/App/PudgeModel.swift")
