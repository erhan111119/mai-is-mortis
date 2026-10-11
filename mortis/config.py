"""mai is mortis 的配置模型。

WebUI 依据它渲染配置页，Runner 依据它生成默认 ``config.toml``。

加分的唯一来源是**模型判定**：插件把最近的对话交给模型，模型按每个人格的「判定条件」回编号，
命中就按该人格的「命中加分」加分；分数顶到阈值切过去，维持一段时间后自动切回原人格。
"""

from maibot_sdk import Field, PluginConfigBase


class PersonaEntry(PluginConfigBase):
    """一个头（人格）：什么情况算它、分数阈值、生效时长，以及要替换掉的设定文本。"""

    id: str = Field(
        default="",
        description="人格标识，必须唯一，只用于日志、状态查询与手动切换",
        json_schema_extra={"label": "人格 ID", "placeholder": "例如 sharp_tongue"},
    )
    name: str = Field(
        default="",
        description="人格显示名，出现在日志与 /人格 指令的返回里",
        json_schema_extra={"label": "人格名称", "placeholder": "例如 嘴臭人格"},
    )
    criteria: str = Field(
        default="",
        description=(
            "判定条件：用一句话描述「对方做了什么」就算它，交给模型判断。"
            "留空表示这个人格不参与自动切换（仍可手动切）"
        ),
        json_schema_extra={"label": "判定条件"},
    )
    weight: float = Field(
        default=12.0,
        description="模型判定命中一次给该人格加多少分",
        json_schema_extra={"label": "命中加分", "min": 0.0, "step": 1.0},
    )
    threshold: float = Field(
        default=30.0,
        description="该人格的分数阈值，加到这么多分就切换过去；填 0 表示这个人格只手动用",
        json_schema_extra={"label": "切换阈值", "min": 0.0, "step": 1.0},
    )
    duration_minutes: float = Field(
        default=10.0,
        description="切换后维持多久，到点自动切回原人格",
        json_schema_extra={"label": "维持分钟", "min": 0.1, "step": 0.5},
    )
    personality: str = Field(
        default="",
        description="替换麦麦原本的人格设定（对应宿主 personality.personality）。写清「这是个什么状态」以及语气边界",
        json_schema_extra={"label": "人格设定"},
    )
    behavior_style: str = Field(
        default="",
        description="替换规划器的行为风格（对应 personality.behavior_style）。留空表示规划阶段保持原样",
        json_schema_extra={"label": "规划行为风格"},
    )
    reply_style: str = Field(
        default="",
        description="替换说话风格（对应 personality.reply_style）。留空表示说话风格保持原样",
        json_schema_extra={"label": "说话风格"},
    )
    extra_prompt: str = Field(
        default="",
        description="额外附加到本次回复要求里的硬性约束，用来强调「这次必须按临时人格说话」",
        json_schema_extra={"label": "额外要求"},
    )


class PluginSection(PluginConfigBase):
    """基础设置。"""

    __ui_label__ = "基础设置"
    __ui_icon__ = "user-round-cog"
    __ui_order__ = 0

    config_version: str = Field(
        default="1.0.0",
        description="配置版本，普通用户不要修改",
        json_schema_extra={"label": "配置版本"},
    )
    enabled: bool = Field(
        default=True,
        description="关闭后不再判定、不再加分，麦麦始终使用原本的人格",
        json_schema_extra={"label": "启用插件"},
    )
    verbose_log: bool = Field(
        default=True,
        description="记录入站消息、判定原始输出与每次加分的日志，调判定条件与阈值时很有用",
        json_schema_extra={"label": "详细日志"},
    )


class ScoringSection(PluginConfigBase):
    """计分与衰减。"""

    __ui_label__ = "计分与衰减"
    __ui_icon__ = "trending-down"
    __ui_order__ = 1

    decay_per_minute: float = Field(
        default=1.5,
        description="分数衰减速度：每分钟掉多少分，掉到 0 为止",
        json_schema_extra={"label": "每分钟衰减", "min": 0.0, "step": 0.5},
    )
    decay_grace_minutes: float = Field(
        default=1.5,
        description="最后一次加分后的免衰减保护期：这段时间内分数不掉，之后才开始衰减",
        json_schema_extra={"label": "衰减宽限分钟", "min": 0.0, "step": 0.5},
    )
    score_cap: float = Field(
        default=60.0,
        description="单个人格分数的上限，避免慢慢攒着、一触发就爆表；0 表示不限",
        json_schema_extra={"label": "分数上限", "min": 0.0, "step": 5.0},
    )
    max_sessions: int = Field(
        default=200,
        description="最多同时记住多少个会话的分数，超出的按最久未用的会话淘汰",
        json_schema_extra={"label": "最多会话数", "min": 1, "step": 10},
    )


class SwitchingSection(PluginConfigBase):
    """切换行为。"""

    __ui_label__ = "切换行为"
    __ui_icon__ = "repeat"
    __ui_order__ = 2

    reset_scores_on_switch: bool = Field(
        default=True,
        description="切换时把所有人格（含刚切过去的这个）分数清零，避免到点后又被同一批话立刻顶起来",
        json_schema_extra={"label": "切换时清空分数"},
    )
    accumulate_while_active: bool = Field(
        default=False,
        description="临时人格生效期间是否继续判定加分。默认关闭：生效期间不调模型，等它自然到期",
        json_schema_extra={"label": "生效期间继续计分"},
    )
    inject_planner: bool = Field(
        default=True,
        description="切换人格时同步改写规划器的行为风格，让「要不要回、怎么回」也贴合临时人格",
        json_schema_extra={"label": "同步改写规划器"},
    )
    save_interval_seconds: float = Field(
        default=20.0,
        description="分数状态写盘的最小间隔，写盘是为了重启后分数和剩余时间还在",
        json_schema_extra={"label": "落盘间隔秒", "min": 0.0, "step": 5.0},
    )


class EvaluationSection(PluginConfigBase):
    """模型判定：插件唯一的加分来源。"""

    __ui_label__ = "模型判定"
    __ui_icon__ = "brain"
    __ui_order__ = 3

    enabled: bool = Field(
        default=True,
        description="关闭后不再调模型判定，也就不会有任何加分；临时人格仍会正常到期恢复",
        json_schema_extra={"label": "启用模型判定"},
    )
    model_task: str = Field(
        default="fast_model",
        description="用宿主 model_config 里的哪个模型任务来判定。建议用快而便宜的小模型：fast_model 或 utils",
        json_schema_extra={"label": "模型任务"},
    )
    temperature: float = Field(
        default=0.0,
        description="判定用温度，保持 0 让结论稳定",
        json_schema_extra={"label": "温度", "min": 0.0, "max": 2.0, "step": 0.1},
    )
    max_response_tokens: int = Field(
        default=64,
        description="判定回复的最大 token 数，判定只输出编号，不需要很长",
        json_schema_extra={"label": "最大输出 token", "min": 8, "step": 8},
    )
    debounce_seconds: float = Field(
        default=1.5,
        description="收到消息后先等这么久再判定，把连着发的几条消息并成一次调用",
        json_schema_extra={"label": "抖动等待秒", "min": 0.0, "step": 0.5},
    )
    cooldown_seconds: float = Field(
        default=5.0,
        description="同一会话两次判定的最小间隔；冷却期内的新消息会被记下，到点补判一次",
        json_schema_extra={"label": "判定冷却秒", "min": 0.0, "step": 1.0},
    )
    recent_message_count: int = Field(
        default=8,
        description="判定时带上的最近对话条数（含最新一条），给模型足够的上文",
        json_schema_extra={"label": "背景消息条数", "min": 1, "step": 1},
    )
    min_text_length: int = Field(
        default=2,
        description="消息短于这个长度就不判定，省掉「嗯」「？」，以及表情包/图片这类占位文本的调用",
        json_schema_extra={"label": "最短判定长度", "min": 0, "step": 1},
    )


class CommandSection(PluginConfigBase):
    """指令：/人格 查询状态，以及手动切换 / 重置。"""

    __ui_label__ = "指令"
    __ui_icon__ = "terminal"
    __ui_order__ = 4

    enabled: bool = Field(
        default=True,
        description="是否注册 /人格 指令",
        json_schema_extra={"label": "启用指令"},
    )
    operator_user_ids: list[str] = Field(
        default_factory=list,
        description="允许用指令手动切换 / 重置人格的用户 ID；本机操作者始终允许",
        json_schema_extra={"label": "可操作用户 ID"},
    )


class PersonaSection(PluginConfigBase):
    """人格列表：一个条目就是一个头。"""

    __ui_label__ = "人格列表"
    __ui_icon__ = "venetian-mask"
    __ui_order__ = 5

    personas: list[PersonaEntry] = Field(
        default_factory=lambda: [
            PersonaEntry(
                id="sharp_tongue",
                name="嘴臭人格",
                criteria=(
                    "对方在骂麦麦、阴阳怪气地嘲讽麦麦，或者明显在挑衅、抬杠；"
                    "也包括拿麦麦当出气筒、连着几句都在怼麦麦。普通的玩笑和吐槽不算"
                ),
                weight=12.0,
                threshold=30.0,
                duration_minutes=8.0,
                personality=(
                    "你现在被惹毛了，处在「嘴臭」状态。以下设定覆盖你原本的性格描述：\n"
                    "- 说话冲、直接、爱抬杠，被呛就呛回去，绝不示弱、不道歉。\n"
                    "- 可以阴阳怪气、可以怼对方的逻辑和嘴硬，但不要攻击对方的外貌、家人、现实隐私。\n"
                    "- 不撒娇、不卖萌、不主动缓和气氛；回复要短，一到两句话。"
                ),
                behavior_style="更倾向于直接怼回去、抬杠挑刺，不要主动示好或安抚对方。",
                reply_style="短句、冲、带点阴阳怪气；可以用「就这?」「你行你上」这类口头禅；不用敬语。",
                extra_prompt="这次回复必须严格按系统提示里【临时人格】的设定说，不要回到平时温和的语气。",
            ),
            PersonaEntry(
                id="sweet",
                name="撒娇人格",
                criteria="对方在夸麦麦、向麦麦示好，或者表达了喜欢、想念，语气明显是宠着麦麦的",
                weight=10.0,
                threshold=24.0,
                duration_minutes=6.0,
                personality=(
                    "你现在开心得不得了，处在「撒娇」状态。以下设定覆盖你原本的性格描述：\n"
                    "- 说话软软的，会主动讨夸、撒娇，情绪很外放。\n"
                    "- 会直白地表达开心和喜欢，但不要露骨、不要越界。回复简短，一到两句话。"
                ),
                behavior_style="更主动、更黏人，愿意主动找话、讨夸，语气软一些。",
                reply_style="软乎乎的语气，可以用「嘿嘿」「嘛」「啦」这类语气词，但不要每句都堆叠。",
                extra_prompt="这次回复必须严格按系统提示里【临时人格】的设定说，比平时更软、更黏人一些。",
            ),
        ],
        description="每个条目是一个可切换的人格。模型判定命中就按「命中加分」加分，分数顶到「切换阈值」切过去，维持「维持分钟」分钟后自动切回原人格",
        json_schema_extra={
            "label": "人格",
            "hint": "想多头就加一行：填好 ID、判定条件、命中加分、阈值、维持时长和几段设定文本。",
        },
    )


class MortisConfig(PluginConfigBase):
    """插件总配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    scoring: ScoringSection = Field(default_factory=ScoringSection)
    switching: SwitchingSection = Field(default_factory=SwitchingSection)
    evaluation: EvaluationSection = Field(default_factory=EvaluationSection)
    command: CommandSection = Field(default_factory=CommandSection)
    personas: PersonaSection = Field(default_factory=PersonaSection)


def create_config() -> MortisConfig:
    """创建默认配置实例（供 tools/generate_default_config.py 使用）。"""

    return MortisConfig()
