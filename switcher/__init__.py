"""人格切换插件的内部实现。

对外只有 :mod:`plugin` 一个入口，这里按职责拆开：

* :mod:`switcher.config` —— 配置模型（人格、判定、衰减、指令）；
* :mod:`switcher.personas` —— 把人格配置整理成运行时用的只读档案；
* :mod:`switcher.state` —— 每个会话的分数账本，负责加分、衰减、激活与落盘；
* :mod:`switcher.prompt_patch` —— 把系统提示里的人设段落换成临时人格；
* :mod:`switcher.evaluator` —— 模型判定：唯一的加分来源。
"""
