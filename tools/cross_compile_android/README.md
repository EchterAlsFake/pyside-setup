# Android cross-build helper

This fork extends the Qt for Python Android helper so one checkout can build
wheels for multiple CPython versions without editing source files or reusing a
stale toolchain.

## Important API split for Qt 6.11

Qt 6.11 needs Android SDK platform 36 for its Java sources, but its supported
NDK, r27c (`27.2.12479018`), only provides native compiler wrappers through API
35. Consequently these are intentionally separate options:

- `--sdk-api-level 36` installs the Java compile SDK when the helper manages
  the SDK.
- `--api-level 35` selects the native API used for CPython and the wheel.

Using 36 for both fails before compilation because binaries such as
`aarch64-linux-android36-clang` do not exist in NDK r27c.

## Example

The host interpreter must have the same major/minor version as
`--python-version`. The host Qt and target Qt may be separate source-built
prefixes.

```bash
python tools/cross_compile_android/main.py \
    --plat-name aarch64 \
    --python-version 3.14 \
    --api-level 35 \
    --sdk-api-level 36 \
    --cache-dir /path/to/cache/python-3.14 \
    --ndk-path /path/to/android-ndk-r27c \
    --sdk-path /path/to/android-sdk \
    --qt-install-path /path/to/Qt/6.11.2 \
    --qt-host-path /path/to/Qt/6.11.2/gcc_64 \
    --parallel 4
```

The version is part of the generated CPython, installation, and CMake
toolchain names. Generated scripts and toolchains are rewritten on every run,
because they contain absolute paths and must not silently survive a change of
Python, Qt, SDK, NDK, or API level.

The helper validates both sides of the API split before starting an expensive
build. It checks for `platforms/android-<sdk-api>/android.jar` in the SDK and
for the architecture-specific `<triplet><native-api>-clang` wrapper in the
NDK. A cached Python prefix is reused only when it contains the matching
headers and shared `libpython`.

When the helper is launched with an absolute virtual-environment interpreter,
it also places that environment's scripts directory on `PATH`. This is needed
because invoking `venv/bin/python` does not itself activate the environment,
and later packaging subprocesses must still be able to find tools such as
`patchelf`.

## Tested matrix

The `android-cross-build-fixes` branch based on `v6.11.2` produced AArch64
PySide6 and Shiboken6 wheels for CPython 3.10, 3.11, and 3.14 on 10 September
2026. All packaged ELF files were AArch64 Android binaries, the extensions
linked to the matching `libpython3.x.so`, and both Qt and PySide shared objects
used 16 KiB load alignment.

This fork also gates the generated `QSslEllipticCurve` wrapper on Qt's `ssl`
feature. Upstream listed the wrapper as an unconditional QtNetwork source even
though Shiboken correctly omits it for a Qt build without SSL, leaving a unity
source that includes a nonexistent file.

Compilation and static wheel inspection do not replace an application test on
an Android device or emulator. Python 3.10 and 3.14 remain fork-supported,
experimental targets; Qt's published Android wheels use Python 3.11.

## Maintaining the fork

Keep Qt's repository as the `upstream` remote and the maintainer's GitHub fork
as `origin`. Use one Android-fix branch per upstream release line rather than
building long-term work directly on Qt's `dev` branch. The initial baseline is
upstream tag `v6.11.2`, branch `android-cross-build-fixes`, with fork release
tags following `v<upstream>-android.<revision>`.

For a new Qt for Python release:

1. fetch `upstream` and create the next branch from the matching signed or
   annotated upstream tag;
2. replay the Android commits, resolving conflicts narrowly rather than
   merging unrelated upstream history into the patch series;
3. build the supported Python/ABI matrix against the exact same Qt release;
4. inspect wheel tags, ELF architecture, `libpython` dependencies, 16 KiB load
   alignment, and archive integrity; and
5. run an APK smoke test on a device or emulator before publishing a fork
   release.

Keep generated CPython trees, Qt installations, wheels, and build logs outside
this repository. Only source changes and reproducibility documentation belong
on the maintenance branch.

## CPython caveat

The generic configure path builds the interpreter, headers, standard library,
and the extension modules supported directly by the NDK. Libraries such as
OpenSSL, libffi, bzip2, and lzma are not supplied by the NDK. Their CPython
modules are therefore omitted unless matching Android builds of those
dependencies are provided. Python 3.14 also has an official Android build
workflow under `Platforms/Android` which can produce a more complete runtime;
the generic installation here is sufficient as the target development prefix
for compiling Qt for Python wheels.
