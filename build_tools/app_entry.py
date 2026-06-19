from __future__ import annotations

import ctypes
import os
import traceback
from pathlib import Path


if __name__ == "__main__":
    try:
        from sanzi_photo_tool.main import main

        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        error_dir = (
            Path(os.environ.get("LOCALAPPDATA", Path.home()))
            / "三资辅助软件"
        )
        error_dir.mkdir(parents=True, exist_ok=True)
        error_file = error_dir / "启动错误.log"
        error_file.write_text(traceback.format_exc(), encoding="utf-8")
        ctypes.windll.user32.MessageBoxW(
            0,
            f"软件启动失败，错误详情已保存到：\n{error_file}",
            "三资辅助软件",
            0x10,
        )
        raise
