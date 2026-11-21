# Security policy

## Supported versions

Security fixes are applied to the latest released version and `main`.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. If that feature is unavailable, contact the repository owner privately rather than opening a public issue.

Include the affected version, macOS version, reproduction steps and impact. Do not include real API tokens, private media filenames or personal AniList data. You should receive an acknowledgement within seven days.

Pudge backups omit Jimaku, AniList, qBittorrent, LLM, Jiten, and JPDB
credentials, the audiobook TTS server key (and a password in its address), and
the release-search proxy address. A full backup does include the iPhone sync
identity — the Mac's server secret and its TLS private key — so a restored Mac
stays paired with your iPhone; keep backup files private and do not share them.
The built-in uninstaller removes Pudge's own Keychain entries but does not
remove shared third-party applications. Treat any report of credentials
appearing in a newly created backup as sensitive.
