"""把插件交给宿主自己的加载/校验代码验证，但不启动机器人。

用法（工作目录随意）：
    <MaiBot>/python-env/python.exe plugins/persona-switcher/tools/host_check.py

校验内容：
1. ``_manifest.json`` 能否通过宿主的 Manifest 校验（含 Host / SDK 版本区间与依赖）；
2. 按宿主的方式加载 ``plugin.py``（相对导入也要成立）并拿到 ``create_plugin()`` 实例；
3. 组件声明（Hook / Command）能否在宿主的组件注册中心注册（Hook 名、blocking 模式是否合法）；
4. WebUI 配置 Schema 能否生成、默认配置能否通过配置模型校验；
5. 改写后的请求 Item 能否被宿主的反序列化函数（含 ``validate_context_items``）吃下。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PLUGIN_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PLUGIN_DIR))


def check_manifest() -> str:
    """校验并打印 Manifest 结果。"""

    from src.plugin_runtime.runner.manifest_validator import ManifestValidator

    validator = ManifestValidator()
    manifest = validator.load_from_plugin_path(PLUGIN_DIR)
    if manifest is None:
        raise SystemExit(f"Manifest 校验失败: {validator.errors}")
    if validator.warnings:
        print(f"Manifest 警告: {validator.warnings}")
    print(f"[1] Manifest 通过: id={manifest.id} version={manifest.version} type={manifest.plugin_type}")
    print(f"    能力声明: {manifest.capabilities}")
    return manifest.id


def load_plugin():
    """按宿主 PluginLoader 的方式加载插件实例。"""

    module_name = "mai_is_mortis_host_check"
    spec = importlib.util.spec_from_file_location(
        module_name,
        str(PLUGIN_DIR / "plugin.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise SystemExit("无法为 plugin.py 构造模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    instance = module.create_plugin()
    print(f"[2] 插件加载成功: {type(instance).__name__}")
    return instance


def check_components(plugin_id: str, instance) -> None:
    """把组件喂给宿主的组件注册中心。"""

    from src.plugin_runtime.hook_catalog import register_builtin_hook_specs
    from src.plugin_runtime.host.component_registry import ComponentRegistry
    from src.plugin_runtime.host.hook_spec_registry import HookSpecRegistry

    hook_spec_registry = HookSpecRegistry()
    register_builtin_hook_specs(hook_spec_registry)
    registry = ComponentRegistry(hook_spec_registry=hook_spec_registry)

    declarations = [
        {
            "name": str(component.get("name", "") or ""),
            "component_type": str(component.get("type", "") or ""),
            "metadata": dict(component.get("metadata", {}) or {}),
        }
        for component in instance.get_components()
        if isinstance(component, dict)
    ]
    registered = registry.register_plugin_components(plugin_id, declarations)
    print(f"[3] 组件注册通过: {registered} 个")
    for declaration in declarations:
        metadata = declaration["metadata"]
        detail = metadata.get("hook") or metadata.get("command_pattern") or ""
        print(f"    - {declaration['component_type']:<12} {declaration['name']:<36} {detail}")


def _count_fields(node: object) -> int:
    """递归数出配置 Schema 里的字段条目数量。"""

    if isinstance(node, dict):
        if "key" in node or "name" in node:
            return 1
        return sum(_count_fields(value) for value in node.values())
    if isinstance(node, list):
        return sum(_count_fields(item) for item in node)
    return 0


def check_config_schema(instance) -> None:
    """生成 WebUI 配置 Schema 并打印配置节。"""

    schema = instance.build_config_schema(
        plugin_id="local.persona-switcher",
        plugin_name="人格切换（评分触发）",
        plugin_version="1.0.0",
        plugin_description="",
        plugin_author="MaiBot 本地插件",
    )
    if not schema:
        raise SystemExit("配置 Schema 为空")
    print(f"[4] 配置 Schema 生成成功: 顶层字段={list(schema.keys())}")
    raw_sections = schema.get("sections")
    if isinstance(raw_sections, dict):
        sections = [(key, value) for key, value in raw_sections.items() if isinstance(value, dict)]
    elif isinstance(raw_sections, list):
        sections = [("", value) for value in raw_sections if isinstance(value, dict)]
    else:
        sections = []
    for fallback_name, section in sections:
        label = section.get("label") or section.get("name") or fallback_name
        print(f"    - 配置节 {label}: {_count_fields(section)} 个字段")
    print(f"    personas 配置节结构: {json.dumps(schema['sections'].get('personas'), ensure_ascii=False)[:220]}...")

    default_config = instance.build_default_config()
    print(f"    默认配置节: {list(default_config.keys())}")
    print(f"    默认人格数: {len(default_config['personas']['personas'])}")
    print(f"    默认配置前 60 字: {json.dumps(default_config, ensure_ascii=False)[:60]}...")


def check_item_contract() -> None:
    """用宿主真实的 Item 序列化/反序列化函数，验证插件改写后的载荷能被吃下。

    这一步等价于宿主在 ``maisaka.replyer.before_model_request`` 之后做的事情：
    序列化 → 交给插件改 → 反序列化（含 ``validate_context_items`` 校验）。
    """

    from src.llm_models.payload_content.context_item import (
        CONTEXT_ITEM_SCHEMA_VERSION,
        ContextItemBuilder,
        RoleType,
    )
    from src.llm_models.payload_content.context_protocol import ContextProtocolMode
    from src.plugin_runtime.hook_payloads import deserialize_prompt_items, serialize_prompt_items

    from mortis.prompt_patch import BasePersonaTexts, PersonaTexts, patch_system_items

    original_items = [
        ContextItemBuilder()
        .set_role(RoleType.System)
        .add_text_content(
            "你的名字是麦麦。\n"
            "是一个大二女大学生，现在正在上网和群友聊天。\n"
            "现在请你读读之前的聊天记录，把握当前的话题，然后给出日常且口语化的回复，\n"
            "你的风格平淡简短，不爱用语气词。\n"
            "你可以参考【回复信息参考】中的信息，但是视情况而定，不用完全遵守。"
        )
        .build(),
        ContextItemBuilder().set_role(RoleType.User).add_text_content("你在干嘛").build(),
    ]
    payload = serialize_prompt_items(original_items)

    base = BasePersonaTexts(
        personality="是一个大二女大学生，现在正在上网和群友聊天。",
        reply_style="你的风格平淡简短，不爱用语气词。",
        bot_name="麦麦",
    )
    persona_texts = PersonaTexts(
        name="嘴臭人格",
        personality="你现在被惹毛了。",
        reply_style="短句、冲。",
        behavior_style="",
    )
    patched_payload, replaced = patch_system_items(payload, base, persona_texts)
    if not replaced:
        raise SystemExit("插件没有改写任何 Item，无法验证契约")

    restored_items = deserialize_prompt_items(
        patched_payload,
        item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION,
        mode=ContextProtocolMode.REQUEST_CONTEXT,
        original_items=original_items,
    )
    restored_text = restored_items[0].parts[0].text
    if "你现在被惹毛了" not in restored_text or "大二女大学生" in restored_text:
        raise SystemExit(f"反序列化后的人设文本不符合预期: {restored_text[:120]}")
    if restored_items[1].parts[0].text != "你在干嘛":
        raise SystemExit("未改动的 Item 没能原样保留")
    print(f"[5] Item 契约校验通过: {len(restored_items)} 个 Item，系统提示已替换且通过 validate_context_items")


def main() -> int:
    """依次跑完全部校验。"""

    plugin_id = check_manifest()
    instance = load_plugin()
    check_components(plugin_id, instance)
    check_config_schema(instance)
    check_item_contract()
    print("\n全部宿主侧校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
