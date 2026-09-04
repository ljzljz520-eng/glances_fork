# Glances 告警通知（邮件 / 钉钉 / 飞书）- 产品需求文档

## Overview

- **Summary**: 为 Glances 监控增加告警通知能力：当 CPU、内存、Swap、负载等核心指标越过阈值产生 WARNING/CRITICAL 告警事件时，通过邮件、钉钉群机器人、飞书群机器人主动推送通知；通知渠道与告警阈值均可在 WebUI 中配置并立即生效，配置持久化到独立 JSON 文件。
- **Purpose**: Glances 目前只能在 TUI/WebUI 界面上被动展示告警事件（footer 告警列表），运维人员不盯着界面就无法及时发现系统异常。需要主动推送通道，并让用户无需编辑配置文件、无需重启即可调整告警阈值和通知渠道。
- **Target Users**: 在服务器上以 webserver 模式（`glances -w`）或 standalone 模式运行 Glances 的运维人员 / 个人用户。

## Goals

- 支持三种通知渠道：邮件（SMTP）、钉钉自定义机器人 webhook、飞书自定义机器人 webhook（均支持加签校验）。
- 新告警事件产生时（WARNING / CRITICAL）向所有已启用渠道发送通知；同类告警有最小通知间隔防骚扰；WARNING 升级为 CRITICAL 时立即再次通知。
- WebUI 提供"告警设置"弹窗：配置渠道参数（含测试发送）、配置 8 个核心指标的 careful/warning/critical 三级阈值与启用开关、配置最小通知间隔。
- 阈值保存后立即对运行中的插件生效（热更新），重启后依然有效。
- 配置持久化到独立 JSON 文件（`notifications.json`，位于用户配置目录），不改动 glances.conf，在只读根文件系统环境（Docker/Snap）下可优雅降级。

## Non-Goals

- 不做告警恢复通知（事件关闭时不发通知）。
- 不做短信、Slack、企业微信、Telegram 等其他渠道。
- 不做通知模板自定义、告警聚合/排班/值班。
- 不对磁盘、网络、传感器等不产生告警事件的插件做阈值配置（配置了也不会触发通知，避免误导）。
- 不做用户/权限体系；配置写接口沿用 Glances 现有的认证模型（配置了 `--password` 时整站需认证）。
- 不改造现有 glances.conf 阈值体系与 TUI 告警展示逻辑。

## Background & Context

- 告警事件源：`glances_events` 单例（[events_list.py](file:///Users/kkcarrot/swe-project/glances_fork/glances/events_list.py)）。插件在 `get_alert(..., log=True)` 越过阈值时调用 `glances_events.add(state, type, value)`；仅 WARNING/CRITICAL 会创建事件（`_create_event`），事件持续期间 `_update_event` 更新，CRITICAL 为最高态且不会降级。
- 实际产生事件的指标（stat_name，即事件 `type` 的小写形式）：
  - CPU：`cpu_total`、`cpu_user`、`cpu_system`、`cpu_iowait`、`cpu_steal`（fields_description 中 `log: True`）
  - 内存：`mem`；Swap：`memswap`；负载：`load`（取 min15，阈值语义为"每核负载比值"，默认 0.7/1.0/5.0）
- 阈值运行时载体：各插件实例的 `_limits` 字典（键如 `cpu_total_critical`、`mem_warning`），启动时由 `load_limits(config)` 从 glances.conf 读取；`plugin.set_limits(item, value)` 可写回（自动加插件名前缀）。
- stats 生命周期：`GlancesStats.__init__` → `load_modules()` 完成所有插件加载；standalone、webserver、XML-RPC server 均使用 `GlancesStats`（client 模式使用独立的 `GlancesStatsClient`，本地不产生事件）。
- webserver 模式下 stats 随 API 请求按 `cached_time` 周期刷新（`__update_stats`），告警事件即在刷新周期内产生。
- REST API：FastAPI，路由集中在 `GlancesRestfulApi._router()`（[glances_restful_api.py](file:///Users/kkcarrot/swe-project/glances_fork/glances/outputs/glances_restful_api.py#L516-L572)）；GET 路由用 `route_mapping` 注册，POST 路由单独注册。
- WebUI：Vue 3 + Bootstrap 5（仅 CSS，无 bootstrap JS），全屏覆盖层模式可参考 help.vue；前端通过 `fetch("api/4/...")` 调接口。
- 依赖约束：核心 requirements 极简（psutil/fastapi 等均为可选），通知功能必须只用 Python 标准库（`smtplib`、`email`、`urllib.request`、`hmac`、`hashlib`、`base64`、`json`、`threading`），不得新增硬依赖。
- 安全约定（CLAUDE.md）：敏感字段（密码、secret、webhook 中的凭据）不得在未认证响应中明文返回，需脱敏；参考 `config.as_dict_secure()` 的做法。

## Functional Requirements

- **FR-1 事件监听钩子**：`GlancesEventsList` 提供监听器注册接口；新 WARNING/CRITICAL 事件创建时、以及 WARNING 升级为 CRITICAL 时，向监听器推送事件字典；监听器异常不得影响 stats 主循环。
- **FR-2 通知配置持久化**：通知配置以 JSON 存储在用户配置目录的 `notifications.json`；启动时自动加载；文件不存在时使用默认值；目录/文件不可写时记录日志并不影响 Glances 正常运行。
- **FR-3 邮件渠道**：支持 SMTP host/port、SSL（465）/STARTTLS（587）、认证用户名/密码、发件人、收件人列表（多个）；邮件内容包含主机名、告警级别、指标类型、当前值、时间、全局告警消息、Top 进程。
- **FR-4 钉钉渠道**：支持自定义机器人 webhook URL 与可选加签 secret；按钉钉官方算法计算签名（`HmacSHA256(timestamp + "\n" + secret)` → Base64 → URL encode），发送 markdown 类型消息。
- **FR-5 飞书渠道**：支持自定义机器人 webhook URL 与可选加签 secret；按飞书官方算法计算签名（与钉钉同算法），发送 interactive/post 文本消息。
- **FR-6 通知触发与防骚扰**：仅对已启用指标、状态为 WARNING/CRITICAL 的事件发送；同一指标类型在 `min_interval`（默认 300 秒）内不重复发送；但升级为 CRITICAL 时不受间隔限制立即发送；渠道发送失败只记录日志与历史，不重试、不阻塞其他渠道。
- **FR-7 阈值配置**：WebUI 可配置 8 个指标（cpu_total/cpu_user/cpu_system/cpu_iowait/cpu_steal/mem/memswap/load）的 careful/warning/critical 三级阈值与启用开关；校验 0 ≤ careful < warning < critical；保存后写入插件 `_limits` 立即生效并持久化。
- **FR-8 REST API**：
  - `GET /api/4/notifications`：返回渠道配置（密码/secret 脱敏为 `********`）、阈值配置、最小间隔、最近发送历史（内存中保留最近 10 条）。
  - `POST /api/4/notifications`：提交完整配置，服务端校验、保存、热应用阈值；脱敏字段提交值为 `********` 或缺失时保留原存储值；校验失败返回 400 与错误说明。
  - `POST /api/4/notifications/test`：向已启用（或指定）渠道发送一条测试通知，返回每个渠道的成功/失败结果。
- **FR-9 WebUI 设置弹窗**：告警区域（plugin-alert）标题旁提供"告警设置"按钮打开弹窗；弹窗包含渠道配置表单、阈值表格、最小间隔、保存/测试/关闭按钮；保存与测试结果有明确的界面反馈；弹窗样式与 help 覆盖层风格一致。
- **FR-10 通知发送异步化**：通知发送在后台线程执行（SMTP/HTTP 可能超时），不得阻塞 stats 刷新与 API 响应。

## Non-Functional Requirements

- **NFR-1 零新增硬依赖**：后端仅使用 Python 标准库实现邮件与 HTTP 调用。
- **NFR-2 健壮性**：任一渠道配置错误、网络不可达、SMTP 认证失败等异常均被捕获并记录，Glances 主进程与告警事件机制不受影响；HTTP/SMTP 调用必须有超时（≤10 秒）。
- **NFR-3 安全性**：GET 接口不明文返回 SMTP 密码、机器人 secret；webhook URL 中如含凭据也需脱敏；配置写接口遵循现有认证模型。
- **NFR-4 可测试性**：通知配置、签名算法、冷却逻辑、事件钩子、阈值热更新、API 端点均有单元测试覆盖；外部调用（smtplib/urllib）全部 mock。
- **NFR-5 代码风格**：遵循现有代码风格（许可证头、logger、类型习惯），前端通过 eslint/prettier 与 webpack 构建。

## Constraints

- **Technical**：Python 3 标准库优先；FastAPI 路由模式；Vue 3 Options API + Bootstrap 5 CSS；配置目录通过 `glances.config.user_config_dir()` 获取。
- **Business**：不破坏现有 API 响应格式与 WebUI 既有行为；新增文件遵循 LGPL-3.0 许可证头。
- **Dependencies**：无新增第三方依赖；前端不新增 npm 依赖。

## Assumptions

- 用户在 WebUI 中配置的阈值语义与 glances.conf 一致：CPU/MEM/SWAP 为百分比（0-100），load 为每核负载比值（与现有 `[load]` 默认 0.7/1.0/5.0 一致）。
- 钉钉/飞书机器人由用户自行在群内创建并获取 webhook（及可选 secret），Glances 不做机器人管理。
- 邮件 SMTP 由用户提供（如企业邮箱、QQ/163 等），Glances 不内置邮件服务。
- 通知功能在 webserver 与 standalone 模式均可用（事件钩子为全局）；client 模式本地不产生事件故不会发通知，符合预期。
- 阈值"启用开关"只控制是否发送通知；阈值数值本身保存后即影响插件告警判定（UI 着色/事件产生），与 glances.conf 阈值行为一致。

## Acceptance Criteria

### AC-1: 新告警事件触发通知
- **Type**: `rule`
- **Given**: 钉钉渠道已启用且 webhook 配置有效，`cpu_total` 指标启用通知
- **When**: 模拟一次使 cpu_total 越过 warning 阈值的 stats 更新，产生新的 WARNING 事件
- **Then**: 钉钉发送函数被调用 exactly once，消息内容包含级别 `WARNING`、指标类型 `CPU_TOTAL` 与主机名
- **Pass Condition**: 单元测试中 mock 的 webhook 调用断言通过，且事件监听器被触发
- **Evidence**: `tests/test_notifications.py` 测试通过输出

### AC-2: 防骚扰冷却与 CRITICAL 升级
- **Type**: `rule`
- **Given**: `min_interval=300`，某指标已发送过 WARNING 通知
- **When**: 5 分钟内同类型 WARNING 事件再次产生 → 不发送；随后事件升级为 CRITICAL → 立即发送
- **Then**: 发送函数调用次数与内容符合上述预期（WARNING 第二次被抑制，CRITICAL 升级通知发出且含 `CRITICAL`）
- **Pass Condition**: 冷却逻辑单元测试通过
- **Evidence**: `tests/test_notifications.py` 测试通过输出

### AC-3: 邮件渠道构建与发送
- **Type**: `rule`
- **Given**: 邮件渠道已启用，SMTP 参数与收件人配置完整
- **When**: 触发一次告警通知
- **Then**: 使用标准库构建的 MIME 邮件包含正确发件人/收件人/主题/正文（含主机名、级别、指标、值、时间），并通过 SMTP（SSL 或 STARTTLS）发送
- **Pass Condition**: mock smtplib 的单元测试断言邮件字段与发送调用正确；SMTP 调用带超时参数
- **Evidence**: `tests/test_notifications.py` 测试通过输出

### AC-4: 钉钉/飞书加签算法正确
- **Type**: `rule`
- **Given**: 固定 timestamp 与 secret
- **When**: 生成签名
- **Then**: 签名为 `HmacSHA256(timestamp + "\n" + secret, key=secret)` 经 Base64 再 URL-encode 的结果，与官方文档算法一致
- **Pass Condition**: 用已知输入/期望输出向量断言通过（钉钉与飞书各一）
- **Evidence**: `tests/test_notifications.py` 测试通过输出

### AC-5: 配置持久化与加载
- **Type**: `rule`
- **Given**: 通过 POST API 保存一份完整通知配置
- **When**: 重启 Glances（重新加载配置）
- **Then**: `notifications.json` 存在于用户配置目录且内容为所保存配置；重新加载后渠道启用状态、阈值、收件人等全部恢复
- **Pass Condition**: 临时目录下的 save→load 往返测试断言一致
- **Evidence**: `tests/test_notifications.py` 测试通过输出 + 实际文件检查

### AC-6: 阈值热更新立即生效
- **Type**: `rule`
- **Given**: GlancesStats 已完成插件加载
- **When**: 通过 POST API 保存 `cpu_total` careful=50/warning=70/critical=90
- **Then**: cpu 插件 `_limits` 中 `cpu_total_careful/warning/critical` 立即变为对应值（无需重启），后续 `get_alert` 按新阈值判定
- **Pass Condition**: 应用阈值后读取插件 limits 断言通过
- **Evidence**: `tests/test_notifications.py` / API 测试通过输出

### AC-7: REST API 行为与脱敏
- **Type**: `rule`
- **Given**: 配置了 SMTP 密码与钉钉 secret
- **When**: 调用 `GET /api/4/notifications`
- **Then**: 密码与 secret 字段返回 `********`（非空时），其余配置正常返回；`POST` 提交 `********` 占位时保留原密码；非法阈值（warning<careful）返回 400；`POST /test` 返回各渠道测试结果
- **Pass Condition**: API 测试（TestClient）对脱敏、保留、400 校验、test 响应均断言通过
- **Evidence**: API 测试通过输出

### AC-8: WebUI 告警设置弹窗
- **Type**: `rule`
- **Given**: webserver 模式启动且 WebUI 可访问
- **When**: 用户点击告警区域的"告警设置"按钮
- **Then**: 弹窗展示三个渠道表单、8 个指标阈值表格、最小间隔；填入配置点击保存后接口返回成功并提示；点击测试按钮返回各渠道结果；刷新页面后配置仍在
- **Pass Condition**: webpack 构建成功；浏览器手工验证（或浏览器自动化）弹窗打开/保存/测试流程可用
- **Evidence**: `npm run build` 成功输出 + 浏览器验证截图/记录

### AC-9: 异常隔离不影响主流程
- **Type**: `rule`
- **Given**: 某渠道 webhook 不可达或 SMTP 认证失败
- **When**: 触发告警通知
- **Then**: 异常被捕获、记录日志与发送历史（ok=false），stats 更新循环与其他渠道发送不受影响，Glances 进程不崩溃
- **Pass Condition**: mock 抛异常的单元测试中通知器仍正常返回、事件流程不中断
- **Evidence**: `tests/test_notifications.py` 测试通过输出

### AC-10: 通知不阻塞主循环
- **Type**: `rule`
- **Given**: 渠道发送设置为慢响应（mock sleep）
- **When**: 触发通知
- **Then**: 事件处理线程不被阻塞（发送在后台线程执行，通知接口立即返回）
- **Pass Condition**: 单元测试断言通知触发后主线程在 1 秒内继续执行
- **Evidence**: `tests/test_notifications.py` 测试通过输出

### AC-11: WebUI 配置体验质量
- **Type**: `rubric`
- **Dimension**: 弹窗的可用性与视觉一致性
- **Scale**: 1-5
- **Anchors**: 1 = 弹窗无法使用或样式与 Glances 整体风格严重冲突；3 = 功能完整但布局/反馈粗糙、有明显样式不一致；5 = 布局清晰、字段分组合理、保存/测试反馈明确、与 help 覆盖层及现有卡片风格一致
- **Pass Threshold**: >= 4
- **Evidence**: 浏览器验证记录 + 构建产物

### AC-12: 实现简洁性与代码风格契合度
- **Type**: `rubric`
- **Dimension**: 实现是否遵循 Glances 现有架构与最小改动原则
- **Scale**: 1-5
- **Anchors**: 1 = 大改现有模块、引入多余抽象/依赖；3 = 功能可用但有不必要的重构或风格脱节；5 = 新模块自包含、对 events_list/stats/restful_api 的改动均为最小必要挂钩、许可证头与 logger 用法一致、无新依赖
- **Pass Threshold**: >= 4
- **Evidence**: 独立评审代码审查

## Open Questions

- 无（关键设计点已与用户确认：核心事件指标范围、WARNING+CRITICAL 触发、独立 JSON 持久化、设置弹窗形态）。
