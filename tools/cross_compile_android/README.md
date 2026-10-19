# Android builds with the companion fork

Branch `android-cross-build-fixes` targets Qt/PySide 6.12.0 and defaults to
**Android NDK r28c (`28.2.13676358`)**. The shared `android_utilities.py` module
also supplies this default to `pyside6-android-deploy`. Explicit CLI and
configuration paths continue to take precedence.

Install the SDK/NDK without building wheels:

```bash
python tools/cross_compile_android/main.py --download-only --auto-accept-license
```

The NDK installs below
`~/.pyside6_android_deploy/android-ndk/android-ndk-r28c`.
Omit `--auto-accept-license` to review SDK licenses interactively.

Build AArch64 wheels using a matching host Python and prepared target runtime:

```bash
python3.11 tools/cross_compile_android/main.py \
    --plat-name aarch64 --python-version 3.11 \
    --cache-dir /path/to/fresh-cache/py311 \
    --ndk-path "$HOME/.pyside6_android_deploy/android-ndk/android-ndk-r28c" \
    --sdk-path /path/to/android-sdk \
    --api-level 35 --sdk-api-level 36 \
    --qt-install-path "$HOME/Qt/6.12.0" \
    --qt-host-path "$HOME/Qt/6.12.0/gcc_64" \
    --parallel 4
```

The helper passes SDK API 36 to Java compilation independently of native API
35. Both native CMake API settings use the selected native API. The generated
toolchain keeps explicit 16 KiB linker alignment and limits x86 compiler flags
to x86 targets.

Use a new cache and clean build tree when changing NDKs. Reusing Qt installer
libraries built with r27c does not rebuild Qt with r28c. Successful wheel
compilation and ELF checks must be followed by APK/device testing.

The [guide repository](https://github.com/EchterAlsFake/PySide6-to-Android)
provides scripts to build target CPython, its native dependencies, and wheels
for Python 3.10–3.14 across all four Android ABIs. This source-build path is
separate from the deployment helper's pinned python-for-android runtime.
