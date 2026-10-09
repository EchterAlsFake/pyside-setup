# Copyright (C) 2023 The Qt Company Ltd.
# SPDX-License-Identifier: LicenseRef-Qt-Commercial OR LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only
from __future__ import annotations

import os
import sys
import logging
import argparse
import stat
import warnings
import shutil
from dataclasses import dataclass

from pathlib import Path
from git import Repo, RemoteProgress, GitCommandError
from tqdm import tqdm
from jinja2 import Environment, FileSystemLoader

from android_utilities import (run_command, download_android_commandlinetools,
                               download_android_ndk, install_android_packages)

# The host interpreter must have the same major/minor version and OpenSSL support.
DEFAULT_PYTHON_VERSION = "3.11"
DEFAULT_SDK_API_LEVEL = "36"

# minimum Android API version support. This is set according to Qt's requiremnts and needs to
# be updated if Qt's minimum API level is updated.
MIN_ANDROID_API_LEVEL = "28"

SKIP_UPDATE_HELP = ("skip the updation of SDK packages build-tools, platform-tools to"
                    " latest version")

ACCEPT_LICENSE_HELP = ('''
Accepts license automatically for Android SDK installation. Otherwise,
accept the license manually through command line.
''')

CLEAN_CACHE_HELP = ('''
Cleans cache stored in $HOME/.pyside6_android_deploy (or --cache-dir).
Options:

1. all - all the cache including Android Ndk, Android Sdk and Cross-compiled Python are deleted.
2. ndk - Only the Android Ndk is deleted.
3. sdk - Only the Android Sdk is deleted.
4. python - The cross compiled Python for all platforms, the cloned CPython, the cross compilation
            scripts for all platforms are deleted.
5. toolchain - The CMake toolchain file required for cross-compiling Qt for Python, for all
               platforms are deleted.

If --clean-cache is used and no explicit value is supplied, then `all` is used as default.
''')

COIN_RUN_HELP = ('''
When run by Qt's continuous integration system COIN. This option is irrelevant to user building
their own wheels.
''')


@dataclass
class PlatformData:
    plat_name: str
    api_level: str
    android_abi: str
    qt_plat_name: str
    gcc_march: str
    plat_bits: str


def occp_exists():
    '''
    check if '--only-cross-compile-python' exists in command line arguments
    '''
    return "-occp" in sys.argv or "--only-cross-compile-python" in sys.argv


def download_only_exists():
    '''
    check if '--download-only' exists in command line arguments
    '''
    return "--download-only" in sys.argv


class CloneProgress(RemoteProgress):
    def __init__(self):
        super().__init__()
        self.pbar = tqdm()

    def update(self, op_code, cur_count, max_count=None, message=""):
        self.pbar.total = max_count
        self.pbar.n = cur_count
        self.pbar.refresh()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="This tool cross builds CPython for Android and uses that Python to cross "
                    "build Android Qt for Python wheels",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument("-p", "--plat-name", type=str, nargs="*",
                        choices=["aarch64", "armv7a", "i686", "x86_64"],
                        default=["aarch64", "armv7a", "i686", "x86_64"], dest="plat_names",
                        help="Android target platforms")

    parser.add_argument("-v", "--verbose", help="run in verbose mode", action="store_const",
                        dest="loglevel", const=logging.INFO)
    # As opposed to Qt, Qt for Python does not require API level 28 because it can be built with a
    # higher API for toolchain compatibility, while still remaining compatible with Qt's runtime
    # minimum.
    parser.add_argument("--api-level", type=str, default="35",
                        help="Android native API level used to compile Python "
                             "(default: %(default)s)")
    parser.add_argument(
        "--sdk-api-level",
        type=str,
        default=DEFAULT_SDK_API_LEVEL,
        help="Android SDK platform installed when the SDK is managed automatically "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--python-version",
        type=str,
        default=DEFAULT_PYTHON_VERSION,
        help="Python major.minor version to build (default: %(default)s)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="Cache directory (default: ~/.pyside6_android_deploy)",
    )
    parser.add_argument("--ndk-path", type=str, help="Path to Android NDK (preferred r27c)")
    # sdk path is needed to compile all the Qt Java Acitivity files into Qt6AndroidBindings.jar
    parser.add_argument("--sdk-path", type=str, help="Path to Android SDK")
    parser.add_argument(
        "--qt-install-path",
        type=str,
        required=not (occp_exists() or download_only_exists()),
        help="Qt installation path eg: /home/Qt/6.8.0"
    )
    parser.add_argument(
        "--qt-host-path",
        type=str,
        help="Host Qt prefix; defaults to <qt-install-path>/gcc_64 on Linux",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=4,
        help="Maximum parallel jobs for the Qt for Python build (default: %(default)s)",
    )

    parser.add_argument("-occp", "--only-cross-compile-python", action="store_true",
                        help="Only cross compiles Python for the specified Android platform")

    parser.add_argument("--dry-run", action="store_true", help="show the commands to be run")

    parser.add_argument("--skip-update", action="store_true",
                        help=SKIP_UPDATE_HELP)

    parser.add_argument("--auto-accept-license", action="store_true",
                        help=ACCEPT_LICENSE_HELP)

    parser.add_argument("--clean-cache", type=str, nargs="?", const="all",
                        choices=["all", "python", "ndk", "sdk", "toolchain"],
                        help=CLEAN_CACHE_HELP)

    parser.add_argument("--coin", action="store_true",
                        help=COIN_RUN_HELP)

    parser.add_argument("--download-only", action="store_true",
                        help="Only download Android NDK and SDK")

    parser.add_argument("--limited-api", choices=["yes", "no"], default=None,
                        help="Use Limited API (yes/no). When yes, tags wheel abi3.")

    parser.add_argument("--dist-dir", type=Path, default=None,
                        help="Destination directory for built wheels")

    args = parser.parse_args()

    logging.basicConfig(level=args.loglevel)
    pyside_setup_dir = Path(__file__).parents[2].resolve()

    # Invoking a virtual-environment interpreter by absolute path does not
    # activate that environment. Keep its scripts directory on PATH so each
    # setup.py subprocess can find build-time tools such as patchelf. Changes
    # made inside the first subprocess cannot propagate back to this driver.
    interpreter_scripts_dir = Path(sys.executable).absolute().parent
    current_path = os.environ.get("PATH", "")
    path_entries = current_path.split(os.pathsep) if current_path else []
    if str(interpreter_scripts_dir) not in path_entries:
        os.environ["PATH"] = os.pathsep.join(
            [str(interpreter_scripts_dir), *path_entries]
        )

    qt_install_path = (
        str(Path(args.qt_install_path).expanduser().resolve())
        if args.qt_install_path
        else None
    )
    explicit_qt_host_path = args.qt_host_path
    ndk_path = args.ndk_path
    sdk_path = args.sdk_path
    only_py_cross_compile = args.only_cross_compile_python
    android_abi = None
    gcc_march = None
    plat_bits = None
    dry_run = args.dry_run
    plat_names = args.plat_names
    api_level = args.api_level
    sdk_api_level = args.sdk_api_level
    python_version = args.python_version
    skip_update = args.skip_update
    auto_accept_license = args.auto_accept_license
    clean_cache = args.clean_cache
    coin = args.coin
    download_only = args.download_only

    host_python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if not download_only and host_python_version != python_version:
        parser.error(
            f"--python-version={python_version} requires a matching host Python; "
            f"the current interpreter is Python {host_python_version}"
        )
    if args.parallel < 1:
        parser.error("--parallel must be at least 1")

    # A venv interpreter sets sys._home. Python 3.10's cross-build sysconfig
    # then mistakes that installed prefix for the CPython source directory.
    # Use the underlying interpreter to run CPython's build-time tooling.
    host_build_python = Path(
        getattr(sys, "_base_executable", None) or sys.executable
    ).resolve()

    # auto download Android NDK and SDK
    pyside6_deploy_cache = (
        args.cache_dir.expanduser().resolve()
        if args.cache_dir
        else Path.home() / ".pyside6_android_deploy"
    )
    logging.info(f"Cache created at {str(pyside6_deploy_cache.resolve())}")
    pyside6_deploy_cache.mkdir(parents=True, exist_ok=True)

    if pyside6_deploy_cache.exists() and clean_cache:
        if clean_cache == "all":
            shutil.rmtree(pyside6_deploy_cache)
        elif clean_cache == "ndk":
            cached_ndk_dir = pyside6_deploy_cache / "android-ndk"
            if cached_ndk_dir.exists():
                shutil.rmtree(cached_ndk_dir)
        elif clean_cache == "sdk":
            cached_sdk_dir = pyside6_deploy_cache / "android-sdk"
            if cached_sdk_dir.exists():
                shutil.rmtree(cached_sdk_dir)
        elif clean_cache == "python":
            cached_cpython_dirs = [
                pyside6_deploy_cache / "cpython",
                *pyside6_deploy_cache.glob("cpython-*"),
            ]
            for cached_cpython_dir in cached_cpython_dirs:
                if cached_cpython_dir.is_dir():
                    shutil.rmtree(cached_cpython_dir)
            for cc_python_path in pyside6_deploy_cache.glob("Python-*"):
                if cc_python_path.is_dir():
                    shutil.rmtree(cc_python_path)
        elif clean_cache == "toolchain":
            for toolchain_path in pyside6_deploy_cache.glob("toolchain_*"):
                if toolchain_path.is_file():
                    toolchain_path.unlink()
    pyside6_deploy_cache.mkdir(parents=True, exist_ok=True)

    if not ndk_path:
        # Download android ndk
        ndk_path = download_android_ndk(pyside6_deploy_cache)

    if not sdk_path:
        # download and unzip command-line tools
        sdk_path = download_android_commandlinetools(pyside6_deploy_cache)
        # install and update required android packages
        install_android_packages(android_sdk_dir=sdk_path, android_api=sdk_api_level,
                                 dry_run=dry_run, accept_license=auto_accept_license,
                                 skip_update=skip_update)

    ndk_path = str(Path(ndk_path).expanduser().resolve())
    sdk_path = str(Path(sdk_path).expanduser().resolve())

    ndk_toolchain_bin = (
        Path(ndk_path)
        / "toolchains"
        / "llvm"
        / "prebuilt"
        / f"{sys.platform}-x86_64"
        / "bin"
    )
    if not dry_run:
        android_jar = Path(sdk_path) / "platforms" / f"android-{sdk_api_level}" / "android.jar"
        if not android_jar.is_file():
            parser.error(
                f"Android SDK platform {sdk_api_level} is missing ({android_jar}). "
                f"Install platforms;android-{sdk_api_level} or choose --sdk-api-level accordingly."
            )

        if not ndk_toolchain_bin.is_dir():
            parser.error(f"Android NDK LLVM toolchain directory is missing: {ndk_toolchain_bin}")

    if download_only:
        print(f"Android NDK and SDK downloaded successfully into {pyside6_deploy_cache}")
        sys.exit(0)

    templates_path = Path(__file__).parent / "templates"

    for plat_name in plat_names:
        # for armv7a the API level dependent binaries like clang are named
        # armv7a-linux-androideabi27-clang, as opposed to other platforms which
        # are named like x86_64-linux-android27-clang
        platform_data = None
        if plat_name == "armv7a":
            platform_data = PlatformData("armv7a", api_level, "armeabi-v7a", "armv7",
                                         "armv7", "32")
        elif plat_name == "aarch64":
            platform_data = PlatformData("aarch64", api_level, "arm64-v8a", "arm64_v8a", "armv8-a",
                                         "64")
        elif plat_name == "i686":
            platform_data = PlatformData("i686", api_level, "x86", "x86", "i686", "32")
        else:  # plat_name is x86_64
            platform_data = PlatformData("x86_64", api_level, "x86_64", "x86_64", "x86-64", "64")

        # python path is valid, if Python for android installation exists in python_path
        python_cache_version = python_version.replace(".", "_")
        python_path = (
            pyside6_deploy_cache
            / f"Python-{platform_data.plat_name}-linux-android-{python_cache_version}"
            / "_install"
        )
        valid_python_path = python_path.exists()
        if python_path.exists():
            expected_paths = [
                ("directory", python_path / "lib"),
                ("directory", python_path / f"include/python{python_version}"),
                ("file", python_path / "lib" / f"libpython{python_version}.so"),
            ]
            for expected_type, expected_path in expected_paths:
                path_is_valid = (
                    expected_path.is_dir()
                    if expected_type == "directory"
                    else expected_path.is_file()
                )
                if not path_is_valid:
                    valid_python_path = False
                    warnings.warn(
                        f"{python_path.resolve()} is missing {expected_path}. "
                        f"New Python for {plat_name} "
                        f"android will be cross-compiled into {str(pyside6_deploy_cache.resolve())}"
                    )
                    break

        compiler_target = f"{platform_data.plat_name}-linux-android"
        if plat_name == "armv7a":
            compiler_target += "eabi"
        native_compiler = ndk_toolchain_bin / f"{compiler_target}{api_level}-clang"
        if not dry_run and not native_compiler.is_file():
            parser.error(
                f"NDK compiler wrapper for native API {api_level} is missing: "
                f"{native_compiler}. Choose a lower --api-level or a compatible NDK."
            )

        environment = Environment(loader=FileSystemLoader(templates_path))
        if not valid_python_path:
            cpython_dir = pyside6_deploy_cache / f"cpython-{python_cache_version}"
            python_ccompile_script = cpython_dir / f"cross_compile_{plat_name}.sh"

            if not cpython_dir.exists():
                logging.info(f"cloning cpython {python_version}")
                try:
                    Repo.clone_from(
                        "https://github.com/python/cpython.git",
                        cpython_dir,
                        progress=CloneProgress(),
                        branch=python_version,
                        depth=1,
                    )
                except GitCommandError as e:
                    # Print detailed error information
                    print(f"Error cloning repository: {e}")
                    print(f"Command: {e.command}")
                    print(f"Status: {e.status}")
                    print(f"Stderr: {e.stderr}")
                    raise

            # Always regenerate the script: it embeds version, NDK, API and
            # interpreter paths, all of which can change between invocations.
            host_system_config_name = run_command("./config.guess", cwd=cpython_dir,
                                                  dry_run=dry_run, show_stdout=True,
                                                  capture_stdout=True).strip()

            template = environment.get_template("cross_compile.tmpl.sh")
            content = template.render(
                plat_name=platform_data.plat_name,
                ndk_path=ndk_path,
                api_level=platform_data.api_level,
                android_py_install_path=python_path,
                host_python_path=host_build_python,
                host_python_dir=host_build_python.parent,
                python_version=python_version,
                host_system_name=host_system_config_name,
                host_platform_name=sys.platform
            )

            logging.info(f"Writing Python cross compile script into {python_ccompile_script}")
            with open(python_ccompile_script, mode="w", encoding="utf-8") as ccompile_script:
                ccompile_script.write(content)

            python_ccompile_script.chmod(python_ccompile_script.stat().st_mode | stat.S_IEXEC)

            # clean built files
            logging.info("Cleaning CPython built files")
            run_command(["make", "distclean"], cwd=cpython_dir, dry_run=dry_run, ignore_fail=True)

            # run the cross compile script
            logging.info(f"Running Python cross-compile for platform {platform_data.plat_name}")
            run_command([f"./{python_ccompile_script.name}"], cwd=cpython_dir, dry_run=dry_run,
                        show_stdout=True)

            logging.info(
                f"Cross compile Python for Android platform {platform_data.plat_name}. "
                f"Final installation in {python_path}"
            )

            if only_py_cross_compile:
                continue

        if only_py_cross_compile:
            requested_platforms = ",".join(plat_names)
            print(f"Python for Android platforms: {requested_platforms} cross compiled "
                  f"to {str(pyside6_deploy_cache)}")
            sys.exit(0)

        qfp_toolchain = (
            pyside6_deploy_cache
            / f"toolchain_{platform_data.plat_name}_py{python_cache_version}.cmake"
        )

        # Always regenerate the toolchain file for the same reason as the
        # Python cross-compile script. Reusing it can silently target stale Qt,
        # SDK, NDK or Python installations.
        template = environment.get_template("toolchain_default.tmpl.cmake")
        content = template.render(
            ndk_path=ndk_path,
            sdk_path=sdk_path,
            api_level=platform_data.api_level,
            qt_install_path=qt_install_path,
            plat_name=platform_data.plat_name,
            android_abi=platform_data.android_abi,
            qt_plat_name=platform_data.qt_plat_name,
            gcc_march=platform_data.gcc_march,
            plat_bits=platform_data.plat_bits,
            python_version=python_version,
            target_python_path=python_path,
            min_android_api=MIN_ANDROID_API_LEVEL
        )

        logging.info(f"Writing Qt for Python toolchain file into {qfp_toolchain}")
        with open(qfp_toolchain, mode="w", encoding="utf-8") as ccompile_script:
            ccompile_script.write(content)

        qfp_toolchain.chmod(qfp_toolchain.stat().st_mode | stat.S_IEXEC)

        if sys.platform == "linux":
            host_qt_install_suffix = "gcc_64"
        elif sys.platform == "darwin":
            host_qt_install_suffix = "macos"
        else:
            raise RuntimeError("Qt for Python cross compilation not supported on this platform")

        if explicit_qt_host_path:
            target_path = str(Path(qt_install_path) / f"android_{platform_data.qt_plat_name}")
            qt_host_install_path = str(Path(explicit_qt_host_path).expanduser().resolve())
        elif coin:
            target_path = str(Path(qt_install_path) / "target")
            qt_host_install_path = qt_install_path
        else:
            target_path = str(Path(qt_install_path) / f"android_{platform_data.qt_plat_name}")
            qt_host_install_path = str(Path(qt_install_path) / host_qt_install_suffix)

        # run the cross compile script
        logging.info(f"Running Qt for Python cross-compile for platform {platform_data.plat_name}")
        qfp_ccompile_cmd = [sys.executable, "setup.py", "bdist_wheel",
                            f"--parallel={args.parallel}",
                            "--standalone",
                            f"--cmake-toolchain-file={str(qfp_toolchain.resolve())}",
                            f"--qt-host-path={qt_host_install_path}",
                            f"--plat-name=android_{platform_data.plat_name}",
                            f"--python-target-path={python_path}",
                            f"--qt-target-path={target_path}",
                            "--no-qt-tools"]
        if args.limited_api:
            qfp_ccompile_cmd.append(f"--limited-api={args.limited_api}")
        if args.dist_dir:
            qfp_ccompile_cmd.append(f"--dist-dir={str(args.dist_dir)}")
        run_command(qfp_ccompile_cmd, cwd=pyside_setup_dir, dry_run=dry_run, show_stdout=True)
