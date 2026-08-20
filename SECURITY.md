# Security policy

## Supported versions

Security fixes are applied to the latest released version and `main`.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. If that feature is unavailable, contact the repository owner privately rather than opening a public issue.

Include the affected version, macOS version, reproduction steps and impact. Do not include real API tokens, private media filenames or personal AniList data. You should receive an acknowledgement within seven days.

Pudge backups omit Jimaku, AniList, qBittorrent, LLM, Jiten, and JPDB
credentials. The built-in uninstaller removes Pudge's own Keychain entries but
does not remove shared third-party applications. Treat any report of credentials
appearing in a newly created backup as sensitive.
