# Third-party notices

The native mpv AniList tracking architecture was inspired by
`AzuredBlue/mpv-anilist-updater`.

MIT License

Copyright (c) 2024 AzuredBlue

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


## Optional external applications

Pudge does not bundle mpv, FFmpeg, JitenMPV, or jpdb-mpv-plugin binaries. During
initial setup, the user may explicitly ask Pudge to install missing mpv/FFmpeg
Homebrew formulae or the official JitenMPV release. They remain separate
programs in the user's normal per-user or system package locations.

- mpv: GPL-2.0-or-later, <https://github.com/mpv-player/mpv>
- FFmpeg: primarily LGPL-2.1-or-later; the exact license can depend on the
  options used by the separately installed build, <https://ffmpeg.org/legal.html>
- JitenMPV: Apache-2.0, <https://github.com/Sirush/JitenMPV>
- jpdb-mpv-plugin: optional user-installed integration,
  <https://github.com/Sahil811/jpdb-mpv-plugin>

The JitenMPV setup action downloads a pinned copy of its official Unix
installer. That installer verifies the checksum published with the selected
JitenMPV release before installing it. Pudge only writes the API key supplied
by the user to JitenMPV's own per-user configuration file.

Pudge can detect and configure an existing jpdb-mpv-plugin installation but
does not download or redistribute it. The upstream repository does not
currently publish a license file, and its server must be built locally with Go.
