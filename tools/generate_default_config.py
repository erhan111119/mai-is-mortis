"""从配置模型生成可提交的配置模板 ``config.toml.example``。

配置模型（``mortis/config.py``）改动后跑一次，保持模板与模型同步。
真正的 ``config.toml`` 不入库：宿主首次加载插件时会按同一个配置模型自动生成，
WebUI 保存配置时也会重写它。

用法（工作目录随意）：
    <MaiBot>/python-env/python.exe plugins/mai-is-mortis/tools/generate_default_config.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import tomlkit

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from mortis.config import MortisConfig  # noqa: E402

HEADER = """# ============================================================================
# mai is mortis（麦麦是多首的怪物）配置模板
#
# 复制成 config.toml 后按需修改；不复制也可以——宿主首次加载插件时会用同样的
# 默认值自动生成 config.toml，之后再在 WebUI 里改。
# 本文件由 tools/generate_default_config.py 从 mortis/config.py 的配置模型生成，请勿手改。
#
# 列一下常用的几个：详细日志（调参时开）、每分钟衰减 / 衰减宽限分钟（分数回落速度）、
# 判定冷却秒（省 token）、以及每个人格的「判定条件 / 命中加分 / 切换阈值 / 维持分钟」。
# ============================================================================

"""


def main() -> None:
    """把配置模型的默认值写成插件根目录的 config.toml.example。"""

    config = MortisConfig()
    document = HEADER + tomlkit.dumps(config.model_dump(mode="python"))
    target = PLUGIN_DIR / "config.toml.example"
    target.write_text(document, encoding="utf-8")
    print(f"written: {target} ({len(document)} chars)")


if __name__ == "__main__":
    main()
