# AD-P1 甲案隔离施工区（Codex 单 #1）

本仓库（原为空仓）临时用作 Codex 隔离施工的材料与产物区，内容全部来自本地工作区 `night-20261029/followup/stealcraft/adp1jia/`，**与生产无任何连接**：

- `TASK_FOR_CODEX.md`——施工任务书（范围、禁项、验收口径），Codex 先读此件。
- `ADP1_FIX_DESIGN_V2.md`——设计定版（主规格，SHA-256 前 8 位 ce9a2609）。
- `ADP1_FIX_DESIGN_V2_ERRATUM1.md`——勘误登记（afc172d2），与 V2 冲突处以勘误为准。
- `ADP1_FIX_DESIGN_REVIEW.md`——独立复核件（a5bbe659，背景）。
- `baseline/bridge.py`——生产 bridge.py 只读副本（SHA-256 f2aae7cd43a862328b2f700b2555591d31f6bcf272e6365340591620cde2845e，233,380 B）。
- `baseline/sender.py`——生产 sender.py 只读副本（SHA-256 25c90e6a17d2793ed4fa3963650c02de1ff85b3889bc369c68fbd0906b94ed7b，37,153 B）。

产物要求（见 TASK_FOR_CODEX.md）：改造后完整文件放 `impl/`、变更说明 `impl/CHANGES.md`、自测脚本 `impl/test_jia.py` 及运行日志 `impl/test_output.txt`。产物最终取回本地隔离目录复核，不从本仓库部署任何东西到生产。
