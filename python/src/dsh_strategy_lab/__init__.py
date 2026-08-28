# coding=utf-8
"""dsh_strategy_lab — 策略层 harness（2026-08-27 规划定稿）。

兄弟包于 dsh_factor_mining（同项目同测试套件）：只允许
``import dsh_factor_mining`` 共享面（env/causality/noise/gates），
反向禁止——依赖方向物理单向（策略→因子）。

六条纪律自因子层迁移：唯一评估路径（CLI）/ 时点环境（物理截断 env）/
事务化准入（submit 全过才写 registry）/ 多重检验计价（deflation +
账本）/ fail-closed + 可行动错误 / 预算分层。

策略层三个新问题的对应物：前视面倍增（→ signal-only 契约收窄）、
路径依赖统计（→ placebo 中心化）、判定基准（→ 增量门 + 启动点敏感性）。

形态：CLI（``python -m dsh_strategy_lab``），ZCode IDE 内由助手驱动；
非 DSH 插件、非 MCP（需要时再立项）。工作场所状态目录 = ``.strategy-lab/``
（与 ``.factor-mining/`` 平级；生产因子状态只读，零迁移）。
"""
__version__ = "0.1.0"
