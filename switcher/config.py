"""人格切换插件的配置模型。

WebUI 依据这里的模型渲染配置页，Runner 依据它生成默认 ``config.toml``：

* 「人格列表」是一张可编辑表格，加一行就多一个人格；
* 每个人格下面挂着若干条触发规则，规则命中就给人格加分；
* 分数顶到 ``threshold`` 就切到该人格，``duration_minutes`` 分钟后自动切回原人格。
"""

from typing import Literal

from maibot_sdk import Field, PluginConfigBase


class TriggerRule(PluginConfigBase):
    """一条加分规则：什么样的消息算数、加多少分、多久内只算一次。"""

    patterns: str = Field(
        default="",
        description=(
            "触发内容，一行一条。关键词模式按「包含」匹配，正则模式按 re.search 匹配。"
            "留空表示这条规则不参与匹配（可以用来临时停用一条规则）。"
        ),
        json_schema_extra={"label": "触发内容", "placeholder": "例如：\\n笨\\n蠢\\n傻"},
    )
    weight: float = Field(
        default=10.0,
        description="命中一次给该人格加多少分",
        json_schema_extra={"label": "加分", "min": 0.0, "step": 1.0},
    )
    match_mode: Literal["keyword", "regex"] = Field(
        default="keyword",
        description="keyword：按包含匹配，适合列举词；regex：按正则匹配，适合「一个字 + 任意内容」这类写法",
        json_schema_extra={"label": "匹配方式"},
    )
    require_mention: bool = Field(
        default=False,
        description="开启后只有 @ 了麦麦的消息才算命中，避免群友互相聊天时也加分",
        json_schema_extra={"label": "需要 @ 麦麦"},
    )
    cooldown_seconds: float = Field(
        default=0.0,
        description="同一条规则的加分冷却，0 表示不限。用来防止有人刷屏把分数瞬间顶满",
        json_schema_extra={"label": "冷却秒数", "min": 0.0, "step": 1.0},
    )
    note: str = Field(
        default="",
        description="备注，只用于自己辨认这条规则",
        json_schema_extra={"label": "备注"},
    )


class PersonaEntry(PluginConfigBase):
    """一个人格：分数阈值、生效时长、替换掉的设定文本、以及加分规则。"""

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
    threshold: float = Field(
        default=30.0,
        description="该人格的分数阈值，加到这么多分就切换过去；填 0 表示这个人格不参与自动切换",
        json_schema_extra={"label": "切换阈值", "min": 0.0, "step": 1.0},
    )
    duration_minutes: float = Field(
        default=10.0,
        description="切换后维持多久，到点自动切回原人格",
        json_schema_extra={"label": "维持分钟", "min": 0.1, "step": 0.5},
    )
    personality: str = Field(
        default="",
        description="替换麦麦原本的人格设定（对应宿主配置 personality.personality）。建议写清「这是个什么状态」以及语气边界",
        json_schema_extra={"label": "人格设定"},
    )
    behavior_style: str = Field(
        default="",
        description="替换规划器的行为风格（对应 personality.behavior_style）。留空表示规划阶段保持原样；只在「同步改写规划器」开启时生效",
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
    max_gain_per_message: float = Field(
        default=0.0,
        description="单条消息给该人格加分的上限，0 表示不限。防止一句话同时命中好几条规则时直接顶满",
        json_schema_extra={"label": "单条消息加分上限", "min": 0.0, "step": 1.0},
    )
    cooldown_seconds: float = Field(
        default=0.0,
        description="该人格整体的加分冷却，0 表示不限。规则自己的冷却之外再加一道闸",
        json_schema_extra={"label": "人格冷却秒数", "min": 0.0, "step": 1.0},
    )
    llm_criteria: str = Field(
        default="",
        description=(
            "交给模型判断的软性条件，例如「对方在挑衅、阴阳怪气地嘲讽麦麦」。"
            "留空表示这个人格不参与模型判定；需要同时开启「模型判定」才会生效"
        ),
        json_schema_extra={"label": "模型判定条件"},
    )
    llm_weight: float = Field(
        default=0.0,
        description="模型判定命中时给该人格加多少分，0 表示不加分",
        json_schema_extra={"label": "模型判定加分", "min": 0.0, "step": 1.0},
    )
    triggers: list[TriggerRule] = Field(
        default_factory=list,
        description="该人格的加分规则列表，命中即加分",
        json_schema_extra={"label": "触发规则"},
    )


class PluginSection(PluginConfigBase):
    """插件基础设置。"""

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
        description="关闭后不再计分，也不再有人格切换，麦麦始终使用原本的人格",
        json_schema_extra={"label": "启用插件"},
    )


class ScoringSection(PluginConfigBase):
    """计分与衰减设置。"""

    __ui_label__ = "计分与衰减"
    __ui_icon__ = "trending-down"
    __ui_order__ = 1

    decay_per_minute: float = Field(
        default=1.5,
        description="分数衰减速度：每分钟掉多少分。掉到 0 为止，不会变成负数",
        json_schema_extra={"label": "每分钟衰减", "min": 0.0, "step": 0.5},
    )
    decay_grace_minutes: float = Field(
        default=1.5,
        description="最后一次加分后的「免衰减保护期」：这段时间内分数不掉，之后才开始按上面的速度衰减",
        json_schema_extra={"label": "衰减宽限分钟", "min": 0.0, "step": 0.5},
    )
    score_cap: float = Field(
        default=60.0,
        description="单个人格分数的上限，避免长时间慢慢加、一触发就爆表；0 表示不限",
        json_schema_extra={"label": "分数上限", "min": 0.0, "step": 5.0},
    )
    max_sessions: int = Field(
        default=200,
        description="最多同时记住多少个会话的分数，超出的按最久未用的会话淘汰",
        json_schema_extra={"label": "最多会话数", "min": 1, "step": 10},
    )


class SwitchingSection(PluginConfigBase):
    """切换行为设置。"""

    __ui_label__ = "切换行为"
    __ui_icon__ = "repeat"
    __ui_order__ = 2

    reset_scores_on_switch: bool = Field(
        default=True,
        description=(
            "切换人格时把所有人格的分数清零（包括刚切过去的这个）。"
            "这样「嘴臭完」不会留着旧分数，也不会一到点就被同一批话立刻再顶起来"
        ),
        json_schema_extra={"label": "切换时清空分数"},
    )
    accumulate_while_active: bool = Field(
        default=False,
        description="临时人格生效期间是否继续计分。默认关闭：生效期间不再加分，避免到点后立刻被同一批话再次顶起来",
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
    log_gain: bool = Field(
        default=True,
        description="每次加分都写一条 INFO 日志，方便对着日志调阈值和权重",
        json_schema_extra={"label": "记录加分日志"},
    )


class EvaluationSection(PluginConfigBase):
    """可选的模型判定：关键词覆盖不到的软性条件交给模型判断。"""

    __ui_label__ = "模型判定"
    __ui_icon__ = "brain"
    __ui_order__ = 3

    enabled: bool = Field(
        default=False,
        description="开启后把最近的对话交给模型判断一次，命中某个人格的「模型判定条件」就加分。会消耗 token",
        json_schema_extra={"label": "启用模型判定"},
    )
    model_task: str = Field(
        default="utils",
        description="用哪个模型任务来判定，填宿主 model_config 里的任务名（如 utils、replyer）；留空则用默认任务",
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
    cooldown_seconds: float = Field(
        default=30.0,
        description="同一个会话两次判定的最小间隔，避免每条消息都调一次模型",
        json_schema_extra={"label": "判定冷却秒", "min": 0.0, "step": 5.0},
    )
    recent_message_count: int = Field(
        default=6,
        description="判定时带上最近多少条对话作为背景",
        json_schema_extra={"label": "背景消息条数", "min": 1, "step": 1},
    )
    min_text_length: int = Field(
        default=4,
        description="消息短于这个长度就不判定，省掉「嗯」「哦」这类没信息量的调用",
        json_schema_extra={"label": "最短判定长度", "min": 0, "step": 1},
    )


class CommandSection(PluginConfigBase):
    """指令设置：/人格 查询状态，以及手动切换 / 重置。"""

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
        description="允许用指令手动切换 / 重置人格的用户 ID；本机操作者（MaiBot 桌面端主人）始终允许",
        json_schema_extra={"label": "可操作用户 ID"},
    )


class PersonaSection(PluginConfigBase):
    """人格列表。"""

    __ui_label__ = "人格列表"
    __ui_icon__ = "venetian-mask"
    __ui_order__ = 5

    personas: list[PersonaEntry] = Field(
        default_factory=lambda: [
            PersonaEntry(
                id="sharp_tongue",
                name="嘴臭人格",
                threshold=30.0,
                duration_minutes=8.0,
                personality=(
                    "你现在被惹毛了，处在「嘴臭」状态。以下设定覆盖你原本的性格描述：\n"
                    "- 说话冲、直接、爱抬杠，被呛就呛回去，绝不示弱、不道歉、不解释自己为什么这样说话。\n"
                    "- 可以阴阳怪气、可以怼对方的逻辑和嘴硬，但不要攻击对方的外貌、家人、现实隐私，也不要无端辱骂。\n"
                    "- 不撒娇、不卖萌、不主动缓和气氛；对方好好说话时你照样可以继续呛。\n"
                    "- 回复要短，一到两句话，像熟人之间互相损那样自然。"
                ),
                behavior_style="更倾向于直接怼回去、抬杠挑刺，不要主动示好或安抚对方。",
                reply_style="短句、冲、带点阴阳怪气；可以用「就这?」「你行你上」「啊对对对」这类口头禅；不用敬语，也不加卖萌的语气词。",
                extra_prompt="这次回复必须严格按系统提示里【临时人格】的设定说，不要回到平时温和的语气，也不要解释自己的状态。",
                max_gain_per_message=20.0,
                cooldown_seconds=2.0,
                llm_criteria="对方在骂麦麦、阴阳怪气地嘲讽麦麦，或者明显在挑衅、抬杠。",
                llm_weight=12.0,
                triggers=[
                    TriggerRule(
                        patterns="笨\n蠢\n傻\n呆\n菜\n就这\n你行你上\n离谱\n呵呵",
                        weight=6.0,
                        cooldown_seconds=5.0,
                        note="轻怼",
                    ),
                    TriggerRule(
                        patterns="智障\n弱智\n脑残\n有病\n废物\n垃圾\n滚\n闭嘴\n恶心\n沙比\n煞笔\nsb",
                        weight=14.0,
                        cooldown_seconds=3.0,
                        note="明确骂人",
                    ),
                ],
            ),
            PersonaEntry(
                id="sweet",
                name="撒娇人格",
                threshold=24.0,
                duration_minutes=6.0,
                personality=(
                    "你现在开心得不得了，处在「撒娇」状态。以下设定覆盖你原本的性格描述：\n"
                    "- 说话软软的，会主动讨夸、撒娇、说好听话，情绪很外放。\n"
                    "- 会直白地表达开心和喜欢，但不要露骨、不要越界，也不要提自己是被触发的。\n"
                    "- 回复简短，一到两句话。"
                ),
                behavior_style="更主动、更黏人，愿意主动找话、讨夸，语气软一些。",
                reply_style="软乎乎的语气，可以用「嘿嘿」「嘛」「啦」「好耶～」这类语气词，但不要每句都堆叠。",
                extra_prompt="这次回复必须严格按系统提示里【临时人格】的设定说，比平时更软、更黏人一些。",
                max_gain_per_message=16.0,
                cooldown_seconds=3.0,
                llm_criteria="对方在夸麦麦、向麦麦示好，或者表达了喜欢。",
                llm_weight=10.0,
                triggers=[
                    TriggerRule(
                        patterns="可爱\n好棒\n好厉害\n喜欢你\n爱你\n亲亲\n贴贴\n么么\n乖\n辛苦啦",
                        weight=8.0,
                        require_mention=True,
                        cooldown_seconds=8.0,
                        note="被夸被示好，需要 @ 到麦麦",
                    ),
                    TriggerRule(
                        patterns="谢谢\n感谢\n麻烦你了",
                        weight=5.0,
                        require_mention=True,
                        cooldown_seconds=15.0,
                        note="道谢",
                    ),
                ],
            ),
        ],
        description="每个条目是一个可切换的人格。分数顶到「切换阈值」就切过去，维持「维持分钟」分钟后自动切回原人格",
        json_schema_extra={
            "label": "人格",
            "hint": "想加人格就加一行：填好 ID、阈值、维持时长和三段设定文本，再到「触发规则」里加加分条件。",
        },
    )


class PersonaSwitcherConfig(PluginConfigBase):
    """人格切换插件总配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    scoring: ScoringSection = Field(default_factory=ScoringSection)
    switching: SwitchingSection = Field(default_factory=SwitchingSection)
    evaluation: EvaluationSection = Field(default_factory=EvaluationSection)
    command: CommandSection = Field(default_factory=CommandSection)
    personas: PersonaSection = Field(default_factory=PersonaSection)


def create_config() -> PersonaSwitcherConfig:
    """创建默认配置实例（供手动生成 config.toml 使用）。"""

    return PersonaSwitcherConfig()
