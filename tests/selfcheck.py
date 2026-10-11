"""mai is mortis 自检脚本（不依赖 pytest，可直接用宿主解释器运行）。

用法：
    <MaiBot>/python-env/python.exe plugins/mai-is-mortis/tests/selfcheck.py

覆盖四段：

1. 计分与状态：加分、衰减（含宽限期与上限）、切换、到期、落盘往返；
2. 人格档案与判定解析：判定条件/加分/阈值的取值、编号解析、判定提示词；
3. Hook 与指令：消息 → 模型判定 → 加分 → 切换 → 提示词替换 → 指令 → 到期停止改写；
4. 判定节流与异常：抖动等待合并连发消息、判定冷却、模型调用失败不炸。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from mortis.config import PersonaEntry  # noqa: E402
from mortis.judge import build_judge_prompt, build_profiles, judged_profiles, parse_verdict  # noqa: E402
from mortis.state import ScoreBoard  # noqa: E402


def load_plugin_class() -> Any:
    """按宿主的方式加载 ``plugin.py``：以插件目录作为包的搜索路径。

    宿主用 ``spec_from_file_location(..., submodule_search_locations=[插件目录])`` 加载插件，
    插件里的相对导入才成立。这里照同样的方式加载，等于顺带验证了相对导入没问题。

    Returns:
        Any: 插件主类。
    """

    module_name = "mai_is_mortis_plugin_selfcheck"
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
    return module.MaiIsMortisPlugin


MaiIsMortisPlugin = load_plugin_class()

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
    """伪造的 ``ctx.llm``：按脚本依次返回判定结果，并记录每次的提示词。"""

    def __init__(self, responses: Sequence[Any] = ("none",)) -> None:
        self.responses: List[Any] = list(responses)
        self.prompts: List[str] = []

    async def generate(self, prompt: Any, **kwargs: Any) -> Dict[str, Any]:
        self.prompts.append(str(prompt))
        response = self.responses.pop(0) if self.responses else "none"
        if isinstance(response, dict):
            return response
        return {"success": True, "response": response}


class FakePaths:
    """伪造的 ``ctx.paths``。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.runtime_dir = data_dir


class FakeContext:
    """伪造的插件上下文，只提供插件真正用到的那几个入口。"""

    def __init__(self, data_dir: Path) -> None:
        self.logger = logging.getLogger("mai_is_mortis.selfcheck")
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


async def drain_judgements(plugin: Any, timeout: float = 3.0) -> None:
    """等待插件的后台判定任务跑完（含积压消息触发的补判）。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        running = [task for task in list(plugin._judge_tasks.values()) if not task.done()]
        if not running and not plugin._judge_pending:
            return
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        await asyncio.sleep(0.02)


def apply_evaluation_config(plugin: Any, **overrides: Any) -> None:
    """在默认配置上打补丁（自检里用很小的抖动/冷却，避免真的等几秒）。"""

    config_data = plugin.get_default_config()
    config_data["evaluation"]["debounce_seconds"] = 0.05
    config_data["evaluation"]["cooldown_seconds"] = 0.05
    config_data["evaluation"]["min_text_length"] = 1
    config_data["switching"]["save_interval_seconds"] = 0.0
    config_data["evaluation"].update(overrides)
    plugin.set_plugin_config(config_data)
    plugin._apply_config()


def check_score_board(checker: Checker, data_dir: Path) -> None:
    """校验计分、衰减、到期与落盘。"""

    print("\n[1] 计分与状态")
    board = ScoreBoard(data_dir / "scores.json", max_sessions=10)
    board.configure(decay_per_minute=6.0, grace_minutes=1.0, score_cap=50.0, max_sessions=10)
    now = 1_000_000.0

    board.add_gain("s1", "sharp_tongue", 20.0, now)
    checker.expect_close(board.effective_score("s1", "sharp_tongue", now), 20.0, "加分后立即读取等于加分值")
    checker.expect_close(
        board.effective_score("s1", "sharp_tongue", now + 60.0), 20.0, "宽限期（1 分钟）内分数不掉"
    )
    checker.expect_close(
        board.effective_score("s1", "sharp_tongue", now + 120.0), 14.0, "宽限期后每分钟衰减 6 分"
    )
    checker.expect_close(
        board.effective_score("s1", "sharp_tongue", now + 600.0), 0.0, "分数衰减到 0 后不再下降"
    )

    board.add_gain("s1", "sharp_tongue", 999.0, now + 700.0)
    checker.expect_close(
        board.effective_score("s1", "sharp_tongue", now + 700.0), 50.0, "分数被 score_cap 截断"
    )

    board.activate("s1", "sharp_tongue", now, 2.0)
    checker.expect(
        board.active_persona("s1", now + 60.0)[0] == "sharp_tongue", "激活后 1 分钟内人格仍然生效"
    )
    checker.expect_close(board.remaining_seconds("s1", now + 60.0), 60.0, "剩余时间按到期时间计算", tolerance=0.5)
    active_persona_id, expired_persona_id = board.active_persona("s1", now + 121.0)
    checker.expect(
        active_persona_id == "" and expired_persona_id == "sharp_tongue", "到期后自动失效并报告到期人格"
    )

    board.clear_scores("s2")
    checker.expect_close(board.effective_score("s2", "sharp_tongue", now), 0.0, "清零人格分数（A）")
    checker.expect_close(board.effective_score("s2", "sweet", now), 0.0, "清零人格分数（B）")

    board.activate("s3", "sweet", now, 5.0)
    board.add_gain("s3", "sweet", 12.0, now)
    checker.expect(board.judgement_ready("s3", now, 10.0) is True, "从未判定过时允许判定")
    board.mark_judged("s3", now)
    checker.expect(board.judgement_ready("s3", now + 5.0, 10.0) is False, "判定冷却期内不再判定")
    checker.expect(board.judgement_ready("s3", now + 11.0, 10.0) is True, "冷却结束后恢复判定")

    board.save(now)
    reloaded = ScoreBoard(data_dir / "scores.json", max_sessions=10)
    reloaded.configure(decay_per_minute=6.0, grace_minutes=1.0, score_cap=50.0, max_sessions=10)
    reloaded.load()
    checker.expect_close(reloaded.effective_score("s3", "sweet", now + 60.0), 12.0, "重启后分数还在")
    checker.expect(reloaded.active_persona("s3", now + 60.0)[0] == "sweet", "重启后临时人格与剩余时间还在")


def check_profiles_and_parser(checker: Checker, plugin: Any) -> None:
    """校验人格档案编译、编号解析与判定提示词。"""

    print("\n[2] 人格档案与判定解析")
    logger = logging.getLogger("mai_is_mortis.selfcheck")
    profiles = build_profiles(plugin.config.personas.personas, logger)
    checker.expect(list(profiles.keys()) == ["sharp_tongue", "sweet"], "按配置顺序编译出两个人格")
    sharp = profiles["sharp_tongue"]
    checker.expect(sharp.judged and sharp.auto_switchable, "有判定条件且加分非 0 的人格参与自动切换")
    checker.expect("骂" in sharp.criteria, "判定条件原文可用")
    checker.expect_close(sharp.weight, 12.0, "命中加分读取正确")

    broken = build_profiles(
        [
            PersonaEntry(id="", name="没 ID"),
            PersonaEntry(id="dup", name="第一次", criteria="a", weight=1.0),
            PersonaEntry(id="dup", name="第二次", criteria="b", weight=1.0),
            PersonaEntry(id="no_criteria", name="没条件", criteria="", weight=5.0),
        ],
        logger,
    )
    checker.expect(list(broken.keys()) == ["dup", "no_criteria"], "空 ID 与重复 ID 被剔除")
    checker.expect(not broken["no_criteria"].auto_switchable, "没有判定条件的人格不参与自动切换")
    checker.expect(len(judged_profiles(broken)) == 1, "参与判定的人格只算有条件的那些")

    checker.expect(parse_verdict("none", 2) == [], "模型回答 none 时不加分")
    checker.expect(parse_verdict("1", 2) == [0], "模型回答编号 1 命中第一个条件")
    checker.expect(parse_verdict("1, 2", 2) == [0, 1], "多个编号都算命中")
    checker.expect(parse_verdict("2", 2) == [1], "编号越界按条件数量裁剪")
    checker.expect(parse_verdict("3", 2) == [], "超出范围的编号被忽略")
    checker.expect(parse_verdict("没有满足的条件", 2) == [], "中文否认也算不命中")

    prompt = build_judge_prompt(
        bot_name="麦麦",
        criteria_list=["对方在骂麦麦", "对方在夸麦麦"],
        history=[("群友", "你行不行啊"), ("麦麦", "行"), ("群友", "菜")],
        latest_is_mention=True,
    )
    checker.expect("1. 对方在骂麦麦" in prompt and "2. 对方在夸麦麦" in prompt, "提示词里带编号清单")
    checker.expect("最后一条消息@ 了麦麦" in prompt, "提示词里告诉模型是否 @ 了麦麦")
    checker.expect("群友：菜" in prompt, "提示词里带上最近的对话")


async def check_hooks(checker: Checker, plugin: Any) -> None:
    """校验消息 → 判定 → 加分 → 切换 → 提示词替换 → 指令的端到端行为。"""

    print("\n[3] Hook 与指令")
    apply_evaluation_config(plugin)
    await plugin.on_load()
    llm: FakeLLMCapability = plugin.ctx.llm

    # 连发三条消息只判一次（抖动等待把一批消息并成一次调用）
    llm.responses = ["1"]
    for text in ("你个废物", "真服了", "有完没完"):
        await plugin.handle_incoming(message=build_message(text))
    await drain_judgements(plugin)
    checker.expect(len(llm.prompts) == 1, "一批连发消息只调一次判定", f"实际 {len(llm.prompts)} 次")
    status = plugin._format_status("session-a", time.time())
    checker.expect("嘴臭人格：12.0/30" in status, "判定命中后按命中加分累加", status)
    checker.expect("当前：原人格" in status, "分数没满时仍然使用原人格", status)

    # 再判两次到阈值 → 切换
    llm.responses = ["1", "1"]
    await plugin.handle_incoming(message=build_message("你就是个笑话"))
    await drain_judgements(plugin)
    await plugin.handle_incoming(message=build_message("继续啊"))
    await drain_judgements(plugin)
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
            patched_items[1]["parts"][0]["text"] == items[1]["parts"][0]["text"], "其他 Item 原样保留"
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

    # 生效期间不再判定（省 token）
    calls_before = len(llm.prompts)
    await plugin.handle_incoming(message=build_message("还在骂"))
    await drain_judgements(plugin)
    checker.expect(len(llm.prompts) == calls_before, "临时人格生效期间不再调模型判定")

    # 没触发过的会话不受影响
    result_other = await plugin.patch_replyer_request(items=build_replyer_items(), session_id="session-other")
    checker.expect("modified_kwargs" not in result_other, "其他会话不受该临时人格影响", str(result_other))

    # 手动切换与重置
    allowed = await plugin.handle_persona_command(
        stream_id="session-a",
        matched_groups={"action": "切换", "argument": "sweet"},
        is_local_operator=True,
        user_id="1",
    )
    checker.expect("已手动切换到「撒娇人格」" in allowed[1], "本机操作者可以手动切换人格", allowed[1])
    denied = await plugin.handle_persona_command(
        stream_id="session-a", matched_groups={"action": "重置"}, is_local_operator=False, user_id="999"
    )
    checker.expect("只有管理员" in denied[1], "普通用户不能重置", denied[1])
    unknown = await plugin.handle_persona_command(
        stream_id="session-a",
        matched_groups={"action": "切换", "argument": "不存在"},
        is_local_operator=True,
        user_id="1",
    )
    checker.expect("没有找到人格" in unknown[1], "切换到不存在的人格时给出可用列表", unknown[1])
    reset = await plugin.handle_persona_command(
        stream_id="session-a", matched_groups={"action": "重置"}, is_local_operator=True, user_id="1"
    )
    checker.expect("已清零本会话的人格分数" in reset[1], "重置会清分并回到原人格", reset[1])

    # 到期后不再改写
    board = plugin._board
    assert board is not None
    board.activate("session-a", "sharp_tongue", time.time() - 10 * 60.0, 1.0)
    expired_result = await plugin.patch_replyer_request(items=build_replyer_items(), session_id="session-a")
    checker.expect("modified_kwargs" not in expired_result, "临时人格到期后不再改写提示词", str(expired_result))
    checker.expect("当前：原人格" in plugin._format_status("session-a", time.time()), "到期后回到原人格")


async def check_throttle_and_failure(checker: Checker, plugin: Any) -> None:
    """校验判定冷却、抖动合并与模型失败时的行为。"""

    print("\n[4] 判定节流与异常")
    llm: FakeLLMCapability = plugin.ctx.llm

    # 抖动等待内连发的多条消息只算一次判定
    apply_evaluation_config(plugin, debounce_seconds=0.2)
    llm.responses = ["none"]
    llm.prompts.clear()
    for text in ("在吗", "看这个", "算了"):
        await plugin.handle_incoming(message=build_message(text, session_id="session-debounce"))
    await drain_judgements(plugin)
    checker.expect(len(llm.prompts) == 1, "抖动窗口内连发的消息合并成一次判定", f"实际 {len(llm.prompts)} 次")

    # 冷却期内不再判定，只记下「有更新」
    apply_evaluation_config(plugin, debounce_seconds=0.0, cooldown_seconds=30.0)
    llm.responses = ["none"] * 5
    llm.prompts.clear()
    await plugin.handle_incoming(message=build_message("第一条", session_id="session-cooldown"))
    await drain_judgements(plugin)
    first_calls = len(llm.prompts)
    for text in ("第二条", "第三条"):
        await plugin.handle_incoming(message=build_message(text, session_id="session-cooldown"))
    await asyncio.sleep(0.15)
    checker.expect(first_calls == 1, "冷却前正常判定一次", f"实际 {first_calls} 次")
    checker.expect(len(llm.prompts) == 1, "冷却期内不再调模型", f"实际 {len(llm.prompts)} 次")
    checker.expect("session-cooldown" in plugin._judge_pending, "冷却期内的新消息被记为待判定")

    # 模型调用失败：不炸、不加分
    apply_evaluation_config(plugin, debounce_seconds=0.0, cooldown_seconds=0.0)
    llm.responses = [{"success": False, "error": "boom"}]
    await plugin.handle_incoming(message=build_message("测试失败", session_id="session-fail"))
    await drain_judgements(plugin)
    status = plugin._format_status("session-fail", time.time())
    checker.expect("嘴臭人格：0.0/30" in status, "模型调用失败时不加分", status)

    # 模型返回空内容：同样不加分
    llm.responses = [{"success": True, "response": "   "}]
    await plugin.handle_incoming(message=build_message("测试空回复", session_id="session-empty"))
    await drain_judgements(plugin)
    status = plugin._format_status("session-empty", time.time())
    checker.expect("嘴臭人格：0.0/30" in status, "模型返回空内容时不加分", status)


async def main() -> int:
    """运行全部自检。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # 数据写在系统临时目录里：避免在插件目录下产生文件而被宿主的插件热重载监听扫到
    data_dir = Path(tempfile.mkdtemp(prefix="mai_is_mortis_selfcheck_"))
    print(f"自检数据目录: {data_dir}")

    checker = Checker()
    plugin = MaiIsMortisPlugin()
    plugin._set_context(FakeContext(data_dir))
    plugin.set_plugin_config(plugin.get_default_config())

    check_score_board(checker, data_dir)
    check_profiles_and_parser(checker, plugin)
    await check_hooks(checker, plugin)
    await check_throttle_and_failure(checker, plugin)
    await plugin.on_unload()

    print(f"\n共 {checker.checks} 项检查，失败 {len(checker.failures)} 项")
    for failure in checker.failures:
        print(f"  - {failure}")
    return 1 if checker.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
