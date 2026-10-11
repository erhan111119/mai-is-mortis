"""人格切换插件自检脚本（不依赖 pytest，可直接用宿主解释器运行）。

用法：
    <MaiBot>/python-env/python.exe plugins/persona-switcher/tests/selfcheck.py

覆盖两段：

1. 计分与状态：加分、衰减（含宽限期与上限）、冷却、切换、到期、落盘往返；
2. Hook 与指令：骂人触发切换、其他人格清零、回复提示词被替换、规划器行为风格被替换、
   额外要求被并入、指令状态文本、到期后停止改写。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from switcher.state import ScoreBoard  # noqa: E402
from switcher.triggers import compile_personas, match_message  # noqa: E402


def load_plugin_class() -> Any:
    """按宿主的方式加载 ``plugin.py``：以插件目录作为包的搜索路径。

    宿主用 ``spec_from_file_location(..., submodule_search_locations=[插件目录])`` 加载插件，
    插件里的相对导入才成立。这里照同样的方式加载，等于顺带验证了相对导入没问题。

    Returns:
        Any: 插件主类。
    """

    module_name = "persona_switcher_plugin_selfcheck"
    spec = importlib.util.spec_from_file_location(
        module_name,
        str(PLUGIN_DIR / "plugin.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法为 plugin.py 构造模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.PersonaSwitcherPlugin


PersonaSwitcherPlugin = load_plugin_class()

BASE_PERSONALITY = "是一个大二女大学生，现在正在上网和群友聊天。"
BASE_REPLY_STYLE = "你的风格平淡简短，不爱用语气词。"
BASE_BEHAVIOR_STYLE = "正常参与群聊，不主动挑事。"


class FakeConfigCapability:
    """伪造的 ``ctx.config``：按点号路径读一份固定配置。"""

    def __init__(self, values: Dict[str, Any]) -> None:
        self._values = values

    async def get(self, key: str, default: Any = None) -> Any:
        current: Any = self._values
        for part in key.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current


class FakeLLMCapability:
    """伪造的 ``ctx.llm``：固定回答「第 1 个条件命中」。"""

    def __init__(self, response: str = "1") -> None:
        self._response = response
        self.calls: List[str] = []

    async def generate(self, prompt: Any, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(str(prompt))
        return {"success": True, "response": self._response}


class FakePaths:
    """伪造的 ``ctx.paths``。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.runtime_dir = data_dir


class FakeContext:
    """伪造的插件上下文，只提供插件真正用到的那几个入口。"""

    def __init__(self, data_dir: Path) -> None:
        self.logger = logging.getLogger("persona_switcher.selfcheck")
        self.paths = FakePaths(data_dir)
        self.config = FakeConfigCapability(
            {
                "personality": {
                    "personality": BASE_PERSONALITY,
                    "reply_style": BASE_REPLY_STYLE,
                    "behavior_style": BASE_BEHAVIOR_STYLE,
                },
                "bot": {"nickname": "麦麦"},
            }
        )
        self.llm = FakeLLMCapability()


def build_replyer_items() -> List[Dict[str, Any]]:
    """构造一条与宿主 replyer 请求同形状的 Items。"""

    system_text = (
        f"你的名字是麦麦。\n{BASE_PERSONALITY}\n"
        "现在请你读读之前的聊天记录，把握当前的话题，然后给出日常且口语化的回复，\n"
        f"{BASE_REPLY_STYLE}\n你可以参考【回复信息参考】中的信息，但是视情况而定，不用完全遵守。"
    )
    return [
        {
            "item_type": "SystemMessageItem",
            "meta": {"item_id": "sys-1", "logical_turn_id": "turn-1", "timestamp": "2026-01-01T00:00:00+00:00"},
            "parts": [{"type": "text", "text": system_text}],
        },
        {
            "item_type": "UserMessageItem",
            "meta": {"item_id": "user-1", "logical_turn_id": "turn-1", "timestamp": "2026-01-01T00:00:01+00:00"},
            "parts": [{"type": "text", "text": "你在干嘛"}],
        },
    ]


def build_planner_items() -> List[Dict[str, Any]]:
    """构造一条与宿主 planner 请求同形状的 Items。"""

    system_text = f"你是麦麦。麦麦的行为风格：{BASE_BEHAVIOR_STYLE}\n现在请判断要不要回复。"
    return [
        {
            "item_type": "SystemMessageItem",
            "meta": {"item_id": "sys-p", "logical_turn_id": "turn-p", "timestamp": "2026-01-01T00:00:00+00:00"},
            "parts": [{"type": "text", "text": system_text}],
        }
    ]


def build_message(text: str, *, session_id: str = "session-a", is_at: bool = False) -> Dict[str, Any]:
    """构造一条与宿主 ``chat.receive.after_process`` 同形状的消息。"""

    return {
        "message_id": "m-1",
        "platform": "qq",
        "session_id": session_id,
        "processed_plain_text": text,
        "is_command": text.startswith("/"),
        "is_at": is_at,
        "is_mentioned": is_at,
        "message_info": {"user_info": {"user_id": "10001", "user_nickname": "群友", "user_cardname": "群友"}},
    }


class Checker:
    """极简断言器，失败时打印上下文并汇总。"""

    def __init__(self) -> None:
        self.failures: List[str] = []
        self.checks = 0

    def expect(self, condition: bool, description: str, detail: str = "") -> None:
        self.checks += 1
        if condition:
            print(f"  ok   {description}")
            return
        self.failures.append(f"{description} | {detail}")
        print(f"  FAIL {description} | {detail}")

    def expect_close(self, actual: float, expected: float, description: str, tolerance: float = 0.01) -> None:
        self.expect(abs(actual - expected) <= tolerance, description, f"实际 {actual:.4f}，期望 {expected:.4f}")


def check_score_board(checker: Checker, data_dir: Path) -> None:
    """校验计分、衰减、冷却、切换与落盘。"""

    print("\n[1] 计分与状态")
    board = ScoreBoard(data_dir / "scores.json", max_sessions=10)
    board.configure(decay_per_minute=6.0, grace_minutes=1.0, score_cap=50.0, max_sessions=10)
    now = 1_000_000.0

    board.add_gain("s1", "sharp_tongue", 20.0, now)
    checker.expect_close(board.effective_score("s1", "sharp_tongue", now), 20.0, "加分后立即读取等于加分值")

    # 宽限期内不衰减
    checker.expect_close(
        board.effective_score("s1", "sharp_tongue", now + 60.0),
        20.0,
        "宽限期（1 分钟）内分数不掉",
    )
    # 宽限期后按每分钟 6 分衰减
    checker.expect_close(
        board.effective_score("s1", "sharp_tongue", now + 120.0),
        14.0,
        "宽限期后每分钟衰减 6 分",
    )
    # 衰减到 0 就停住，不会变负
    checker.expect_close(
        board.effective_score("s1", "sharp_tongue", now + 600.0),
        0.0,
        "分数衰减到 0 后不再下降",
    )

    # 上限生效
    board.add_gain("s1", "sharp_tongue", 999.0, now + 700.0)
    checker.expect_close(
        board.effective_score("s1", "sharp_tongue", now + 700.0),
        50.0,
        "分数被 score_cap 截断",
    )

    # 冷却
    board.mark_cooldown("s1", "sharp_tongue#1", now + 700.0, 5.0)
    checker.expect(
        board.is_cooling_down("s1", "sharp_tongue#1", now + 702.0),
        "冷却期内同一条规则不再加分",
    )
    checker.expect(
        not board.is_cooling_down("s1", "sharp_tongue#1", now + 706.0),
        "冷却结束后规则恢复可用",
    )

    # 激活与到期
    board.activate("s1", "sharp_tongue", now, 2.0)
    checker.expect(
        board.active_persona("s1", now + 60.0)[0] == "sharp_tongue",
        "激活后 1 分钟内人格仍然生效",
    )
    checker.expect_close(board.remaining_seconds("s1", now + 60.0), 60.0, "剩余时间按到期时间计算", tolerance=0.5)
    active_persona_id, expired_persona_id = board.active_persona("s1", now + 121.0)
    checker.expect(
        active_persona_id == "" and expired_persona_id == "sharp_tongue",
        "到期后自动失效并报告到期人格",
    )

    # 其他人格清零
    board.add_gain("s2", "sharp_tongue", 10.0, now)
    board.add_gain("s2", "sweet", 8.0, now)
    board.clear_scores("s2")
    checker.expect_close(board.effective_score("s2", "sharp_tongue", now), 0.0, "清零其他人格分数（A）")
    checker.expect_close(board.effective_score("s2", "sweet", now), 0.0, "清零其他人格分数（B）")

    # 落盘往返
    board.activate("s3", "sweet", now, 5.0)
    board.add_gain("s3", "sweet", 12.0, now)
    board.save(now)
    reloaded = ScoreBoard(data_dir / "scores.json", max_sessions=10)
    reloaded.configure(decay_per_minute=6.0, grace_minutes=1.0, score_cap=50.0, max_sessions=10)
    reloaded.load()
    checker.expect_close(reloaded.effective_score("s3", "sweet", now + 60.0), 12.0, "重启后分数还在")
    checker.expect(
        reloaded.active_persona("s3", now + 60.0)[0] == "sweet",
        "重启后临时人格与剩余时间还在",
    )


def check_triggers(checker: Checker, plugin: PersonaSwitcherPlugin) -> None:
    """校验触发规则的编译与匹配。"""

    print("\n[2] 触发规则")
    now = 1_000_000.0
    personas = compile_personas(plugin.config.personas.personas, logging.getLogger("persona_switcher.selfcheck"))
    hits = match_message(personas, "你个智障", is_mention=False)
    checker.expect(
        [hit.persona_id for hit in hits] == ["sharp_tongue"],
        "骂人话命中嘴臭人格的大权重规则",
        f"实际命中 {[hit.persona_id for hit in hits]}",
    )
    hits = match_message(personas, "麦麦好可爱", is_mention=False)
    checker.expect(
        hits == [],
        "没 @ 麦麦时撒娇规则不命中（require_mention）",
        f"实际命中 {[hit.persona_id for hit in hits]}",
    )
    hits = match_message(personas, "麦麦好可爱", is_mention=True)
    checker.expect(
        [hit.persona_id for hit in hits] == ["sweet"],
        "@ 麦麦后撒娇规则命中",
        f"实际命中 {[hit.persona_id for hit in hits]}",
    )
    checker.expect(now == 1_000_000.0, "匹配过程不依赖时间")


async def check_hooks(checker: Checker, plugin: PersonaSwitcherPlugin, data_dir: Path) -> None:
    """校验三个 Hook 与指令的端到端行为。"""

    print("\n[3] Hook 与指令")
    await plugin.on_load()

    # (a) 一句话同时命中两条规则时，两条规则的权重都要算进这一条消息
    await plugin.handle_incoming(message=build_message("你个废物，真蠢"))
    status = plugin._format_status("session-a", time.time())
    checker.expect("嘴臭人格：20.0/30" in status, "同一条消息命中多条规则时权重累加", status)

    # (b) 把冷却全部关掉，验证加分累积与阈值切换（避免自检为了等冷却而真的睡几秒）
    config_data = plugin.get_default_config()
    for persona in config_data["personas"]["personas"]:
        persona["cooldown_seconds"] = 0.0
        for trigger in persona["triggers"]:
            trigger["cooldown_seconds"] = 0.0
    plugin.set_plugin_config(config_data)
    plugin._apply_config()

    for text in ("你有点蠢", "你个废物", "你这人真是垃圾"):
        await plugin.handle_incoming(message=build_message(text))
    status = plugin._format_status("session-a", time.time())
    checker.expect("当前：嘴臭人格" in status, "分数顶到阈值后切换人格", status)
    checker.expect("嘴臭人格：0.0/30" in status, "切换时把分数清零", status)

    # 生效期间回复提示词被替换
    items = build_replyer_items()
    result = await plugin.patch_replyer_request(items=items, session_id="session-a", extra_prompt="")
    patched_items = result.get("modified_kwargs", {}).get("items")
    checker.expect(patched_items is not None, "临时人格生效时改写回复提示词", f"返回值 {result}")
    if patched_items:
        patched_text = patched_items[0]["parts"][0]["text"]
        checker.expect("嘴臭" in patched_text, "系统提示换成了嘴臭人格的设定", patched_text[:120])
        checker.expect(BASE_PERSONALITY not in patched_text, "原人格文本已被替换掉", patched_text[:120])
        checker.expect("你行你上" in patched_text, "说话风格也被替换", patched_text[:200])
        checker.expect(
            patched_items[1]["parts"][0]["text"] == items[1]["parts"][0]["text"],
            "其他 Item 原样保留",
        )
        checker.expect(items[0]["parts"][0]["text"] != patched_text, "不修改调用方原本的 Items")

    # 额外要求
    requirement_result = await plugin.append_reply_requirement(session_id="session-a", extra_prompt="原有要求")
    merged = requirement_result.get("modified_kwargs", {}).get("extra_prompt")
    checker.expect(
        isinstance(merged, str) and "原有要求" in merged and "临时人格" in merged,
        "人格的额外要求被并进 extra_prompt，且不覆盖原有内容",
        str(merged),
    )

    # 规划器行为风格
    planner_result = await plugin.patch_planner_request(items=build_planner_items(), session_id="session-a")
    planner_items = planner_result.get("modified_kwargs", {}).get("items")
    checker.expect(planner_items is not None, "规划器提示词被改写", f"返回值 {planner_result}")
    if planner_items:
        planner_text = planner_items[0]["parts"][0]["text"]
        checker.expect(BASE_BEHAVIOR_STYLE not in planner_text, "规划器行为风格已被替换", planner_text[:200])
        checker.expect("直接怼回去" in planner_text, "规划器里写入了临时人格的行为风格", planner_text[:200])

    # 没触发过骂人的会话不受影响
    result_other = await plugin.patch_replyer_request(items=build_replyer_items(), session_id="session-other")
    checker.expect(
        "modified_kwargs" not in result_other,
        "其他会话不受该临时人格影响",
        str(result_other),
    )

    # 手动切换与重置
    allowed = await plugin.handle_persona_command(
        stream_id="session-a",
        matched_groups={"action": "切换", "argument": "sweet"},
        is_local_operator=True,
        user_id="1",
    )
    checker.expect("已手动切换到「撒娇人格」" in allowed[1], "本机操作者可以手动切换人格", allowed[1])
    denied = await plugin.handle_persona_command(
        stream_id="session-a",
        matched_groups={"action": "重置"},
        is_local_operator=False,
        user_id="999",
    )
    checker.expect("只有管理员" in denied[1], "普通用户不能重置", denied[1])
    reset = await plugin.handle_persona_command(
        stream_id="session-a",
        matched_groups={"action": "重置"},
        is_local_operator=True,
        user_id="1",
    )
    checker.expect("已清零本会话的人格分数" in reset[1], "重置会清分并回到原人格", reset[1])

    # 到期后不再改写
    board = plugin._board
    assert board is not None
    board.activate("session-a", "sharp_tongue", time.time() - 10 * 60.0, 1.0)
    expired_result = await plugin.patch_replyer_request(items=build_replyer_items(), session_id="session-a")
    checker.expect(
        "modified_kwargs" not in expired_result,
        "临时人格到期后不再改写提示词",
        str(expired_result),
    )
    checker.expect("当前：原人格" in plugin._format_status("session-a", time.time()), "到期后回到原人格")

    # (c) 规则级冷却
    config_data = plugin.get_default_config()
    for persona in config_data["personas"]["personas"]:
        persona["cooldown_seconds"] = 0.0
    plugin.set_plugin_config(config_data)
    plugin._apply_config()
    for _ in range(2):
        await plugin.handle_incoming(message=build_message("你有点蠢", session_id="session-rule-cd"))
    status = plugin._format_status("session-rule-cd", time.time())
    checker.expect("嘴臭人格：6.0/30" in status, "规则冷却内同一条规则只加一次分", status)

    # (d) 人格级冷却：冷却期内换一条规则也不加分（默认人格冷却 2 秒）
    config_data = plugin.get_default_config()
    plugin.set_plugin_config(config_data)
    plugin._apply_config()
    await plugin.handle_incoming(message=build_message("你有点蠢", session_id="session-persona-cd"))
    await plugin.handle_incoming(message=build_message("你个废物", session_id="session-persona-cd"))
    status = plugin._format_status("session-persona-cd", time.time())
    checker.expect("嘴臭人格：6.0/30" in status, "人格冷却内换规则也不加分", status)


async def check_llm_judgement(checker: Checker, plugin: PersonaSwitcherPlugin) -> None:
    """校验可选的模型判定链路（用假 LLM 顶替真实调用）。"""

    print("\n[4] 模型判定")
    config_data = plugin.get_plugin_config_data()
    config_data["evaluation"]["enabled"] = True
    config_data["evaluation"]["cooldown_seconds"] = 0.0
    plugin.set_plugin_config(config_data)
    plugin._apply_config()

    board = plugin._board
    assert board is not None
    board.reset("session-judge")
    board.mark_judged("session-judge", 0.0)
    await plugin.handle_incoming(message=build_message("你今天有点怪怪的", session_id="session-judge"))
    for task in list(plugin._judge_tasks):
        await task
    status = plugin._format_status("session-judge", time.time())
    checker.expect("嘴臭人格：12.0/30" in status, "模型判定命中后按 llm_weight 加分", status)
    checker.expect(len(plugin.ctx.llm.calls) == 1, "一次判定只调一次模型", f"实际 {len(plugin.ctx.llm.calls)} 次")

    config_data["evaluation"]["enabled"] = False
    plugin.set_plugin_config(config_data)
    plugin._apply_config()


async def main() -> int:
    """运行全部自检。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # 数据写在系统临时目录里：避免在插件目录下产生文件而被宿主的插件热重载监听扫到
    data_dir = Path(tempfile.mkdtemp(prefix="persona_switcher_selfcheck_"))
    print(f"自检数据目录: {data_dir}")

    checker = Checker()
    plugin = PersonaSwitcherPlugin()
    plugin._set_context(FakeContext(data_dir))
    plugin.set_plugin_config(plugin.get_default_config())

    check_score_board(checker, data_dir)
    check_triggers(checker, plugin)
    await check_hooks(checker, plugin, data_dir)
    await check_llm_judgement(checker, plugin)

    print(f"\n共 {checker.checks} 项检查，失败 {len(checker.failures)} 项")
    for failure in checker.failures:
        print(f"  - {failure}")
    return 1 if checker.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
