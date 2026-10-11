"""按配置模型重新生成插件根目录的默认 ``config.toml``。

配置模型（``switcher/config.py``）改动后跑一次，保持默认配置文件与模型同步。

用法（工作目录随意）：
    <MaiBot>/python-env/python.exe plugins/persona-switcher/tools/generate_default_config.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import tomlkit

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from mortis.config import MortisConfig  # noqa: E402


def main() -> None:
    """把配置模型的默认值写成插件根目录的 config.toml。"""

    config = MortisConfig()
    document = tomlkit.dumps(config.model_dump(mode="python"))
    target = PLUGIN_DIR / "config.toml"
    target.write_text(document, encoding="utf-8")
    print(f"written: {target} ({len(document)} chars)")


if __name__ == "__main__":
    main()
