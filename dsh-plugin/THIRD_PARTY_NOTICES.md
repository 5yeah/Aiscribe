# Third-party notices

`dsh-aireport` itself is MIT-licensed (see `LICENSE`). The npm package also ships a
PyInstaller-built Windows sidecar under `runtime/win32-x64/`, which bundles the
following third-party components:

| Component | License | Text |
|---|---|---|
| CPython 3.10 | Python Software Foundation License 2.0 | `third-party/CPython-LICENSE.txt` |
| PyInstaller 6.22.3 bootloader | GPL-2.0-or-later, with the PyInstaller exception permitting distribution of bundled applications | `third-party/PyInstaller-COPYING.txt` |
| zstandard 0.19.0 (Python bindings) | BSD-3-Clause | `third-party/zstandard-LICENSE.txt` |
| OpenSSL 1.1.1 | Apache License 2.0 | <https://www.openssl.org/source/license.html> |
| SQLite | Public Domain | <https://www.sqlite.org/copyright.html> |
| zlib | zlib License | <https://www.zlib.net/zlib_license.html> |
| bzip2 | BSD-like bzip2 license | <https://sourceware.org/bzip2/> |
| liblzma / XZ Utils | Public Domain | <https://tukaani.org/xz/> |
| Microsoft Visual C++ runtime DLLs | Microsoft distributable code terms | Included by the Python/Windows toolchain |

The sidecar is built from this repository's `packaging/sidecar_entry.py` with
`scripts/build_sidecar.py`; the build is reproducible from source. No third-party
component is modified.

The Aiscribe Python source has no mandatory third-party runtime dependencies; the
optional `zstandard` package is used only to read zstd-compressed Codex/dsh session
files and is included in the sidecar for that purpose.
