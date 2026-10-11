"""mai is mortis 的内部实现。

对外只有 :mod:`plugin` 一个入口：

* :mod:`mortis.config` —— 配置模型（人格、判定、衰减、指令）；
* :mod:`mortis.judge` —— 人格档案与模型判定（唯一的加分来源）；
* :mod:`mortis.state` —— 每个会话的分数账本：加分、衰减、激活与落盘；
* :mod:`mortis.prompt_patch` —— 把系统提示里的人设段落换成临时人格。
"""
