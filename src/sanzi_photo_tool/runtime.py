from __future__ import annotations

import sys
import os
from pathlib import Path


APP_NAME = "三资辅助软件"
APP_VERSION = "1.2"
APP_VERSION_FULL = "1.2.0"


def is_compiled() -> bool:
    """判断当前是否运行在 Nuitka 编译后的程序中。"""
    return "__compiled__" in globals()


def application_dir() -> Path:
    """返回可执行程序目录；源码运行时返回项目根目录。"""
    if is_compiled():
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parents[2]


def package_resource(*parts: str) -> Path:
    """返回随 Python 包分发的资源路径。"""
    return Path(__file__).resolve().parent.joinpath(*parts)


def application_resource(filename: str) -> Path:
    """返回安装目录中的网页资源，兼容 wheel 内置资源。"""
    external = application_dir() / filename
    if external.exists():
        return external
    return package_resource("web", filename)


def user_data_dir() -> Path:
    """返回当前用户可写的数据目录。"""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    directory = base / APP_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory
