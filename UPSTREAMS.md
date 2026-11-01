# Upstream sources and licenses

This repository starts from the complete `obadevyh/openseek` source tree at commit
`1216bfc2abbb51ee8f5ecb1bcd0ffa4c7302c5b1`. Its original `LICENSE`, notices,
copyright statements, and third-party notices are preserved. The OpenSeek backend and this extension are
distributed under AGPL-3.0-or-later.

The following checked-out projects informed integration design:

* `pencarimovie-server` (`LICENSE` identifies MIT, copyright PencariMovie Downloader contributors). Its public
  thumbnail route demonstrates serving Telegram media bytes through an ordinary HTTP image route. Its Rust/
  Grammers implementation is not copied into this Python project; OpenSeek already owns the HTTP cache and
  routes, so this project implements the Telegram Bot API adapter in Python.
* `Telegram-Stremio` (`LICENSE` identifies GPL-3.0). Its media-streaming code is not copied: it streams video
  content and uses a different runtime, while this pool transfers only small, already-generated JPEG documents.
  The Telegram Bot API is used directly, without PyroFork or MTProto client code.

No source files from those two repositories are included in this initial implementation. If source code from
either is added later, preserve its copyright notices and full license, and review the resulting combined-work
license before distributing it.
