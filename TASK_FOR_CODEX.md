# TASK FOR CODEX — AD-P1 甲案隔离实现（会话史写入侧送达对账根治改造）

你是一名隔离施工方。本任务在一份**生产代码的只读副本**上实现一套已冻结设计的改造，产物只落隔离目录、供独立复核，**不部署、不接触生产**。规格以随附三件为准，本任务书为其施工摘要；冲突时以规格件为准，并按下文「勘误优先」条处理。

随附材料：
- `ADP1_FIX_DESIGN_V2.md`（设计定版，SHA-256 前 8 位 ce9a2609）——主规格，施工范围为其 §三（甲案）、§七（字段缺口）、§八（验收）。
- `ADP1_FIX_DESIGN_V2_ERRATUM1.md`（afc172d2）——勘误登记：凡 V2 中涉及 tx 分组构成之处（「14 条 tx=null 含 TYPO_FIX」「TYPO_FIX tx=null」），**以勘误为准**：TYPO_FIX 记录**无 tx 键**（键缺失组），其挂接定位不得依赖 tx，只能用 sends 记录行＋文本指纹。
- `ADP1_FIX_DESIGN_REVIEW.md`（a5bbe659）——独立复核件，R-1～R-8 回填要求已并入 V2 正文，仅作背景。
- `baseline/bridge.py`（生产只读副本，SHA-256 f2aae7cd…，233,380 B）、`baseline/sender.py`（同，25c90e6a…，37,153 B）——改造底本。bridge.py 中注释明示现行语义：生成当刻即作为会话自身轮次写入会话转录（约 4419–4423 行附近注释），与 sender 发送结果无对账通道——这正是要修的点。

## 施工范围（仅甲案，只做这四件）

### 1. 生成入 PENDING 暂存（写入侧）
- 回复生成时，不再直接把生成全版写入持久权威转录；先写入「待对账轮次」记录，状态 `PENDING`。
- turn 级新增字段（须**向后兼容**：不认识新字段的旧读取路径忽略之、行为不变）：
  - `status` ∈ {PENDING, DELIVERED, DELIVERED_PARTIAL, NOT_DELIVERED, DELIVERY_UNCERTAIN}
  - `delivered_text` 槽（转正时填实际送达版）
  - `sends_evidence` 指针（message_ids 等平台接受级证据）
  - `corrections` 附记槽（TYPO_FIX 用，见第 3 件）
  - 定位四元组：原始会话 id＋轮次序号＋生成文本指纹＋tx/等价关联键
- PENDING 轮次不进持久权威转录、不被 digest 摘录、不作跨会话前情。

### 2. sender 终局事件回写通道（新增）
- sender 对一轮得出终局后，向**落盘文件队列**（append-only JSONL 即可，不新建服务）发出终局事件：含定位四元组、终局 result、segments_sent、message_ids、draft（改写后文本）、draft_orig（原稿）指针。
- 通道须**有序＋幂等**：同轮多次事件以最后终局为准；重复回写不得重复落账。

### 3. reconciler 转正规则（写入侧回写器）
按实际送达版把 PENDING 轮次转正落入正式转录：
- **全段 SENT**：delivered_text ＝ sends 的 draft **原文**（含 CQ 引用码等原始形态，落账零清洗、零改写、零规范化——清洗只许发生在下游呈现层、不得反向回填），status＝DELIVERED，附 message_ids 指针。
- **部分送达／rubber 删改**：只落已送达部分的拼接版（以 sends 的 draft 为准），status＝DELIVERED_PARTIAL；生成全版与 draft_orig 永不入转录。
- **非 SENT 终局**（result ∈ {RATE_LIMITED, FAILED, GATE_OFF, PROACTIVE_LIMITED}）：整轮不入正式转录；留痕只入与转录物理分离的「未送达附记」，status＝NOT_DELIVERED。
- **结果未知**（sends-unknown 同型）：落入正式转录但 status＝DELIVERY_UNCERTAIN，默认被 digest 摘录与新会话前情排除；仅人工核放可引用且须带不确定标记。
- **TYPO_FIX**：不新建 turn；作其所修正的已转正轮次（DELIVERED／DELIVERED_PARTIAL）的顺序附记入 `corrections` 槽；**严禁**把修正拼回 delivered_text（实际送达序列是「原消息＋修正消息」两条）；附记自身状态以其自身平台接受证据为准（无证据标 DELIVERY_UNCERTAIN）。注意 TYPO_FIX 记录无 tx 键，挂接靠记录行＋文本指纹定位原轮次。
- **语音轮次**：一律 DELIVERY_UNCERTAIN，本实现不给语音轮次任何转正路径。
- **回写超时／回写器故障**：终局事件在可配置时限内未到，轮次保持 PENDING 并触发积压告警（钩子即可）；人工处置入口只能把 PENDING 转 DELIVERY_UNCERTAIN 或 NOT_DELIVERED，**代码层禁止人工直接转正为 DELIVERED**。
- **跨会话轮换定位（R-8）**：PENDING 登记簿独立于活动会话指针持久保存；人设切换（会话轮换）事件落账登记（旧会话 id→新会话 id）；轮换后到达的终局仍按原始会话 id 回写落入**原会话**转正层，严禁落入新会话冒充其轮次；定位失败（原会话不可定位／四元组对不上）→ 失败关闭：保持 PENDING＋告警，禁止就近写入任何其他会话。

### 4. 运行时上下文与持久转录分层
- 同会话即时上下文可引用生成版，但必须显式标 `provisional`，仅供当轮后续生成，不落盘为权威记录。
- 持久权威层及其一切下游读取（转录、digest 摘录、新会话前情、召回的会话一路）只认转正后的版本与状态。

## 禁项（硬约束，违反即整单作废）
- 不写任何生产路径、不连生产凭据、不访问网络与真实 QQ/OneBot 端点；一切运行只在隔离目录与合成数据上。
- 不改判门结论、不改闸门策略、不改节流、不动 digest 取材门的干净部分、不动 sender 既有 D-01 留痕形态（result／segments_sent／message_ids／draft／draft_orig 的既有语义只读消费）。
- 不重写、不删除、不「顺手整理」任何历史转录原文；本单不处理历史存量订正（那是乙案，不在本单）。
- 全案禁用「确认投递／已送达确认」表述；SENT 只证明**平台接受级**。
- 不得把乙案（摘录前对账）冒充甲案关闭条件；本单只做甲案。

## 产物形态（缺一不可，全部放本仓库 `impl/` 目录）
1. 隔离实现树：以 baseline 两文件为底本的**改造后完整文件**（drop-in 形态，与底本同名，置 `impl/`）＋新增模块文件（如 reconciler、回写队列读写、PENDING 登记簿，同置 `impl/`），每个文件头部注明与底本的对应关系。
2. 变更说明 `impl/CHANGES.md`：逐文件、逐处列明改了什么、对应设计条目编号（§3.1 第几点／§七缺口几／R-8 等）、未改动面的声明。
3. 自测脚本 `impl/test_jia.py`：可离线一键运行、零网络、零生产接触，覆盖下文验收场景，输出逐项 PASS/FAIL 与汇总，不得有跳过项。
4. 自测运行日志原文（脚本实际输出）存 `impl/test_output.txt` 一并交付。

## 验收口径（设计 §八在本单的对应面：合成样本＋过程门）
自测必须构造并逐条核对（转录落账文本、status、运行时 provisional 引用、digest 摘录资格）：
- 五类剧本：①全段送达；②整段未送达（限流／失败／闸门各一例）；③部分送达；④rubber 删改；⑤结果未知；每类含多段与长稿变体；
- TYPO_FIX 附记例：原轮次 delivered_text 不被改写、附记顺序与状态正确、无 tx 键挂接成功；
- 语音轮次例：恒 DELIVERY_UNCERTAIN、无转正路径；
- 跨轮换例：轮次 PENDING 期间发生会话轮换、终局其后到达 → 回写落入原会话、定位键全程可追、新会话转录无冒充条目；
- 过程门：回写超时注入 → PENDING 不转正且告警触发；回写器故障注入 → 失败关闭；重复回写 → 幂等不重复落账；人工处置入口尝试直接转正 → 被代码拒绝；
- 向后兼容例：旧格式转录（无新字段）被新代码读取不报错、旧读取逻辑面对新字段不崩溃。

三数验收门（未送达入史率／删改残留率／误杀已送达率，目标全 0）的**历史回放面不在本单**（属乙案与部署窗验收）；本单只须在合成样本面上达成等价断言：未送达零入史、被删片段零残留、已送达零误标。
