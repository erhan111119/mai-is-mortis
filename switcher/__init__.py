"""人格切换插件的内部实现。

对外只有 :mod:`plugin` 一个入口，这里按职责拆开：

* :mod:`switcher.config` —— 配置模型（人格、触发规则、衰减、判定、指令）；
* :mod:`switcher.state` —— 每个会话的分数账本，负责加分、衰减、激活与落盘；
* :mod:`switcher.triggers` —— 把配置里的关键词/正则编译成匹配器；
* :mod:`switcher.prompt_patch` —— 把系统提示里的人设段落换成临时人格；
* :mod:`switcher.evaluator` —— 可选的模型判定，补关键词覆盖不到的软性条件。
"""
