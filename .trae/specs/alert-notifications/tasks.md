# Glances 告警通知（邮件 / 钉钉 / 飞书）- 实施计划

任务依赖顺序：T1（配置）→ T2（事件钩子）→ T3（发送器）→ T4（阈值应用）→ T5（REST API）→ T6（WebUI）→ T7（构建/文档）→ T8（独立评审）。

## Task 1: 后端通知配置模块（JSON 持久化 / 校验 / 脱敏）
- **Status**: `completed`
- **Completion Evidence**:
  - 新建 `glances/notifications/config.py`（GlancesNotificationConfig：默认配置、JSON 原子持久化、update/temporary_update/check_update、validate、as_dict_secure 脱敏、THRESHOLD_METRICS 元数据）。
  - TR-1.1/1.2/1.3/1.4 均由 `tests/test_notifications.py` 覆盖并通过（30 个测试全绿），包括 save→load 往返、缺文件默认值、不可写目录返回 False、非法阈值校验、脱敏与 `********` 占位保留。
  - `uv run ruff check/format` 通过。
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 新建 `glances/notifications/` 包（`__init__.py`、`config.py`），实现 `GlancesNotificationConfig`：
    - 默认配置结构：`version`、`min_interval`（默认 300 秒）、`channels`（email/dingtalk/feishu 三组，含 enabled 与各自字段）、`thresholds`（8 个指标 cpu_total/cpu_user/cpu_system/cpu_iowait/cpu_steal/mem/memswap/load，每个含 enabled + careful/warning/critical，默认值与 glances.conf 现有默认一致：CPU/内存/Swap 50/70/90，load 0.7/1.0/5.0）。
    - 配置文件路径：`user_config_dir()` 首个可写目录下的 `notifications.json`；`load()` 文件不存在时用默认值；`save()` 时 `os.makedirs(exist_ok=True)` 并原子写入（临时文件 rename）；不可写时捕获异常、logger.warning 且不抛出。
    - `update(data)`：合并提交数据；脱敏占位值（`********`）或缺失的敏感字段（smtp_password、dingtalk.secret、feishu.secret）保留原值；收件人支持逗号分隔字符串或列表。
    - `validate()`：阈值必须为数值且 0 ≤ careful < warning < critical；email 启用时 smtp_host/sender/recipients 非空；dingtalk/feishu 启用时 webhook 非空且为 http(s) URL；min_interval 为非负整数；返回错误列表。
    - `as_dict_secure()`：敏感字段非空时返回 `********`，webhook URL 中内嵌凭据按 `secure_option` 方式脱敏。
    - 指标元数据 `THRESHOLD_METRICS`：key → (plugin, limit_prefix, 单位/标签)，供阈值应用与 API/UI 使用。
- **Acceptance Criteria Addressed**: AC-5, AC-7
- **Test Requirements**:
  - `rule` TR-1.1: 临时目录中 save → 重新 load，配置往返一致（含 channels/thresholds/min_interval）。
  - `rule` TR-1.2: 文件不存在时 load 返回默认值且不抛异常；目录不可写时 save 不抛异常仅记日志。
  - `rule` TR-1.3: validate 拒绝 careful≥warning、非数值阈值、启用渠道缺字段，返回明确错误；合法配置通过。
  - `rule` TR-1.4: as_dict_secure 对非空密码/secret 返回 `********`；update 收到 `********` 时保留原密码。
- **Notes**: 仅用标准库（json/os/logging）；许可证头与现有模块一致。

## Task 2: 事件监听器钩子（events_list）
- **Status**: `completed`
- **Completion Evidence**:
  - `glances/events_list.py` 增加 `add_listener()`、`_fire_listeners()`（try/except 隔离）；`_create_event` insert 后触发、`_update_event` 检测 WARNING→CRITICAL 升级触发；补充 logger 导入。
  - TR-2.1/2.2/2.3 由 `test_event_listener_new_event_and_escalation`、`test_event_listener_exception_isolation` 覆盖通过。
  - git diff 确认 events_list.py 为纯新增 33 行、无逻辑改动。
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 在 [events_list.py](file:///Users/kkcarrot/swe-project/glances_fork/glances/events_list.py) 的 `GlancesEventsList` 中增加：
    - `__init__` 中初始化 `self._listeners = []`；
    - `add_listener(callback)` 方法；
    - `_fire_listeners(event_dict)`：逐个回调，整体 try/except 包裹并 logger.error，监听器异常绝不影响事件主流程；
    - `_create_event` 中事件实际 insert 后触发监听（仅 WARNING/CRITICAL，与现有创建条件一致）；
    - `_update_event` 中检测 WARNING→CRITICAL 升级（更新前 state 非 CRITICAL 且新 state 为 CRITICAL），更新后触发监听。
- **Acceptance Criteria Addressed**: AC-1, AC-2, AC-9
- **Test Requirements**:
  - `rule` TR-2.1: 注册监听器后，add() 一个新 WARNING 事件，监听器收到事件字典且 type/state 正确。
  - `rule` TR-2.2: 同类型持续 WARNING 更新不重复触发（创建只触发一次）；升级 CRITICAL 时再触发一次。
  - `rule` TR-2.3: 监听器回调抛异常时，events.add() 正常返回、其他监听器仍被调用。
- **Notes**: 改动保持最小，不改变现有事件创建/关闭/清理逻辑。

## Task 3: 通知发送器（邮件 / 钉钉 / 飞书 + 冷却 + 异步 + 历史）
- **Status**: `completed`
- **Completion Evidence**:
  - 新建 `glances/notifications/notifier.py`：GlancesNotifier 单例 + 事件注册；冷却（升级 CRITICAL 例外）；daemon 线程异步 fan-out；邮件（SMTP_SSL/STARTTLS，timeout=10）、钉钉 markdown webhook、飞书 text webhook（共用 HmacSHA256 加签）；内存历史最近 10 条；send_test。
  - TR-3.1–3.7 由 `tests/test_notifications.py` 覆盖：冷却/升级序列断言 `['WARNING','CRITICAL']`、签名独立向量比对、SMTP SSL/STARTTLS 调用断言、webhook payload/签名/timeout、异常隔离、<1s 非阻塞（30 测试全绿）。
- **Priority**: high
- **Depends On**: T1, T2
- **Description**:
  - 新建 `glances/notifications/notifier.py`，实现 `GlancesNotifier` 单例 `glances_notifier`：
    - `__init__`：加载 `GlancesNotificationConfig`，向 `glances_events.add_listener` 注册回调；维护 per-type 最后发送时间戳与内存发送历史（最近 10 条：时间/渠道/事件类型/级别/ok/error）。
    - 事件回调 `_on_event(event)`：指标未启用 → 跳过；冷却判断（同类型 min_interval 内抑制，WARNING→CRITICAL 升级例外）；通过后在后台 daemon 线程执行 fan-out（FR-10）。
    - 消息构建：标题 `[Glances告警][STATE] TYPE on <hostname>`；正文含主机名（socket.gethostname）、级别、指标、当前值（max/avg）、发生时间、global_msg、top 进程。
    - 邮件 `_send_email`：`smtplib` + `email.message.EmailMessage`；ssl 端口 465 用 SMTP_SSL，否则 SMTP+starttls（587）；`timeout=10`；支持密码认证。
    - 钉钉 `_send_dingtalk`：webhook 附带 `timestamp` 与 `sign`（secret 非空时），POST JSON `{"msgtype":"markdown","markdown":{"title":...,"text":...}}`；urllib.request，timeout=10。
    - 飞书 `_send_feishu`：webhook query 带 `timestamp` 与 `sign`（secret 非空时），POST JSON `{"msg_type":"text","content":{"text":...}}`；urllib.request，timeout=10。
    - 签名 `_sign(timestamp, secret)`：`base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256).digest())`（钉钉/飞书同算法）。
    - 每个渠道 try/except 独立捕获，失败写历史（ok=false+error）并 logger.error；`send_test(channels=None)` 发送固定测试消息并返回各渠道结果 dict。
- **Acceptance Criteria Addressed**: AC-1, AC-2, AC-3, AC-4, AC-9, AC-10
- **Test Requirements**:
  - `rule` TR-3.1: mock 钉钉发送函数，新 WARNING 事件触发 exactly once 且消息含 WARNING/类型/主机名（AC-1）。
  - `rule` TR-3.2: min_interval 内重复 WARNING 不发送；升级 CRITICAL 立即发送（AC-2）。
  - `rule` TR-3.3: mock smtplib，断言邮件 From/To/Subject/正文内容与 SMTP_SSL/starttls 调用路径正确、timeout 传入（AC-3）。
  - `rule` TR-3.4: 固定 timestamp/secret 的签名向量与按官方算法独立计算的期望串一致（钉钉、飞书各一）（AC-4）。
  - `rule` TR-3.5: mock 发送函数抛异常，notifier 不抛出、历史记录 ok=false、其他渠道仍被调用（AC-9）。
  - `rule` TR-3.6: mock 慢发送（sleep），事件回调在 1 秒内返回（后台线程）（AC-10）。
  - `rule` TR-3.7: send_test 对启用渠道返回 {channel: {ok: true/false, error?}}。
- **Notes**: 仅用标准库（smtplib/email/urllib/hmac/hashlib/base64/json/threading/socket/time）。

## Task 4: 阈值运行时应用（stats 启动钩子 + 热更新）
- **Status**: `completed`
- **Completion Evidence**:
  - `notifier.apply_thresholds(stats)` 按 THRESHOLD_METRICS 映射写入插件 limits；`glances/stats.py` 在 load_modules 后调用（defensive try/except）。
  - 关键设计：仅用户显式定制的阈值（threshold_overrides：文件加载或 API 提交）才覆盖插件，默认配置不改动 glances.conf 行为——修复了 test_core.py::test_023_get_alert 回归。
  - TR-4.1/4.2/4.3 由 `test_apply_thresholds_to_plugins`、`test_apply_thresholds_on_real_stats`（真实 GlancesStats）、`test_apply_thresholds_missing_plugin_never_raises`、`test_default_config_does_not_override_plugin_limits` 覆盖通过。
- **Priority**: high
- **Depends On**: T1
- **Description**:
  - 在 `glances/notifications/config.py`（或 notifier 模块）实现 `apply_thresholds(stats)`：遍历配置中 thresholds，按 THRESHOLD_METRICS 映射取 `stats.get_plugin(plugin)`，调用 `plugin.set_limits(limit_prefix + level, float(value))`（prefix 为空时直接 level），写入 careful/warning/critical 三个键；插件不存在时跳过并 logger.debug。
  - 在 [stats.py](file:///Users/kkcarrot/swe-project/glances_fork/glances/stats.py) 的 `GlancesStats.__init__`（load_modules 之后）调用 `glances_notifier.apply_thresholds(self)`（import 放在方法内或模块顶部，注意避免循环导入；notifier 不得 import stats）。
  - 校验 set_limits 写入的键与 get_limit 查找键一致（如 `cpu_total_critical`、`mem_warning`、`load_critical`）。
- **Acceptance Criteria Addressed**: AC-6
- **Test Requirements**:
  - `rule` TR-4.1: 构造带 cpu/mem/memswap/load 插件（可用真实插件实例或轻量 fake）的 stats，apply_thresholds 后各插件 `_limits` 中对应键值等于配置值。
  - `rule` TR-4.2: 应用后插件 `get_limit('critical', stat_name='cpu_total')` 返回新值；`get_alert` 按新阈值判定（可用真实 cpu 插件或 fake 验证）。
  - `rule` TR-4.3: 配置中不存在的插件名不导致异常。

## Task 5: REST API 端点（GET/POST notifications + test）
- **Status**: `completed`
- **Completion Evidence**:
  - `glances/outputs/glances_restful_api.py` 新增 `_api_notifications_get/set/test`；GET/POST 路由注册在 `{plugin}` 通配路由之前（修复路由顺序冲突）；POST 先 check_update 非破坏校验再应用持久化；test 端点用 temporary_update 试用配置不污染运行态。
  - TR-5.1–5.4 由 `test_api_notifications_get/post_applies_and_persists/post_invalid_returns_400/post_bad_json_returns_400/test` 覆盖通过。
  - 真实服务器 curl 端到端验证：GET 200、非法 POST 400 中文错误、合法 POST 200 且 `/api/4/cpu/limits` 立即变为新阈值、notifications.json 落盘、test 端点失败渠道返回 ok=false 不 500。
- **Priority**: high
- **Depends On**: T3, T4
- **Description**:
  - 在 [glances_restful_api.py](file:///Users/kkcarrot/swe-project/glances_fork/glances/outputs/glances_restful_api.py) 的 `_router()` 中注册：
    - GET `{base_path}/notifications` → `_api_notifications_get`：返回 `as_dict_secure()` + threshold 元数据 + 发送历史。
    - POST `{base_path}/notifications` → `_api_notifications_set`：读取 JSON body，`config.update(data)` → `validate()`，失败返回 400 + 错误列表；通过后 `save()`、`glances_notifier.apply_thresholds(self.stats)`、刷新 notifier 配置引用，返回脱敏后配置。
    - POST `{base_path}/notifications/test` → `_api_notifications_test`：可选 body 指定渠道；调用 `send_test()`，返回各渠道结果。
  - 端点遵循现有 GlancesJSONResponse/HTTPException 模式；认证沿用 router 级 Depends。
- **Acceptance Criteria Addressed**: AC-6, AC-7, AC-8
- **Test Requirements**:
  - `rule` TR-5.1: TestClient GET 返回 200，敏感字段为 `********`，结构含 channels/thresholds/history。
  - `rule` TR-5.2: POST 合法配置返回 200 且配置文件被写入；POST 非法阈值返回 400 且含错误说明。
  - `rule` TR-5.3: POST 后内存插件 limits 立即变化（热更新，AC-6 端到端）。
  - `rule` TR-5.4: POST /test 返回每渠道 ok/error 字段（mock 发送）。
- **Notes**: 参考现有 test_api.py 的 TestClient 用法；测试中临时配置目录用 monkeypatch/tmp_path。

## Task 6: WebUI 告警设置弹窗组件 + 入口
- **Status**: `completed`
- **Completion Evidence**:
  - 新建 `glances/outputs/static/js/components/settings-alerts.vue`（覆盖层弹窗：三渠道表单、8 指标阈值表、间隔、保存/测试/关闭 + 结果反馈）；plugin-alert.vue 标题区新增"告警设置"按钮并挂载组件；style.scss 新增深色主题弹窗样式。
  - TR-6.1/6.2 由浏览器自动化 7 步验证全部通过：弹窗结构完整、测试失败反馈不崩溃、保存成功提示、关闭、重开回显 75 等（离屏环境无截图，以无障碍树+DOM 文本核验）。
  - eslint 0 error（已 --fix 并声明 emits、移除未用 prop）。
- **Priority**: high
- **Depends On**: T5
- **Description**:
  - 新建 `glances/outputs/static/js/components/settings-alerts.vue`：
    - 全屏覆盖层弹窗（参考 help.vue 结构与样式，Bootstrap 5 卡片/表单类，无 bootstrap JS 依赖）。
    - 渠道区：邮件（启用开关、smtp_host、smtp_port、ssl/tls 勾选、smtp_user、smtp_password、sender、recipients 逗号分隔）、钉钉（启用、webhook、secret）、飞书（启用、webhook、secret）。
    - 阈值区：8 行指标表格（中文标签 + 单位：CPU 类/内存/Swap 为 %，load 为 每核负载），每行启用勾选 + careful/warning/critical 数字输入。
    - 通用：最小通知间隔（秒）。
    - 按钮：保存配置（POST）、发送测试通知（POST /test）、关闭；操作结果（成功/失败/各渠道测试结果）在弹窗内反馈。
    - 打开时 GET /api/4/notifications 拉取配置；密码/secret 字段显示 `********` 占位，未修改则原样回传。
  - 在 [plugin-alert.vue](file:///Users/kkcarrot/swe-project/glances_fork/glances/outputs/static/js/components/plugin-alert.vue) 标题区"Clear alerts"旁增加"告警设置"按钮，点击切换弹窗；引入并挂载 settings-alerts 组件。
- **Acceptance Criteria Addressed**: AC-8, AC-11
- **Test Requirements**:
  - `rule` TR-6.1: 弹窗包含全部渠道字段、8 个指标阈值行、间隔字段与三个按钮（代码审查 + 浏览器验证）。
  - `rule` TR-6.2: 浏览器中打开弹窗→修改阈值/启用钉钉（测试 webhook 可用 mock 或仅验证接口交互）→保存提示成功→刷新后配置回显。
  - `rubric` TR-6.3: 可用性与视觉一致性；scale 1-5；anchors 1=不可用/风格冲突，3=功能完整但粗糙，5=分组清晰、反馈明确、与 help 覆盖层风格一致；threshold >= 4；evidence 为浏览器验证记录。
- **Notes**: 不新增 npm 依赖；遵循现有 tab 缩进与 prettier 规则。

## Task 7: 后端测试收尾、前端构建与文档
- **Status**: `completed`
- **Completion Evidence**:
  - `tests/test_notifications.py` 共 30 个测试全部通过；`test_core/test_api/test_api_secure/test_webui_template_response/test_plugin_cpu/mem/load/quicklook/test_main` 等回归 209+26 测试通过，无回归。
  - `npm run build` 成功（glances.js 686 KiB / browser.js 560 KiB），产物 public/glances.js 含弹窗代码（"告警通知设置"、"notifications/test" 验证）。
  - 文档：docs/config.rst 新增 "Alert notifications" 章节；NEWS.rst 新增 4.6.0 条目。
- **Priority**: medium
- **Depends On**: T6
- **Description**:
  - 汇总后端测试到 `tests/test_notifications.py`（T1–T5 的 TR 全部落地），运行 `python -m pytest tests/test_notifications.py -v` 全绿；运行既有相关测试（test_api.py、test_glances_stats.py）确认无回归。
  - 前端：在 `glances/outputs/static` 下执行 `npm run lint`（如可用）与 `npm run build`，生成构建产物（glances.js 等 public 资源）。
  - 文档：在 [docs/config.rst](file:///Users/kkcarrot/swe-project/glances_fork/docs/config.rst) 增加"告警通知（邮件/钉钉/飞书）"小节，说明 WebUI 配置入口、notifications.json 位置与字段；按 NEWS.rst 格式添加 changelog 条目。
- **Acceptance Criteria Addressed**: AC-5, AC-8
- **Test Requirements**:
  - `rule` TR-7.1: `pytest tests/test_notifications.py` 全部通过；`pytest tests/test_api.py tests/test_glances_stats.py` 无回归（或与主干一致的既有失败）。
  - `rule` TR-7.2: `npm run build` 成功退出且产物更新；lint 无新增错误。
  - `rule` TR-7.3: docs/config.rst 与 NEWS.rst 包含新功能说明。

## Review R1 问题修复

### Issue I-1: webhook 凭据脱敏（F1，Review R1）
- **Status**: `completed`
- **Discovered By**: Review R1（severity: medium, actionable）
- **Completion Evidence**:
  - `_SENSITIVE_FIELDS` 增加 `webhook`（[config.py](file:///Users/kkcarrot/swe-project/glances_fork/glances/notifications/config.py)）：as_dict_secure 非空返回 `********`；update 收到 `********`/空值保留原 URL；新增 `test_config_update_preserves_redacted_webhook`、GET 脱敏断言。
  - 真实服务器验证：配置钉钉 webhook 后 GET 返回 `webhook: ******** / secret: ********`，磁盘文件仍为真值。

### Issue I-2: 阈值回填运行态值（F2，Review R1）
- **Status**: `completed`
- **Discovered By**: Review R1（severity: medium, actionable）
- **Completion Evidence**:
  - 新增 `GlancesNotifier.effective_thresholds(stats)`：careful/warning/critical 取插件 `get_limit()` 运行态值，enabled 取通知配置；无插件限制时回退配置默认值。
  - GET `/api/4/notifications` 返回 effective thresholds（不再返回硬编码默认值）；新增 `test_effective_thresholds_reflect_plugin_limits`、`test_effective_thresholds_fallback_to_config_defaults`、`test_api_notifications_get_returns_effective_thresholds`。
  - 真实服务器验证（conf/glances.conf 中 cpu_total=65/75/85）：GET 返回 65/75/85；仅保存渠道配置后 `/api/4/cpu/limits` 的 cpu_total_critical 仍为 85（未被默认值覆盖）。

### Issue I-3: 测试监听器清理（F4，Review R1 advisory，顺手修复）
- **Status**: `completed`
- **Completion Evidence**: events_list 新增 `remove_listener`；notifier 新增 `unregister`；测试 autouse fixture 在每个用例后清理本用例注册的监听器，避免跨套件外发请求。签名测试改用硬编码期望向量。

## Task 8: 独立评审
- **Status**: `completed`
- **Completion Evidence**:
  - Review R1（fail）→ 修复 F1（webhook 脱敏）、F2（运行态阈值回填）、F4（监听器清理+硬编码向量），见 Issue I-1/I-2/I-3。
  - Review R2（pass）：全新上下文独立评审，35 个通知单测全绿、276 回归通过、ruff/eslint 干净、TestClient 端到端冒烟；CP-1~CP-10 rule 全 pass，CP-11/CP-12 rubric 均 4/5。
  - R2 advisory 中 A1（webhook strip）、A2（/test asyncio.to_thread）、A3（SMTP 证书校验）已顺手修复并补测；A4 接受现状。
  - 结论：全部 12 条 AC 达标，review.md 记录在案。
- **Priority**: high
- **Depends On**: T7
- **Description**:
  - 队列清空后进入 Review：创建 review.md，由全新上下文的独立评审者按 AC 逐项核验（测试结果、API 行为、前端构建、代码风格契合度 AC-12），输出 pass/fail/blocked；fail 则将发现物化为 Issue 回到 Implement。
- **Acceptance Criteria Addressed**: 全部 AC
- **Test Requirements**:
  - `rule` TR-8.1: 每个 AC 在 review.md 中有独立证据与结论。
  - `rubric` TR-8.2: 实现简洁性与架构契合度（AC-12）；scale 1-5；anchors 见 spec；threshold >= 4；evidence 为评审代码审查记录。
