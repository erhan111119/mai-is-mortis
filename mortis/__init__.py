"""mai is mortis —— 麦麦是多首的怪物。

加分的唯一来源是模型判定：分数满即临时换人格，到点自动切回，分数随时间衰减。

对外只有 :mod:`plugin` 一个入口：

* :mod:`mortis.config` —— 配置模型（人格、判定、衰减、指令）；
* :mod:`mortis.judge` —— 人格档案与模型判定；
* :mod:`mortis.state` —— 每个会话的分数账本：加分、衰减、激活与落盘；
* :mod:`mortis.prompt_patch` —— 把系统提示里的人设段落换成临时人格。
"""
