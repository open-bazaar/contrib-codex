# Codex Harness 插件验收

## 目标与依据

验证 `bazaar-contrib-codex` 对公共 Harness 契约的实际实现效果。契约以已发布
`bazaar-contrib-harness==0.3.0rc4` 的 `HarnessBuilder`、`Harness`、输入、输出和审批类型为依据，
对应本插件的 `inspection.py`、`runtime.py`、`approval.py` 与 `transport.py`。

开发线程 `75943c0d-36a8-58c6-8241-73989f7c43c5` 确认以下七项范围；
`4799fb6c-2981-5436-85e4-d9c36df2b7c9` 将其确定为验收标准。
`a61af7fc-97a7-52e3-8ca5-13ef0430b9f0` 确认提醒使用五秒。

## 验收标准

| 能力 | 验收要求 |
| --- | --- |
| 发现与配置 | `HarnessBuilder.inspect` 返回实际 CLI 可用性、版本、模型、effort 和技能；会话收到实际 workspace、指令、环境、model/effort、沙箱与审批策略。 |
| 执行与输出 | `start_session`、`start_turn` 完成真实 bcc 工作；文本、工具结果、用量和终态通过 `HarnessOutput` 返回，保留会话、回合、来源和原始事件。 |
| 连续会话与 steering | 后续自然消息沿用已有上下文；执行中的补充消息经 `steer_turn` 进入所属回合，关联标识与实际执行一致。 |
| 恢复与刷新 | `reconcile_session` 在所属 app-server 退出后核对并恢复原会话及回合状态；指令或技能变化经 `receive_event` 返回 `HarnessExpire`，由宿主在安全时机更新上下文。 |
| 审批往返 | 原生审批通过 TestChannel 交互，核对操作、发送者、会话、回合与支持范围；批准、拒绝和 scope 映射回原生协议后的实际执行与决定一致。 |
| 后台工作与资源释放 | `has_background_job` 反映所属会话的实际后台状态；关闭 stream、`stop_session` 与插件退出取消所属执行，回收自有进程和待处理请求。 |
| 错误与调用预算 | 通过真实所属进程的暂停、退出和原生报错观察失败；保留实际原因和错误码，区分执行失败、断联、超时和需要恢复的未确认状态。 |

接口结果、关联标识、审批与资源归属按公共契约核对。自然回复依据实际有效行为判断，
记录真实命令及其结果、消息交付和回合完成情况。

## 执行方式

独立安装实际 PyPI rc4、当前 provider wheel 和真实 TCP TestChannel。使用临时配置、数据库、
CODEX_HOME、工作区及自有进程，保留完整公开 AuditEvent、HarnessOutput、工具结果和清理产物。
TestChannel 沿原有入站契约将测试会话标记为已审核，再观察所属 Harness 的原生审批交互。

按七个独立 pytest 用例分别创建实际安装、配置、数据库、CODEX_HOME 与工作区：

- ONCE：Seattle 的自然天气查询经 ONCE 批准，在实际 MCP 返回后核对结果并关闭所属应用。
- SESSION 复用：先查询 New York 并批准 SESSION，再自然查询 Chicago；第二条消息可以进入
  仍活动的原回合或下一个回合，宿主 session／原生 thread 必须保持一致，且没有重复 MCP 审批。
- 跨会话授权：先批准 New York 的 SESSION，再正常关闭所属 bcn；保留 CODEX_HOME 和工作区，
  使用新的临时数据库重启 bcn，查询 Boston 并重新批准 ALWAYS；再次重启后查询 San Francisco，
  核对持久授权。每次均在实际 MCP 结果满足要求后结束检查，不等待模型自然结束整轮。
- bcc 对话：执行“五秒后提醒我喝水”并补充简短要求，再追问前文；核对 steering、
  连续会话、命令审批及拒绝后的实际行为。
- 进程恢复：独立的简短自然对话正常结束后，终止所属 app-server，再继续对话；核对原
  session／thread 及实际进程变化，最后在一个新活动回合启动时关闭应用。
- 指令刷新：独立自然对话正常结束后更新 AGENTS.md，再继续对话，核对安全刷新创建新上下文。
- 后台工作：独立原生会话核对实际后台任务、已完成／活动状态恢复、中断和生命周期。

TCP 收信持续异步运行，在执行、判定目标达成和应用关闭期间原样保存全部 AuditEvent 与
HarnessOutput。MCP 目标以原生 `tool.completed`／`tool.failed` 为观测边界，核对服务、工具、
参数、真实 HTTP／结构化结果与审批；不把工具完成写成自然回合完成。只有正常结束但
没有目标调用或 MCP 审批的尝试才接受最多八次实际入站反馈，反馈保持原 session／thread。

需要连续上下文的审批、恢复、steering 与刷新步骤仍在各自用例内部执行。
主用例一旦观察到 `compaction.started`，保留原始事件并按未完成退出本项，其他独立用例继续。
压缩观测如需扩展，应放入单独测试。

在隔离 CODEX_HOME 中使用原生命令审批规则，使自然发生的 bcc 命令进入审批交互，
通过 TestChannel 返回实际决定并观察执行结果。规则格式依据
[OpenAI Docs：Rules](https://learn.chatgpt.com/docs/agent-configuration/rules)。
范围选择以原生请求的 allowed_scopes 为准。

后台用例使用独立安装、全新 CODEX_HOME、工作区和相同本地模型，通过 Harness 发起一个临时预览服务的
自然请求。在该回合完成后核对实际原生后台终端列表、`has_background_job` 与已完成状态恢复。
随后在同一会话中自然追问，在新回合启动时执行活动状态核对，关闭 stream 后观察原生中断终态，
再从原生历史恢复该终态；最后检查 `stop_session` 的
自有进程与待处理请求释放。该原生会话使用 `never` 审批策略，审批往返由前述 TestChannel 场景验证。

元数据、文件变化和调用预算使用实际 Codex 进程验证。已存在的真实原生查询、暂停超时、
恢复查询和进程退出验证，按本轮 provider 源码及发行产物复核并登记适用范围。

## 任务与记录

### 上下文窗口对照（2026-10-24）

开发线程 `1f918f96-8af5-50d4-a82c-d29a761b25fc` 询问内置提示词是否造成过早压缩，
`44a12dd0-ff2a-5a3d-8444-6c7b17dba30d` 要求通过调整窗口继续测试。
上一轮首次请求已经约 14.1K tokens；测试主动配置 32,768 窗口、24,576 压缩门槛，
独立 SESSION 和全新 San Francisco 上下文在反复尝试错误的工具调用路线后进入压缩。
不能将这些结果解释为成功查询数个城市后耗尽上下文。

本轮将 Gateway 的 `context-size` 与 Codex 的 `model_context_window` 同时设为 65,536，
将 `model_auto_compact_token_limit` 设为 49,152；模型、量化、CPU、容器内存、提示词、
工具、七个独立场景、八次反馈上限和原有压缩失败边界保持原状。Codex CLI 固定为上一轮
实际使用的 0.164.0，避免 `latest` 变化引入版本差异。

先完成静态检查和七场景收集，再签名推送并复跑实际 CI。终态核对实际配置与模型窗口、
首次及压缩前用量、真实目标 MCP 与审批复用、反馈次数、完整输出／审计及资源清理。
扩大窗口只能验证更多余量是否帮助完成功能，不作为最大稳定窗口、提示词裁剪或压缩能力验收。
旧轮次的功能失败与六条关闭输出审计缺口继续保留，PR 保持草稿。

开发线程 `c696abc8-6831-5688-a847-6e27d0715434` 要求整理过多的提交。按先前
`e7e828f9-e63b-51a6-8da6-c5b390be9cf5` 已明确的约定，本 PR 的实现、测试、文档和
本次配置修改合成一个签名提交；后续验收记录 amend 回同一提交，不再累积修补或文档提交。
整理前保存原历史，核对最终文件树后用明确旧 head 的 `--force-with-lease` 更新该功能分支。

- [x] 同步修改两端窗口；Ruff、15 文件格式、Pyright 零错误、七用例收集和 wheel／sdist 构建通过。
- [x] 签名推送并复跑；保存实际配置、完整产物和终态结果，更新本记录与草稿 PR。

#### 64K 完整结果（2026-10-24 14:22 终态）

签名测试提交 `af2e16704bb52641182734f05569b75ebc669753` 的
[CI 40060305476](https://github.com/open-bazaar/contrib-codex/actions/runs/40060305476)
整体失败：**4 项通过、3 项失败，8689.94 秒（2:24:49）**。三个系统的完整静态检查及构建通过。
Gateway 后端确认 `n_ctx_slot=65536`，Codex 原生 usage 确认有效窗口 62259。

| 用例 | 本轮实际结果 |
| --- | --- |
| ONCE | 未完成。Seattle 首次自然结束但没有目标 MCP；一次实际反馈后仍反复使用错误工具路线，相同 weather 目录查找实际启动 162 次。05:33:23.507Z 开始原生压缩，最后 usage 49200 tokens；未取得天气调用或 ONCE MCP 审批，没有用尽八次反馈。 |
| SESSION | 失败。New York 的实际结果和 SESSION 审批通过。随后 Chicago 正文完整到达渠道，但原生输入只有未读通知；模型没有再次读信，以新 item 再查 New York，参数断言失败。无重复 MCP 审批不等于 Chicago 复用验收通过。 |
| 跨会话 ALWAYS | 通过。New York 经 SESSION 批准；正常关闭后，新数据库／上下文查询 Boston 并重新批准 ALWAYS；再次重启后 San Francisco 实际成功且没有重复 MCP 审批。三个上下文、数据库不同，CODEX_HOME 相同，完整 HTTP、参数和结果一致。 |
| bcc | 失败。真实读信、steering、命令批准及提醒创建存在；错误的 `--text` 退出 2，读帮助后改用 `--title`，提醒在正确锚点上于 5.005 秒后触发。后续只进入未读通知，模型没有读信或调用工具，没有命令拒绝或 Channel 回信，拒绝断言失败。 |
| 恢复 | 通过所列断言。恢复后仍为原 session／thread，reconcile confirmed；新活动回合 started 后关闭，宿主取消保留。 |
| 指令刷新 | 通过。两个自然回合完成，AGENTS.md 变化后创建不同 session／thread。 |
| 后台生命周期 | 通过。真实后台终端、完成态和活动态恢复、原生 interrupted、持久失败分类以及自有资源释放均核对通过。 |

完整产物含 5588 个有效 AuditEvent、5454 个有效 HarnessOutput，5448 个原生 provider 输出
全部原样审计，原生关联一致；另有六条关闭阶段宿主合成 cancelled 缺少对应 AuditEvent：
ONCE 1、SESSION 1、ALWAYS 3、恢复 1。原输出保留，完整输出审计覆盖仍有缺口。
13 个完成检查点按实际包顺序核对一致，未伪造因压缩退出的 ONCE 第二次完成检查点。
五次真实天气 HTTP 与原生完整结果一致；一次实际反馈的入站、通知、原上下文和上限 8 一致。

21 次审批的路由、原生请求、决定与执行一致：18 次 ONCE 命令批准、两次 SESSION MCP
批准及一次 ALWAYS MCP 批准。本轮没有实际拒绝或 ONCE MCP 审批，命令 SESSION／ALWAYS
仍未覆盖。两条 outbound 均有确认回执。225 条实际 usage 与 225 个完成后端请求的未缓存输入、
输出 token 一一匹配，无重复；另有四个实际取消请求，无其他未分类／未取消的未完成请求。

八次应用节点退出均为 0，自有存活子进程为空；后台另有 198 个有效 HarnessOutput、五个
关闭后的原生包，23 项生命周期核对通过，进程、连接、请求及交互清零。
Upload、Post Gateway 通过，网关两个容器、网络和卷均移除。后台没有留存 HTTP 响应或
仅绑定 loopback 的检查；恢复替代 PID 的具体值及刷新后的完整原生指令仍有原先的归档范围限制。

唯一产物 ID `12550339821`，run／head、安全路径、类型、大小及 CRC 已核对，pytest 重复目录
逐文件一致且未重复计数；ZIP SHA256 与 GitHub digest 一致：
`11626fff87a3848710ac9b221831ebb3435c85fc8798a73e2756af7c9d28b713`。
完整产物补齐浏览器间隙，最终观测后仅关闭本轮自有标签并验证已移除。

按开发线程 `14f784fa-45ec-513d-8088-0ee7f813f369`、
`3082c9c4-bb11-5670-bdd8-3529334a4572` 的要求，当前七个场景已导出单文件 Markdown 并交付。
11268 行源事件、883 个 item 快照、217 次工具开始／216 次结果、九组实际启动说明、
21 次审批、225 条 usage 和一次压缩均独立核对原文完整性。环境变量值未导出；全量模型
HTTP 请求、Codex 内部指令和工具 schema 的逐次精确拼接未归档，文档中已明确范围。

窗口扩大确实生效，但单轮对照不足以把功能变化完全归因于窗口大小；ONCE 的重复工具路线
及 SESSION／bcc 的未读消息处理仍需单独定位。历史失败和六条关闭输出审计缺口保留，PR 继续草稿。

### 128K 对照与实际资源观测（2026-10-24）

开发线程 `601568e6-9b44-5260-a14b-9bfb5cd6fc19` 已授权支持时继续增加窗口。
[Qwen 原始模型配置](https://huggingface.co/Qwen/Qwen3.5-4B/raw/main/config.json) 的
`max_position_embeddings=262144` 支持尝试下一档；实际 runner 的稳定上限仍需实测。
本轮将 Gateway／Codex 窗口同步为 131072，压缩门槛 98304；保留模型、量化、CPU、
内存上限、CLI、提示、工具、七场景、八次反馈及压缩失败边界。

64K 未留存容器实际内存／峰值／OOM 记录，10 GiB／512 MiB 仅是上限。
新增只读资源观测包裹原 pytest 命令，原样传递其退出码，记录属于本次 Gateway 的容器状态、
cgroup 当前／峰值／事件、进程 RSS／峰值及主机内存；归档实际 GGUF 元数据供版本比较。
在原进程退出后追加最后快照，不改变模型请求、场景断言、反馈或功能失败条件。
资源观测本身的失败独立记录，不覆盖 pytest 的原始结论。

- [x] 同步下一档窗口，原七场景与生产源码不变；Ruff、16 文件格式、Pyright、七用例收集和 wheel／sdist 构建通过。资源观测失败时，真实子进程退出 0／7 均原样透传。
- [x] amend 回同一签名提交并启动 CI；核对有效窗口、实际资源和取消终态，按实际结果评估继续上调的条件。

#### 128K 取消终态（2026-10-24 20:34）

签名测试提交 `6f693705640dd92cad25f4592bc452ecd42b6c3a` 的
[CI 40070690531](https://github.com/open-bazaar/contrib-codex/actions/runs/40070690531)
被 GitHub 作业六小时上限取消；注解明确为 `The job has exceeded the maximum execution time of 6h0m0s`。
Test 于 12:33:57Z 取消，作业于 12:34:33Z 结束。结果为 **2 项通过、1 项未完成、4 项未执行**，
没有完整 pytest 汇总，不能写成完整七项通过或五项失败。三个系统的静态检查及构建全部通过。
Gateway 实际 `n_ctx_slot=131072`，399 条 Codex usage 的有效窗口均为 124518。

| 用例 | 本轮实际结果 |
| --- | --- |
| ONCE | 通过。Seattle 原生天气结果、完整 NWS HTTP、参数及 ONCE MCP 审批一致，无追加反馈；驱动退出 0、pytest PASSED，耗时 1451.58 秒。 |
| SESSION | 通过。New York 经 SESSION 批准；Chicago 的渠道正文先到达，随后发送旧 New York 回复被 freshness hold 保存为草稿，原生命令结果返回了完整 Chicago 正文，然后才实际调用 Chicago，且无重复 MCP 审批。宿主 session、原生 thread 和活动 turn 相同；驱动退出 0、pytest PASSED，耗时 974.80 秒。没有将未发出的 New York 草稿算作已交付回复。 |
| 跨会话 ALWAYS | 未完成。仍在第一个 New York 上下文，尚未重启或进入 Boston／San Francisco。首次自然结束未调用 MCP；一次真实反馈后继续原 session／thread。全用例共 387 个不同 item 将 `mcp__weather__get_forecast` 当作 shell 命令，全部退出 127／command not found：首次一个，反馈后 386 次命令字节相同。没有实际天气 MCP 调用或 MCP 审批；最后 usage 为输入 66629、输出 53、合计 66682 tokens，未发生压缩。没有用尽八次反馈，也没有原生取消终态或驱动／pytest 完成结果。 |
| bcc、恢复、指令刷新、后台生命周期 | 四项均未执行；不引用旧轮次结果替代本轮覆盖。 |

三份实际事件文件共 3941 行，含 1991 个有效 AuditEvent、1941 个有效 HarnessOutput；
1939 个原生 provider 输出全部原样审计，关联标识一致。ONCE 和 SESSION 各有一条关闭阶段
宿主合成 cancelled 缺少 AuditEvent，原输出保留。四个已完成检查点逐包重放一致，未伪造
ALWAYS 反馈后的完成检查点。三份完整天气 HTTP 与原生结果一致；六次审批为四次 ONCE 命令
批准、一次 ONCE MCP 批准及一次 SESSION MCP 批准，路由、原生请求、决定及执行一致。
本轮没有拒绝或 ALWAYS MCP 审批。三个真实 outbound 均有确认回执，全部属于 ALWAYS 用例。

399 个已完成后端请求与 399 条原生 usage 的未缓存输入及输出 token 全部一一匹配；
另有一个实际取消请求，以及一个仅留存启动／prefill、没有完成或取消记录的请求。
ONCE 和 SESSION 两个节点正常退出 0、存活自有子进程为空；ALWAYS 缺少 cleanup.json。
Runner 最终记录终止七个遗留进程，其中包括该项节点 PID 29382，不能据此宣称其正常退出。
Upload、Post Gateway 成功，两个网关容器、网络和卷均记录移除。

资源文件包含一个实际模型元数据和 1434 个快照：12:34:15.321Z 最后快照前，每次均为
同一容器／PID，最长间隔 15.084 秒，没有读取错误或 OOM 计数。
模型 cgroup 内核已记录峰值 **8406429696 bytes（7.83 GiB）**，配置上限 10 GiB；
Magpie 峰值 131739648 bytes（125.64 MiB），配置上限 512 MiB。
模型 init 进程的采样 RSS／报告 HWM 为独立口径，不能替代容器 cgroup 用量。
强制取消未留存 after 快照或 pytest 原始退出码，最后快照也早于容器清理，因此资源审核
保留未完成状态，不宣称覆盖整个终止区间的最终峰值或正常退出。

本轮在 66682 tokens、低于 98304 压缩线时已因重复错误调用耗尽六小时。
因此本次先停在 128K；模型标称 262144 和已观测内存余量不能证明更高窗口能稳定完成验收。
更高窗口未执行；后续需要先取得逐请求可调用工具定义与实际模型 HTTP，定位错误调用路线。
ALWAYS 的 weather 启动事件为 ready／error null，但 ready 本身不能排除工具定义暴露问题，
不能仅凭错误 shell 调用认定 provider 或模型的最终根因。

原始产物 `bcc-acceptance`，ID `12560008845`，安全解压后原文件保留，下载 SHA-256 与 GitHub 摘要匹配：
`73bf6ea75f0d4d0514658da539d7367a77a6aa3eacd32454aa58db2dab6742b7`。
按用户最新要求交付简版 Markdown，只保留五次真实提问和三条已发送回复；前三条成功天气查询
以工具结果为结束边界，未生成最终文字回复，文档已注明。UTF-8 BOM、原文、次序和完整覆盖
均独立核对，工具日志与完整审计继续在原始产物留存。后续文档 amend 不作为已重新测试的 head；
生产源码、配置、原七场景及失败边界均未因本次结果修改，PR 保持草稿。

### 缺项反馈与更多尝试（2026-10-23 14:49）

开发线程 `1c0c10af-0988-5c09-9d57-2ca239ed6b05` 要求对尚未调用的步骤继续提示并增加机会，
`4917d39f-4096-58e5-a4d8-edc1f6a40113` 指定沿用 VA／IL 的“还未完成什么，请完成什么”方式。

原验收成功条件保持不变。对自然结束、仍无目标工具调用和 MCP 审批的尝试，明确反馈
“还未拿到本次预报，请调用 weather 的 get_forecast，使用本次经纬度获取实际结果”；
最多八次实际入站反馈，每次继续核对 canonical receipt、原生 userMessage 及原 session／thread。
目标 MCP 返回后仍立即核验参数、完整 HTTP、结构化结果及授权范围，不等待整轮自然结束。

每个检查点记录反馈上限，原始入站和进度继续保存每次真实反馈与结果。工具失败、参数错误、
审批错误和自然回合失败不能用增加反馈次数掩盖。独立场景隔离、压缩不混入功能验收的规则、
异步收信及正常资源清理保持原状；上轮压缩前只发出一至两次反馈的事实不改写为预算耗尽。

- [x] 更新反馈文案与上限；Ruff、15 文件格式、Pyright 零错误、七用例收集与 provider wheel／sdist 构建通过。
- [x] 签名推送后复跑实际验收，核对完整产物，记录实际反馈次数、MCP 调用、授权复用结果及剩余缺口。

#### 本轮完整结果（2026-10-23 18:56 终态）

签名提交 `b350c3980985b83a4ff19d97e38d28273a142640` 的
[CI 39914397135](https://github.com/open-bazaar/contrib-codex/actions/runs/39914397135)
整体失败：**5 项通过、2 项失败／未完成，14408.34 秒（4:00:08）**。
Linux、macOS、Windows 完整日志中的 Ruff、格式、Pyright、发行构建及 rc4 安装检查全部通过。

| 独立用例 | 本轮实际结果 |
| --- | --- |
| ONCE | 通过。Seattle 的实际 `weather.get_forecast` 成功即结束检查；ONCE 审批、经纬度、完整 NWS HTTP 响应和结构化结果一致。 |
| SESSION 复用 | 失败／未完成。New York 初始及前两次反馈均自然完成且没有目标调用或 MCP 审批，第三次实际反馈后于 16:06:31.202 开始压缩；尚未取得该用例的天气审批，Chicago 未到达。 |
| 跨会话 ALWAYS | 失败／未完成。New York 经 SESSION 批准成功；正常关闭后以新数据库、同 CODEX_HOME 重启，Boston 在新上下文重新批准 ALWAYS 并成功。第三次启动后的 San Francisco 初始回合自然完成但没有目标调用，一次实际反馈后于 17:19:35.580 开始压缩，持久复用未验证。 |
| bcc | 通过。真实读信、steering、同上下文追问、命令批准与拒绝有完整记录；提醒对应实际入站锚点，5.009 秒后触发。 |
| 恢复及活动关闭 | 通过驱动断言。初始及恢复后对话完成并保持原 session／thread，实际 reconcile 为 confirmed；原 app-server PID 99869，替代 PID 不同由驱动断言。新活动回合 started 后关闭，保存同逻辑回合的宿主 cancelled。 |
| 指令刷新 | 通过。已提交驱动在 AGENTS.md 变化后继续对话，两个自然回合均完成，刷新使用新的 session／thread。 |
| 后台生命周期 | 通过。真实后台终端与原生 item／process ID 一致，完成态恢复后仍存活；后续活动回合关闭后收到所属原生 interrupted，持久历史恢复为 failed／INTERRUPTED；停止与资源清理通过。 |

完整产物逐条核对了 **4 次实际反馈**：独立 SESSION 3 次、San Francisco 1 次。
文案均明确缺失的预报、`weather.get_forecast` 与本次经纬度；canonical receipt、原生
userMessage、包顺序和原 session／thread 一致。每次反馈前均实际自然完成且没有目标调用、
目标启动或 MCP 审批。所有检查点记录上限 8；两项失败均因原有压缩边界提前终止，
没有用尽八次机会。压缩保留为功能未完成，未扩展为压缩验收通过。

6 个应用场景共有 **2409 个有效 AuditEvent、2229 个有效 HarnessOutput**；
2223 个原生 provider 输出全部在审计中原样保留，原生 method、item 与会话／回合标识一致。
另有 **6 条关闭阶段的宿主合成 cancelled 缺少对应 AuditEvent**：ONCE 1、独立 SESSION 1、
ALWAYS 3、恢复 1；原始输出保留，不含原生 thread／turn，不能作为原生 interrupted 证据。
因此完整输出审计覆盖仍不通过；未据此推断正式全局审计是否曾发出。

14 个进度检查点按原始包顺序重放，receipt→native userMessage、旧 item 排除、第一个
目标结果边界及审批／工具列表均一致。3 次真实天气调用的完整 HTTP、参数和结构化结果一致。
30 次审批的路由、原生请求、决定与执行一致：26 次 ONCE 命令批准、1 次 ONCE 命令拒绝，
天气 MCP 的 ONCE、SESSION、ALWAYS 各一次。7 条命令请求另提供 ALWAYS，但实际全部
选择 ONCE；命令 SESSION／ALWAYS 映射仍未覆盖。6 条 outbound 均有确认回执。

90 条实际 usage 对应 90 个完成后端请求，未缓存输入／输出 token 全部一一匹配，无重复快照；
另有 4 个实际取消请求独立保留，未发现其他未分类或关闭时未完成且未取消的请求。
8 次应用节点退出均为 0、自有存活子进程为空；ALWAYS 三个数据库不同、CODEX_HOME 相同。
后台另有 201 个有效 HarnessOutput、5 个关闭后的原生包，app-server 退出 0，存活子进程、
连接、待处理请求与交互全部清零。Upload、Post Gateway 成功，网关容器、网络和卷均移除。

证据范围继续保留：恢复替代 PID 未另存具体值；应用活动关闭缺少该回合的原生 interrupted
及最后入站的原生 userMessage，原生中断由独立后台用例证明。刷新后的完整原生指令载荷及
AGENTS.md 文件未单独归档。后台命令为 `python3 -m http.server 8000`、processId `57937`，
未保存 HTTP 响应或仅绑定 loopback 的核验；后续 HTTP 检查在 started 后中断，尚未完成。

唯一产物 `bcc-acceptance`，ID `12496083374`，run／head、安全路径、CRC 与文件类型已核对；
ZIP SHA256 与 GitHub digest 一致：
`85a743765c768e3c4891fa473104c0a2dc984ef3b934c1bc147a5821fd43be2a`。
pytest 的 `*current` 目录与原目录逐文件一致，未重复计数。完整日志和产物补齐浏览器采集范围；
最终收集后关闭仅属本次观测的两个自有标签，并验证已移除。

缺项反馈与增加上限的改动已完成实际验证，**整体功能验收仍不通过，PR 保持草稿**。
SESSION 复用、ALWAYS 持久复用与关闭输出审计覆盖继续登记为缺口。

### 持续异步观测 MCP 目标完成（2026-10-23 10:42）

开发线程 `1c8e11ee-5aeb-558f-ba1c-135bf8a7490c` 明确要求持续异步检测目标 MCP 调用，
目的达成后结束检查，无需等待模型自然结束整轮。

收信改为独立异步任务，原始数据包持续写入事件文件，再交给当前检查点判定。
实际目标 MCP 完成或失败后立即保存该结果；成功条件仍要求参数、真实 HTTP 返回、
结构化结果和审批范围匹配，调用失败与错误参数不会通过反馈重试掩盖。
进度中的 `completion=mcp_result` 与实际 `state=tool.completed/tool.failed` 明确区分于自然回合终态。

新的查询先通过真实 `channel.received` 找到 canonical message，再关联原生 userMessage 的
clientId 或实际 inbox notice。第二条 SESSION 消息允许 steering 到原活动回合，前一请求遗留的
工具和终态仍原样归档，不能误作新请求完成。保留原长 JSON 分包读取与最多三次同上下文反馈。

MCP 用例完成后通过测试所属 stdin 关闭隔离 bcn，收信任务继续运行至 Channel EOF，
记录实际取消与退出。跨会话授权在关闭后切换到新的临时 SQLite 数据库并重启，保留原 CODEX_HOME
以核对 SESSION 隔离和 ALWAYS 持久授权；每个所属节点的 PID、数据库、退出码和自有存活
子进程均记录在 cleanup.json。恢复与指令刷新改为各自独立的短自然对话，仍核对实际终态、
关联标识及原有安全刷新，不再与 MCP 的目标完成条件绑定。

- [x] Ruff、15 个文件格式、Pyright 零错误、七个独立用例收集及 provider／TestChannel 的 wheel、sdist 构建通过。
- [x] 签名推送并检查新 CI 每个实例的实际 MCP 截止点、异步收信、审批、上下文和正常清理；记录实际失败及证据边界。

#### 完整产物结果（2026-10-23 13:13 终态）

签名提交 `3ae7556a256ba56a984cd5f6b78fb87173372d45` 的
[CI 39893024978](https://github.com/open-bazaar/contrib-codex/actions/runs/39893024978)
整体失败：**5 项通过、2 项失败，7615.56 秒**；Linux、macOS、Windows 的基础检查全部通过。
持续异步检测已经在真实 MCP 结果处结束检查，没有等待模型自然结束。

| 独立用例 | 实际结果 |
| --- | --- |
| ONCE | 通过。Seattle 目标工具成功后立即保存检查点；参数、ONCE 审批、完整 NWS HTTP 与结构化结果一致，应用正常退出。 |
| SESSION 复用 | 失败／未完成。New York 首次自然结束但没有天气 MCP，第一次真实纠正保持原上下文，11:33:45.297 开始压缩。该独立用例未取得天气 MCP 审批，Chicago 未到达。 |
| 跨会话 ALWAYS | 失败／未完成。New York 经 SESSION 批准成功；正常退出后以新数据库、同 CODEX_HOME 重启，Boston 在新的上下文重新审批并批准 ALWAYS，工具成功。再次正常重启后，San Francisco 首次和第一次反馈均自然结束但没有调用，第二次反馈于 12:20:05.756 开始压缩，持久复用未验证。 |
| bcc | 通过。实际读信、命令审批与拒绝、steering、同上下文追问及交付有完整记录；提醒与原入站锚点一致，5.006 秒后触发。 |
| 恢复及活动关闭 | 通过驱动断言。初始及恢复后对话均完成，原 session／thread 不变，实际 reconcile 为 confirmed；记录原 PID 58537，并在驱动中断言替代 PID 不同。最后关闭新 started 回合，保存同逻辑回合的宿主 cancelled。 |
| 指令刷新 | 通过。已提交驱动在 AGENTS.md 变化后继续对话，两个自然回合完成，刷新使用新的 session／thread。 |
| 后台生命周期 | 通过。实际后台终端与原生 item／process ID 一致，完成态恢复后仍存活；后续活动回合关闭后收到所属原生 interrupted，历史恢复为 failed／INTERRUPTED，停止和资源清理通过。 |

实时浏览器缺失的 ALWAYS 300 行和 bcc 22 行，已由完整日志与产物补证。
6 个应用场景共有 2616 个有效 AuditEvent、2458 个有效 HarnessOutput；全部原生 provider 输出
均在审计中原样保留，宿主／原生标识、原生 method 与 item 内容一致。
**其中 6 条关闭阶段的宿主合成 cancelled 在产物中没有对应 AuditEvent**：ONCE 1、
独立 SESSION 1、ALWAYS 3、恢复 1。原始输出完整保存；它们不含原生事件及原生 thread／turn，
不能当作原生 interrupted，也不能声称所有输出审计全覆盖。未据此推断正式全局审计是否曾发出。

全部 13 个进度检查点按原始包顺序核对 receipt→native userMessage、旧 item 排除及第一个
完成边界，避免将收包 phase 标签误作队列消费范围。3 次实际纠正均进入原 session／thread，
此前尝试确实自然结束且没有目标调用；两次压缩均保留为未完成，不是三次反馈预算耗尽，
未将其登记为压缩验收。

22 次审批的路由、实际请求、决定与执行一致：18 次 ONCE 命令批准、1 次 ONCE 命令拒绝，
天气 MCP 的 ONCE、SESSION、ALWAYS 各一次。3 次天气工具的完整 HTTP 响应、参数和结构化
结果一致。全部 5 条 outbound 有确认回执。当前命令审批均选择 ONCE；一条请求还提供 ALWAYS，
但没有选择该分支，因此命令 SESSION／ALWAYS 映射未增加覆盖。

全部实例按实际事件时间合并的 67 条原始 usage 包含 1 条重复累计快照；剩余 66 个完成请求
与模型后端的未缓存输入／输出 token 一一匹配，另有 4 个实际取消请求单独保留。
没有未分类请求或关闭时仍未完成且未取消的请求。全部 8 次应用节点退出均为 0、自有存活
子进程为空；ALWAYS 的三次退出各有独立记录，三个数据库不同、CODEX_HOME 相同。
后台另有 202 个有效 HarnessOutput 和 5 个关闭后的原生包，app-server 退出 0，连接、待处理
请求、交互和存活自有子进程均清零。Upload 与 Post Gateway 成功，网关容器、网络、卷均移除。

保留证据边界：恢复用例的替代 PID 只有驱动断言，未另存具体值；应用活动关闭没有该回合的
原生 interrupted 或最后消息的原生 userMessage，原生中断由独立后台用例证明。刷新后的完整
原生指令载荷及 AGENTS.md 文件未单独归档。后台实际命令为 `python3 -m http.server 8000`、
processId `23034`，未记录 HTTP 响应，也未核验仅绑定 loopback；后续 HTTP 请求在 started 后
立即中断，不能声称已完成。

唯一产物 `bcc-acceptance`，ID `12479249561`，run／head 已核对，安全解包后的 ZIP SHA256
与 GitHub digest 一致：
`48bae0a234e45f116477d6c80d6a82e40459a516718f9716bc68606938dcf6d3`。
pytest 的 `*current` 目录与原始目录逐文件相同，核对时未重复计数。

本轮异步检测改动已实际验证，**整体功能验收仍不通过，PR 保持草稿**。
SESSION 复用、ALWAYS 持久复用和关闭输出的完整审计覆盖保留为缺口；没有新一轮盲目重跑，
没有因此更改生产 provider、Channel、模型、依赖或压缩策略。

初步拆分提交 `6fb3060917024fdd89c6c4b32a95f708e6e65d08` 的三系统完整日志均通过，
其 [中间 CI 39890748840](https://github.com/open-bazaar/contrib-codex/actions/runs/39890748840)
仍使用等待自然回合结束的旧判定，结果单独保留；本次继续按新的人工要求修改。

### 按独立场景隔离上下文（2026-10-23 10:26）

开发线程 `a301086b-3988-5b45-aaef-e17601f05023` 要求使用新会话或重启隔离 bcn，
避免将上下文不断累计到压缩；`4ddcc050-2ea5-5b2b-8a3b-734c5ccad73c`、
`b3ca2072-980f-5c6c-a871-a9195bb02634`、`000b3e20-e55c-55e4-b597-8b64d7d6aa3d`
明确压缩不属于当前功能验收，压缩事件观测应单独测试。

据此将 `test_installed_application` 参数化为五个独立用例；每次都实际安装并准备独立的
配置、数据库、CODEX_HOME 和工作区。ONCE 与恢复、SESSION 复用、跨会话 ALWAYS、
bcc 对话、后台生命周期分别执行。恢复原上下文、刷新成新上下文、SESSION 不外泄和
ALWAYS 持久授权等原有要求保留；查询结果验证、审批路由、真实 HTTP 记录、原始审计、
长 JSON 收信、最多三次同上下文反馈及退出清理逻辑保留。

主用例记录原始 `compaction.started` 后终止本项，明确保留为未完成；后续独立用例继续。
不提高压缩阈值或改变生产 provider、模型、依赖及默认指令。

两轮旧测试均已取消并分别归档，不能登记为验收成功：

- [天气 CI 39877948980](https://github.com/open-bazaar/contrib-codex/actions/runs/39877948980)
  的完整产物显示，Seattle 首回合重复调用 16 次天气 MCP，并在 09:05–09:25 发生压缩；
  后续 Chicago 回合在 09:58–10:11 完成另一次压缩，于 10:21 正常结束，再进入 Boston 的刷新回合。
  浏览器回放遗漏后续内容，先前依据“无新日志”作出的卡死判断已更正；以完整产物为准。
- [旧便笺 CI 39875967762](https://github.com/open-bazaar/contrib-codex/actions/runs/39875967762)
  在实际正常结束的回合后已经发送三次纠正，第三次反馈的回合仍在反复读信并经历压缩，
  未正常结束。保留全部成功、失败及未完成尝试，不增加第四次反馈。

两轮的 Upload 与 Post Gateway 均成功，但取消时未生成应用 `cleanup.json`，末尾存在
GitHub 对残留所属进程的清理记录，不能宣称正常退出及全部应用清理通过。完整复核确认：

- 天气产物包含 1389 条 AuditEvent、1249 条 HarnessOutput，输出在审计中原样保留；
  33 次审批的路由、原始请求、实际决定与执行结果一致。19 次真实天气 HTTP 查询均成功，
  原生 MCP 参数和结构化结果全部匹配完整 HTTP 记录；已完成的 Seattle、New York、Chicago
  三个检查点通过，Boston 未完成，San Francisco 未到达。
- 旧便笺产物包含 1854 条 AuditEvent、1677 条 HarnessOutput，原始事件与 43 次审批一致；
  前四个 MCP 检查点通过，最后一个检查点的首次及前两次反馈均未调用目标工具，第三次反馈
  未完成。三次反馈均有实际入站和原生消息关联，未把 shell 写文件视为 MCP 成功。
- 两轮分别有 52、68 个完成的后端请求，与实际请求用量一一匹配。原始重复快照各三条；
  天气两条、旧便笺一条零请求 token 的压缩上下文快照，均与同一原生回合、同一时间的
  `compaction.completed` 事件对应，单独登记并保留原文。两轮各有两个实际取消请求，
  服务退出时各有一个尚在 prefill 的未完成请求，均不计作完成。
- 真实五秒提醒分别于 5.011 秒和 5.006 秒触发，实际交付均有确认回执；
  网关所属容器、网络和卷已删除。缺失应用清理证据、未到达的活动关闭与后台检查仍不通过。

天气产物 `12474291953` 的 SHA256 为
`bdc0c87d1a40dcfdc487af50446065c93356e1328a0908460c956469414f8f46`；
旧便笺产物 `12473908188` 的 SHA256 为
`e53a69f792629db25c5d8755cdc796e4deff45579a27ece94a0837f7b1910f17`，均匹配 GitHub 摘要。
新用例结果不会替代这些旧证据。

- [x] 五个独立用例全部收集，Ruff、15 个文件的格式检查、Pyright 零错误及 provider／TestChannel 的 wheel、sdist 构建通过。
- [ ] 签名推送并复核新 CI 每个实例的完整行为、审计和清理。

### 使用常规天气查询 MCP（2026-10-23）

开发线程 `8381b63a-cdae-527f-bc89-50fa699d8476` 指出便笺场景可能诱使模型直接使用 bash，
允许更换工具类型；`2fd8c435-24c2-5ca3-bc6d-cca98fbad32d` 要求采用常见 MCP 例子。
按 [MCP 官方天气服务示例](https://modelcontextprotocol.io/docs/develop/build-server)，
将隔离 SDK 服务改为 `weather.get_forecast(latitude, longitude)`，实际查询美国国家气象局
`api.weather.gov` 的 points 与 forecast 接口，返回最近三个预报时段。
不预置天气读数，不修改模型、生产 provider、依赖或其默认指令。

五个检查点分别自然询问 Seattle、New York、Chicago、Boston、San Francisco 的天气，
请求提供对应坐标。依次继续核对 ONCE、恢复原会话后的 SESSION、同会话授权复用、
刷新后 SESSION 隔离与 ALWAYS、再次刷新后的持久授权。未调用时仍只允许最多三次真实
入站反馈；一旦调用或请求 MCP 审批，立即核对，失败不通过重试掩盖。

验收要求实际原生 `weather.get_forecast` 调用、准确参数、成功结构化结果、实时 HTTP 返回和
审批范围一致。`weather-requests.jsonl` 在助手工作区外保留真实请求、HTTP 状态、完整服务
响应及返回结果；各阶段进度保存对应查询摘要。不比较固定天气、固定自然回复或工具顺序。
通过 shell 查询到天气也不代替目标 MCP 调用。原有 bcc、五秒提醒、拒绝、steering、会话恢复、
指令刷新、活动关闭和顺序后台核对保留。

- [x] 对 Seattle 与 New York 的真实 points／forecast 查询均收到 HTTP 200，未使用模型回合。
- [x] 使用实际 SDK stdio 客户端核对五组查询、工具定义与完整响应记录，35 项真实 HTTP／MCP 结果检查全部通过。
- [x] Ruff、15 个文件的格式检查、Pyright 零错误，以及 provider／TestChannel 的 wheel、sdist 构建通过。
- [x] 源码复核确认结果验证器之外的 19 个断言、审批 scope 分支、长行收信与其余执行场景不变；总计仍为 25 个断言。生产 wheel 字节不变。
- [ ] 签名推送并检查新 CI 的实际调用、审批、审计、用量、后台和清理产物。

更换场景不抹除既有便笺失败；收信修复 CI `39875967762` 在 `4bc34a9` 上的三系统检查
已通过，端到端后来取消并独立保留，不并入天气场景的通过结论。

### MCP 未调用时的入站反馈（2026-10-23）

开发线程 `c9f7029b-c82b-5bd5-86c3-ebe9042258c6` 要求在没有实际工具调用时继续入站反馈，
最多反馈重试三次，并询问启动时设定 goal 是否可替代。此前 work 线程
`8526c692-b89e-5d56-b22a-bd7dc5c4a1d9` 与 `01a12e5c-a734-74f0-b5c2-0c2e9f56b4c3`
已讨论通过 missing／Completion 反馈继续未完成的直播复盘。当前实现也会检查实际提交，
未提交时沿原会话反馈；本次仅将该方式用于隔离验收对话。

五个 MCP 检查点保留原始自然请求。正常完成的回合如果没有出现目标 MCP 工具的启动、
完成或失败事件，且没有 MCP 审批，驱动通过原 TestChannel 再发送“未观察到调用，请使用
便笺 MCP 工具保存指定正文”的反馈。首次请求之外最多再反馈三次，每次核对宿主 session
与原生 thread 均保持一致。四次均未调用仍按原断言失败。

一旦出现实际调用或 MCP 审批，立即执行原有检查；工具失败、参数或结构化结果错误、文件
内容不符、审批范围错误和会话变化不会因重试被掩盖。真实调用、保存结果、ONCE／SESSION／
ALWAYS、授权隔离、恢复、后台及清理要求均保留。

每次尝试分别保存到 `progress.json` 和完整事件流，反馈原文与真实入站标识保留在
`inbound.json`；新增 `mcp-checkpoints.json` 关联各检查点的全部尝试、反馈次数和实际验证结果。
首次成功与反馈后成功按实际次数分别记录，过去 rc4 的失败及 rc3 的独立成功保持原结论。

[OpenAI Docs：Goals](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)
说明 goal 属于原生 thread 的持续目标，自动继续受空闲状态、预算和工具调用等条件限制，
不能保证必然调用 MCP。当前公共 Harness 接口没有 goal 操作；本次使用已支持的入站反馈，
不为验收场景扩展生产 provider 或改变原生 goal 状态。

- [x] Ruff、15 个文件的格式检查、Pyright 零错误，以及 provider／TestChannel 的 wheel、sdist 构建通过。
- [x] 源码复核确认原 24 个断言和完整 `verify_note` 保留，新增一次重试会话归属检查，五个检查点均使用有界反馈。
- [x] 复核首次反馈 CI 的完整日志、产物、审批、保存结果及失败路径清理，保留未完成的第一次反馈。
- [ ] 修复测试收信的长 JSON 行处理和未完成尝试登记后，核对后续 CI 的完整验收结果。

[首次反馈 CI 39871461717](https://github.com/open-bazaar/contrib-codex/actions/runs/39871461717)
对应签名提交 `5f09d4fbb8193cef15954993c7434a6c437abb4f`。三系统检查全部通过，
完整日志确认实际安装 12 个已发布 rc4 包；端到端测试于 2065.26 秒后失败。
产物 `12468441528` 的 SHA-256 与 GitHub 摘要一致：
`4d65e24a6796e5078f5e03f5c66cd7b8ce2db310551b4359352588c192a181bd`。

前三个检查点均首次成功：ONCE 保存 `Drink water.`，恢复原会话后 SESSION 保存
`Take a short walk.`，同会话无重复 MCP 审批保存 `Stretch.`。刷新后的新会话未调用
`notes.save_note`，而是通过 shell 写入 `Read a book.` 并附带换行；该文件变化不算 MCP 成功。
驱动随后实际发出第一次纠正反馈，真实入站及原生用户消息一致，session／thread 均保留。

第一次反馈回合进行了资源、模板枚举和多次 MCP CLI 安装尝试，没有实际 `notes.save_note`
或 MCP 审批。最后一条安装命令产生大量输出，测试收信端在 `reader.readline()` 遇到
默认 64 KiB 行限制，抛出 `LimitOverrunError`／`ValueError`。该回合未保留终态，第二、三次
反馈及后续检查未执行，不能记为已耗尽三次反馈，也不能记为验收通过。原清单在 `finish`
返回后才登记尝试，因此只包含已完成的原始请求；第一次反馈由完整入站和实际事件单独关联，
明确保留为未完成尝试。

4385 个已保留数据包包含 2230 条有效 AuditEvent、2133 条 HarnessOutput、21 次审批和一次
确认交付。已保留输出均在审计中原样保存，原生关联及审批往返一致；20 次批准、一次拒绝，
19 次命令审批均仅提供 ONCE，MCP 审批分别为 ONCE、SESSION。六个应用回合完成，第一次
反馈回合仅启动；两次 steering 分别发生于 bcc 和恢复后的回合，五秒提醒实际 5.007 秒触发。
保留所有 CLI 参数和安装错误。56 个已完成模型请求均匹配原生用量；60 条原始 usage 中有
三条重复累计快照，以及一条与压缩完成事件同刻、累计总量不变的零 last 重置通知，原文保留，
仅在模型请求核对时分别分类。另有三次取消请求，无未分类请求。

失败驱动退出码 1，底层应用退出码 0，自有存活子进程为零；Upload、Post Gateway 成功，
所属两个容器、网络及卷均删除。超长数据包及反馈终态未保留，因此只对已保留事件逐项核对，
不声称已获得该回合完整事件流。ALWAYS、跨会话授权隔离、持久授权、活动关闭及顺序后台
仍未获得本轮结果；失败请求的完整工具模式未保留，未据此认定生产 provider 缺陷。

后续修复只调整验收驱动：分块读取 TCP 字节，按换行拼接完整 JSON 数据包，跨检查点共用
同一个解析迭代器；等待回合前即将尝试写入 `mcp-checkpoints.json`。不裁剪长输出、不增加
任意行长上限，不改变首次加最多三次反馈、原有 25 个断言或 `verify_note`。
真实 TCP 回放核对 4385 个原始事件数据包及原产物 progress 文档；后者序列化为 74216 字节的
单行 JSON，用于验证长行收信。4386 个 JSON 记录全部原样解析，跨回合暂停后无丢失。
这是收信验证，不是新的模型或验收成功。Ruff、15 个文件格式和 Pyright 检查通过；
生产 provider、依赖、工作流和原始自然请求均不改变，旧失败及 rc3 独立成功继续保留。

### rc4 回归基线（2026-10-23）

开发线程 `24985576-8375-5000-b2bb-f67cab747cff` 要求主仓库合并后发布新 rc，
`c7806b76-9b2a-5d01-adc8-85abbe631ab4` 要求 Codex 插件使用该版本触发新测试。

[BCN rc4 发布流程 39824761350](https://github.com/yuchanns/bazaar-compute-node/actions/runs/39824761350)
基于 PR #119 合并结果，固定发行提交为 `fc873d87ba585b5123a518f9cde28453c8a8cb4d`，
标签为 `v0.3.0rc4`。28 个包、56 个文件的 PyPI 哈希与固定产物一致；三系统 wheel、sdist
安装及最终 PyPI 干净安装通过，34 项流程检查全部完成。首次上传后的安装检查遇到下载索引
延迟；普通索引齐全后，原流程复用固定产物完成失败环节与标签收尾。

开发依赖、隔离应用安装和 TestChannel 的四项 BCN 依赖下限统一更新到 rc4，锁定 12 个实际
发布的 BCN 包；其 24 个 wheel／sdist 哈希与主仓库发行清单一致。项目隔离环境中实际安装
版本均为 rc4，公共 MCP 检查类型可正常导入。第三方依赖版本保持原锁定结果。

MCP 自然便笺、原生调用与文件证据，以及独立 bcc、审批、恢复、指令刷新、后台与清理场景
继续沿用已调整的验收条件。Provider wheel 与上一轮构建逐字节一致。

- [x] 核对 rc4 发布、固定提交、PyPI 哈希和实际安装版本，更新测试安装及锁文件。
- [x] Ruff、15 个文件的格式检查、Pyright 零错误，以及 provider／TestChannel 的 wheel、sdist 构建通过。
- [x] 推送并核对新 CI 的三系统实际安装与完整端到端产物，记录失败及未执行范围。

[rc4 CI 39829293944](https://github.com/open-bazaar/contrib-codex/actions/runs/39829293944)
对应签名提交 `6ac0e6c994f1be0591a7cad543cfd8ac1c11c18c`。三系统完整日志均确认安装
12 个实际发布的 rc4 包，静态检查与发行构建通过；端到端测试于 3740.86 秒后失败。
完整日志、产物和清理记录已复核，本轮不能登记为全部验收通过。

完整产物 `bcc-acceptance`，ID `12452680574`，SHA-256 与 GitHub 摘要一致：
`adb444e3d480ad7cc6115349a3b3a2b7fc4e8c0853791fbce54b7f952f510ba1`。
其中 639 条 AuditEvent、537 条 HarnessOutput 全部通过实际 rc4 公共类型验证，输出在审计中
原样保留，原生标识、方法与审批关联一致；`current` 目录与实例 0 完全相同，按一个实例核对。

前三个 MCP 检查点通过：`Drink water.` 经 ONCE 批准保存；恢复原会话后
`Take a short walk.` 经 SESSION 批准保存；随后同会话 `Stretch.` 无重复 MCP 审批。
三次原生 `notes.save_note` 的参数、结构化结果及各阶段真实文件内容一致。

失败发生在指令刷新后的新会话 `refreshed`。原生 `notes` 服务报告 ready，模型完成了 bcc
读信并交付“Note updated to: Read a book.”，普通终态也声称保存完成，却没有实际 MCP 调用
或 MCP 审批；检查点和最终文件都仍为 `Stretch.`。`verify_note` 正确拦下该结果。
因此 SESSION 的跨会话隔离、ALWAYS 授权及再次刷新后的持久授权未获本轮证据，后续
活动回合关闭与顺序后台场景没有执行。模型回信成功不代表便笺保存成功。

20 次原生审批包含 18 次仅提供 ONCE 的命令审批和两次 MCP 审批，19 次批准、一次拒绝；
目标、发送者、决定、scope 与原生执行一致，被拒命令为 declined 且未创建进程。
保留四次 CLI 参数错误，退出码均为 2。六个应用回合完成，两次 steering 进入所属 bcc 回合；
五秒提醒实际于 5.009 秒触发，三条回信均有确认交付，其中包括上述未实际保存的错误声明。
自有 app-server 退出后的恢复沿用原 session／thread，测试亦确认替换后的所属进程 PID 变化；
指令刷新创建了不同的新上下文。

30 个已完成模型请求全部匹配 30 个不同的原生累计用量快照。31 条原始 usage 中有一条
携带新回合标识的上一累计快照；原文保留，仅在后端请求比对时按所属线程及完整快照计一次。
另有两次取消请求单独记录，没有未分类请求。

失败驱动退出码为 1，底层 BCN 应用退出码为 0，自有存活子进程为零；Upload 与 Post Gateway
成功，所属网关两个容器、网络及卷均已删除。这证明本次失败路径的清理，不代替未执行的
活动关闭与后台生命周期检查。

实际发现结果保留 Codex 0.163.0、8 个模型、5 个技能；公开 inspect 的 `mcp_servers` 为空。
三个实际 MCP 调用证明其已观察到的可用性，但 ready 状态和早先成功均不能证明失败请求中
完整工具模式的内容；该次模型请求未保留工具模式，现有记录不足以判定生产实现存在缺陷。
本次只登记完整证据，保留失败与原有断言，不修改生产实现或盲目重跑模型。

#### 前一轮 rc3 独立结果

前一轮 [rc3 CI 39822943500](https://github.com/open-bazaar/contrib-codex/actions/runs/39822943500)
使用提交 `a5d2c1704d560b7612c840127f23121a7f9a4db3`，四项 CI 检查全部通过，
端到端耗时 2878.61 秒。以下完整复核结论仅对应该 rc3 提交，不替代上述 rc4 未完成的验收范围。

产物 `12448157474` 的 SHA256 为
`3661208a80cc7e4e13e14404b600e8f5fb4ac945a311b716f896344a8d9934ea`，与 GitHub 摘要一致。
完整事件包含 894 条有效 AuditEvent、775 条 HarnessOutput；全部输出在审计中原样保留，
原生会话、回合、方法及审批关联均一致。产物中的 `current` 目录与实例 0 完全相同，按同一实例核对。

五项 MCP 检查点均通过：首次 `Drink water.` 经 ONCE 批准；恢复原会话后
`Take a short walk.` 经 SESSION 批准；同会话 `Stretch.` 不再请求 MCP 审批；刷新为新会话后
`Read a book.` 重新请求并批准 ALWAYS；再次刷新后 `Rest your eyes.` 无重复 MCP 审批。
参数、结构化返回与各阶段真实文件内容一致。bcc 提醒阶段还发生一次经 ONCE 批准的
`Drink water.` 保存，因此共六次真实 `notes.save_note` 调用、四次 MCP 审批。

21 次原生审批包括 17 次命令审批和上述四次 MCP 审批，20 次批准、一次拒绝；拒绝的命令
原生状态为 declined，未创建进程。保留四次 CLI 参数错误与后续成功行为。五秒提醒实际
5.007 秒触发；两次 steering 进入同一 bcc 回合，四次回信均有确认交付。所属 app-server 恢复
后沿用原会话，两次指令刷新创建不同新会话。七个应用回合完成，第八个启动后关闭应用；
应用退出码 0，自有子进程全部释放。

后续独立后台检查实际创建持久终端，完成回合后与从历史恢复后均报告后台活动；同会话
新回合启动后关闭 stream，原生中断事件与恢复出的 interrupted 终态一致。stop_session 确认，
app-server 退出码 0，自有子进程、连接和待处理请求均为零，所属网关容器、网络和卷清理完成。

全部 36 个已完成模型请求与 36 个不同原生累计用量快照匹配。38 条原始 usage 中有两条
重复累计快照，包括一次携带后续回合标识的上一累计值；原始事件全部保留，仅在后端请求
比对时按所属线程及完整快照计一次。另有两次取消请求单独记录。

该轮 MCP 的 ONCE／SESSION／ALWAYS 与持久授权验证通过，命令审批原生仅提供 ONCE，
其 SESSION／ALWAYS 仍未实测；应用第八回合的终态未进入已关闭的 stream，后台原生检查
单独证明了中断历史。按上述实际范围记录通过结论。

### MCP 验收条件澄清与本地验证（2026-10-23）

开发线程 `c7718d01-eb8a-5928-a715-80935e8739b3` 明确：这次 MCP 补充的目标是工具可见与
真实调用，回不回信不影响该项结果，bcc 已有独立验证。
`585df6ec-2485-5f38-a0db-10fb40414bf0`、`569acbcd-1c62-5592-aa19-66f8c6133ccf`
要求据此调整验收条件、推送仓库并触发 CI。

MCP 场景使用自然便笺请求，不在请求中指定服务名、工具名或 bcc 操作。通过条件为实际出现
`notes.save_note` 原生调用、工具成功完成、参数与结构化结果及真实便笺文件一致；相关审批与
会话边界继续按所提供的 scope 核对。模型的普通文字、回信数量和回复措辞不作为该项门槛，
也不能用模型自述已保存代替原生执行与文件证据。

`mcp-once` 现在是单独的首个请求；提醒、steering 与命令拒绝继续由随后独立的 bcc 请求验证。
恢复、SESSION／ALWAYS、跨会话、活动关闭及后台检查保留；后续 MCP 请求也只自然修改便笺，
不再附加摘要或回信要求。生产实现、默认开发者指令和全局运行环境未改。

本地先以独立 CODEX_HOME 的 Codex 0.163.0 接真实 Qwen3.5-4B 验证，再通过隔离安装的实际
BCN 0.3.0rc3、当前 provider wheel 与真实 TCP TestChannel 发送同一句
`Please save a note: Drink water.`。两次均真实调用 `notes.save_note`，参数、结构化结果与
便笺文件均为 `Drink water.`。被动记录的实际模型请求包含 `mcp__notes.save_note(text)` 定义。
模型、网关与 MCP 服务均真实运行，没有替换执行结果。

BCN 本地场景含 91 条有效 AuditEvent、80 条 HarnessOutput，输出在审计中原样保留；
一次 bcc 读信命令与一次 MCP 调用均经 ONCE 批准成功，三个实际后端请求匹配三条 usage。
模型最后输出完成说明而未执行 `bcc message send`；按上述人工澄清的范围，该事实不影响
MCP 可见与调用通过。原诊断驱动曾额外要求回信而退出 1，原始记录保留，未重跑模型或改写原始结果。
该回合正常完成，BCN 退出码 0，自有子进程、容器、网络与卷全部清理；全局 Codex 程序、配置
和认证文件前后哈希相同。本地证据不替代后续 CI 的审批作用域及生命周期结果。

- [x] 按明确的 MCP 范围核对本地真实调用与工具定义，调整自然请求与独立检查点。
- [x] Ruff、15 个文件的格式检查、Pyright 与 provider／TestChannel 的 wheel、sdist 构建通过。
- [x] 核对 rc3 CI 39822943500 的实际结果和完整产物，登记 MCP、bcc、生命周期与剩余范围。

### MCP 简单场景补充（2026-10-22 22:40）

开发线程 `b51a1f95-6e85-5700-9c88-d08f9bdafd25` 要求简单 MCP 场景；
`7c60297c-c3f2-5ee8-ba7b-0e950739948d` 指出旧版会拒绝 MCP，随后
`e97c4791-85ca-54e9-ae72-cfa4c062b3e8` 明确以验证现有修复为主。

在原有隔离应用对话内加入一个实际本地 MCP 工具 `notes.save_note`，只替换真实便笺文件并返回
路径和正文。分别核对 ONCE、SESSION、ALWAYS 的原生审批、Channel 决定、工具完成与文件内容。
同会话追问验证 SESSION 后无重复工具审批；指令刷新后的新会话验证 SESSION 不外泄，
再批准 ALWAYS，并在下一次指令刷新后的新会话验证持久授权生效。原有 bcc、拒绝、steering、
恢复、后台与资源释放检查保留。生产代码仅在真实失败证明存在问题时修改。

本地已通过真实 MCP SDK 的初始化、工具发现与 `save_note` 调用，结构化返回与实际便笺文件一致。
这是服务端协议检查，尚不能代替 Codex → Harness → Channel 审批的端到端验证。

- [x] 新场景通过静态检查及发行构建，完成首轮真实 CI 并保留失败证据。
- [x] 下载完整证据，核对审批、执行结果、会话边界及资源清理，登记实际结果与未通过项。

[首轮 MCP CI 39798753126](https://github.com/open-bazaar/contrib-codex/actions/runs/39798753126)
在 `fe3cb4028c64a83662dfd5e79faf197f399eaf4e` 上三平台检查全部通过，端到端测试于
3474.66 秒后失败。完整产物中的 978 条 AuditEvent、887 条 HarnessOutput 均通过公共模式验证，
全部输出在审计中原样保留，原生关联标识一致；28 个已完成后端请求全部匹配实际 usage。

简单 MCP 调用已实际通过：`Drink water.` 经 ONCE 批准保存，恢复原会话后 `Take a short walk.`
经 SESSION 批准保存，随后同会话的 `Stretch.` 无重复 MCP 审批。三次调用的参数、结构化返回与
各阶段实际便笺内容一致。16 次原生审批包含 14 次命令审批及上述两次 MCP 审批，批准、拒绝、
作用域、发送者、目标与原生执行结果全部一致；五个回合完成，两次 steering 被接受，五秒提醒
实际触发，四条回信交付。

失败发生在指令刷新后的新会话：原生 `notes` 服务报告 ready，但模型尝试不存在的
`bcc notes save_note`、`bcc save_note`，随后声称成功；该回合没有原生 MCP 调用或审批，
实际文件仍为 `Stretch.`。因此 SESSION 不跨会话与 ALWAYS 的持久授权尚未验证，后续活动关闭
及顺序后台场景也未执行；不将已有成功扩大为本轮完整通过。应用仍以 0 退出，自有子进程清零。
ready 状态不等于该次模型请求完整工具列表的证据，产物未保留后者；本次失败未证明生产实现存在缺陷。

原始产物 `bcc-acceptance`，ID `12437858575`，下载后 SHA-256 与 GitHub 摘要匹配：
`b860588e28cc078161c56af9075de553a296c778b99f006c29915ab5e3d43fc4`。
重跑仅把自然请求明确为 `notes` MCP 服务的 `save_note` 工具；原有审批、文件、结构化结果、
会话边界及后台断言全部保留，生产源码未改。

明确工具名后的 [CI 39808941734](https://github.com/open-bazaar/contrib-codex/actions/runs/39808941734)
在 `be3ee8df3bf33e97b20080c1029c0359a894dda2` 上三平台检查全部通过，端到端测试于
2040.46 秒后失败。本轮完整日志与产物已核对，MCP 全部新增要求仍未验收通过。

失败提前发生在首轮 `verify_note(first, "Drink water.", ONCE)`：原生 `notes` 服务报告 ready，
模型仍尝试不存在的 `bcc message save_note`、`bcc notes save_note`，然后查询内置 MCP 资源与
资源模板列表；两者为空后，模型声称工具不可用。没有实际 `notes.save_note` 调用或 MCP 审批，
首轮检查记录的便笺为不存在。资源、资源模板与可调用工具不同，空资源列表不能证明工具不可用；
该次模型请求的完整工具模式仍未留存，故本轮未证明生产 provider 的拒绝或工具注入存在缺陷。

完整产物含 361 条有效 AuditEvent、332 条 HarnessOutput，所有输出在审计中原样保留，
宿主与原生关联标识一致。八次命令审批均仅提供 ONCE，批准、Channel 路由与实际执行一致；
三次命令参数／子命令错误保留退出码 2。12 个已完成后端请求全部匹配 12 条实际 usage，
两次 steering 取消的后端请求另行保留。一个回合完成，两次 steering 被接受，五秒提醒于
5.015 秒后实际触发，但没有向 Channel 交付回信。应用退出码 0，自有存活子进程为零。

本轮没有到达后续拒绝、恢复、刷新、SESSION／ALWAYS、跨会话、活动回合关闭及顺序后台场景；
首轮已证明的三次 MCP 保存与更早后台证据保持其各自适用范围，不替代本轮缺失结果。
所有 24 个验收断言和生产源码保持原样；不凭模型声称不可用修改生产实现，也不继续盲目重跑。
后续需先取得可调用工具发现与实际模型请求工具列表的证据，区分工具可见性和模型选择，
再决定具体改动；公共 inspect 的 provider 适配仍等待公共契约 task 的人工 review。

本轮原始产物 `bcc-acceptance`，ID `12442230238`，下载后 SHA-256 与 GitHub 摘要匹配：
`4f0b0f4e74eb1a1222da2d0ce28b26ba07c90058ab667e402c5e93f7cf75bcc9`。

- [x] 按公开契约确认七项验收标准，并准备单实例自然对话场景。
- [x] 完成 Ruff、格式、Pyright 与 provider／TestChannel 发行构建检查。
- [x] 核对七项标准的完整真实证据，登记通过结果及具体缺口。
- [x] 更新插件 PR 的实现说明和验收结果，提交人工 review。

## 已核对的真实结果

[CI 39776002953](https://github.com/open-bazaar/contrib-codex/actions/runs/39776002953)
在 `69bd0ff3b94e3b53b5ffa7197edc39f0b3671293` 上完成，三平台检查和真实模型场景均成功。
完整产物包含 633 条有效 AuditEvent、573 条 HarnessOutput；输出全部原样保留，原生关联标识一致。
读信、检查收件箱、五秒提醒创建与触发、真实回信、steering、连续会话、自有进程退出后的恢复、
指令变化后的新上下文均有实际记录。九次原生命令审批中八次批准、一次拒绝；被拒命令为
`declined` 且没有启动进程。应用在第五个活动回合中关闭，退出码为 0，自有存活子进程为零。

该轮 bcc 原生审批仅提供 `once`，未验证命令审批的 `session`／`always` 分支。原生查询已核对
后台空闲、查询不可用时保守返回有任务、恢复后空闲；真正的后台终端、活动回合核对与 stream
关闭后的中断终态由新增预览服务场景补测；修正后结果见下文。

原始产物为 `bcc-acceptance`，ID `12427355153`，SHA-256：
`f21c08617ef0616f8c51db0687abc35f149acd3b05b8bb295a881e4dcca7d7f8`。

[补测 CI 39782458065](https://github.com/open-bazaar/contrib-codex/actions/runs/39782458065)
在 `17c901b1f5a478bda75ec76e6377f8b361752be6` 上三平台检查通过，原有应用会话完成。
完整产物中的 625 条 AuditEvent 与 554 条 HarnessOutput 均验证通过，输出原样保留；
十次原生审批为九次批准、一次拒绝，三条真实回信交付。原生补测启动了实际 HTTP 后台终端，
其命令与原生 processId 在终端列表中留存，自有进程最终清零。

该补测因测试检查点错误失败：持久终端没有发出完成事件，直到回合正常完成才检查后台列表，
随后关闭的是已完成状态恢复出的流，因此没有新的中断终态可等。现将后台存活检查保留在
已完成回合后，另在同一会话的新活动回合核对取消与中断恢复。
该轮产物 ID `12428191961`，SHA-256：
`780d603cfc9ee9d809a4d4b56ffef54706868ee3b976c70f69f0e5588a7189b3`。

## 修正后完整核对结果

[CI 39786174210](https://github.com/open-bazaar/contrib-codex/actions/runs/39786174210)
在 `b98cd7f443928bc4c8b65c5ad46f068fca8952ce` 上四项作业全部成功。
Linux、macOS、Windows 的完整日志均确认安装 12 个实际发布的 rc3 包，Ruff、格式、
Pyright 与 wheel／sdist 构建通过。真实模型测试完成，耗时 1811.75 秒。

七项证据已逐项核对。其余六项在下列场景内通过；审批的批准、拒绝和 `once` 通过，
但本轮原生请求未提供 `session`／`always`，这两种命令授权映射仍未验证。
因此不将结果登记为无缺口的七项全通过，PR 保持草稿供人工 review。

| 能力 | 核对结论与实际证据 |
| --- | --- |
| 发现与配置 | 通过。实际 CLI 为 Codex 0.163.0；原生元数据查询取得 8 个模型、effort 与技能。应用原生设置确认工作目录、`local/local-model`、默认 effort、`on-request`、`workspaceWrite` 与网络策略；真实 bcc 执行证明所需环境可用。未宣称覆盖全部配置组合。 |
| 执行与输出 | 通过。完整应用产物含 533 条有效 AuditEvent、459 条 HarnessOutput，全部输出在审计中原样保留，原生会话与回合关联一致。四个应用回合完成；真实读信、检查、五秒提醒与三条回信均有执行及交付记录。提醒从创建到实际触发为 5.009 秒。 |
| 连续会话与 steering | 通过。两次 `turn/steer` 被接受，首回合含三条实际用户消息；后续回合继续使用原 Harness session 与原生 thread。回复按有效行为核对，未比较固定文案或工具顺序。 |
| 恢复与刷新 | 通过。终止自有 app-server 后，实际恢复保留原 session／thread，再完成对话。工作区指令变化后产生新上下文。另有真实原生工作区／主目录指令和技能变更通知到 HarnessExpire 的证据；当前五个生产源码文件与该证据对应的 wheel 字节一致。 |
| 审批往返 | 有覆盖限制。11 次原生命令审批经 TestChannel 返回 10 次批准、1 次拒绝；发送者、目标、session、turn、item、审计决定和原生执行结果全部一致。被拒命令为 `declined`，processId 为空。全部请求只提供 `once`；未验证 `session`／`always` 命令映射。 |
| 后台工作与资源释放 | 通过。原生终端列表与 tool.started 的 itemId、命令、processId 对应，预览回合完成及完成态恢复后 `has_background_job` 均为真。同会话的后续活动回合核对后关闭 stream，收到所属原生回合的 `interrupted`；再从持久历史恢复为 Harness `failed`／`INTERRUPTED`。`stop_session` 已确认，退出后存活自有子进程、连接、待处理请求与交互全部为零，app-server 退出码 0。 |
| 错误与调用预算 | 通过所列场景。实际 bcc 参数错误保留退出码 1／2，并映射为 tool.failed；审批拒绝没有启动进程。真实原生查询在进程暂停时按 0.1 秒预算返回 TIMEOUT／needs_reconciliation，恢复进程后查询成功，进程退出后返回 CONNECTION_LOST 并保留原始退出码。上述元数据／传输证据适用于字节一致的当前生产源码；本轮持久化中断恢复为 INTERRUPTED 且无需再核对。 |

后台原生场景保留另外 52 条通过公共模式验证的 HarnessOutput 和关闭后的 5 条原生事件；
它在应用退出后顺序执行，因此这些输出不属于前述应用 AuditEvent 总数。
实际后台终端为 `python3 -m http.server 8080`，原生 processId 为 `75673`。
已证明真实后台终端及其生命周期；未记录 HTTP 响应，也未核验仅绑定 loopback。
自然追问在原生 started 后立即被中断，不能把该追问视为已完成 HTTP 检查。
原应用在第五个活动回合关闭，只记录到 started；中断终态与历史恢复由原生补测单独证明。

完整 Post Gateway 日志中的 20 个已完成后端请求全部匹配实际 usage 的未缓存输入与输出 token。
21 条原始 usage 通知中有一次相同累计快照重复，原文保留，匹配请求时只计算一次。
另有 3 个实际取消的后端请求独立登记，不混入已完成请求的用量与耗时。
应用退出码为 0，无存活自有子进程；原生补测的清理结果同样全部通过。

完整产物 `bcc-acceptance`，ID `12430328255`，下载后 SHA-256 与 GitHub 摘要匹配：
`1a1dfc99ad5f8170ae2af6de9fb1d0ac21799b746c4f7752186d7dbfa186e702`。
本轮只修正测试检查点并登记证据，没有因该失败修改生产实现。
结论不扩展为全部 model／effort、沙箱、原生错误码或 API 分支的穷尽覆盖。
