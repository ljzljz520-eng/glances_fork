# Glances 告警通知（邮件 / 钉钉 / 飞书）- Independent Review

- [x] CP-1: 新告警事件触发通知（事件钩子→通知器→渠道调用，消息含级别/类型/主机名）
  - **Type**: `rule`
  - **Covers**: AC-1, TR-2.1, TR-3.1
  - **Evidence**: R2 评审 pass。events_list `_create_event` insert 后 `_fire_listeners`（events_list.py L343）；notifier `_on_event`→daemon 线程 `_fan_out`；35 个单测全绿（含事件→渠道集成断言），真实服务器 curl 验证。

- [x] CP-2: 冷却抑制与 CRITICAL 升级例外
  - **Type**: `rule`
  - **Covers**: AC-2, TR-3.2
  - **Evidence**: R2 pass。notifier.py L92-L101 冷却逻辑（`state=='CRITICAL' and last[1]!='CRITICAL'` 绕过）；测试断言发送序列 `['WARNING','CRITICAL']`。

- [x] CP-3: 邮件渠道构建与发送（MIME 字段、SSL/STARTTLS、超时）
  - **Type**: `rule`
  - **Covers**: AC-3, TR-3.3
  - **Evidence**: R2 pass。_send_email 使用 SMTP_SSL(465)/SMTP+starttls(587)，timeout=10，R2 后加固为 `ssl.create_default_context()` 校验证书（A3 修复）；测试断言 From/To/Subject/正文/starttls/timeout/context。

- [x] CP-4: 钉钉/飞书加签算法正确（硬编码向量）
  - **Type**: `rule`
  - **Covers**: AC-4, TR-3.4
  - **Evidence**: R2 pass。sign() = HmacSHA256(ts+"\n"+secret, key=secret)→Base64→quote_plus；测试使用硬编码期望向量（钉钉毫秒/飞书秒）+ 独立复算交叉校验。

- [x] CP-5: 配置持久化与加载（save→load 往返、缺文件默认、不可写降级）
  - **Type**: `rule`
  - **Covers**: AC-5, TR-1.1, TR-1.2
  - **Evidence**: R2 pass。原子写 mkstemp+os.replace；不可写返回 False 不抛异常；往返测试 + 真实服务器落盘验证。

- [x] CP-6: 阈值热更新立即生效且默认配置不覆盖 glances.conf
  - **Type**: `rule`
  - **Covers**: AC-6, TR-4.1–4.3
  - **Evidence**: R2 pass。apply_thresholds 仅应用 threshold_overrides；stats.py 启动钩子 + POST 热应用；R2 用真实 GlancesStats 验证键名 cpu_total_critical/mem_warning/memswap_critical/load_critical；真实服务器验证仅存渠道配置时 conf 阈值（65/75/85）不被覆盖。

- [x] CP-7: REST API（GET 脱敏含 webhook、POST 校验 400、占位保留、test 结果）
  - **Type**: `rule`
  - **Covers**: AC-7, TR-5.1–5.4
  - **Evidence**: R2 pass。F1 修复后 webhook/secret/smtp_password 均脱敏；check_update 非破坏校验；非法 POST 400 中文错误；/test 经 asyncio.to_thread 在线程池执行（A2 修复，不阻塞事件循环）；R2 用 TestClient+真实 GlancesStats 端到端实测。

- [x] CP-8: WebUI 告警设置弹窗（渠道表单/8 指标阈值表/间隔/保存/测试/回显）
  - **Type**: `rule`
  - **Covers**: AC-8, TR-6.1, TR-6.2
  - **Evidence**: R2 pass。浏览器自动化 7 步验证（弹窗结构、测试失败反馈不崩溃、保存成功提示、关闭、重开回显）；npm build 成功，产物含弹窗代码；eslint 0 error。

- [x] CP-9: 渠道异常隔离，不影响主流程与其他渠道
  - **Type**: `rule`
  - **Covers**: AC-9, TR-3.5
  - **Evidence**: R2 pass。监听器逐回调 try/except；渠道独立 try/except + history(ok=false)；测试覆盖 dingtalk 失败而 feishu 成功。

- [x] CP-10: 通知发送异步化，不阻塞 stats/API
  - **Type**: `rule`
  - **Covers**: AC-10, TR-3.6
  - **Evidence**: R2 pass。事件路径 daemon 线程发送，<1s 返回测试通过；/test 端点 R2 后改为 asyncio.to_thread。

- [x] CP-11: WebUI 配置体验质量
  - **Type**: `rubric`
  - **Covers**: AC-11, TR-6.3
  - **Scale**: 1-5
  - **Anchors**: 1 = 弹窗不可用或风格严重冲突；3 = 功能完整但布局/反馈粗糙；5 = 分组清晰、反馈明确、与 help 覆盖层风格一致
  - **Pass Threshold**: >= 4
  - **Evidence**: R2 评分 **4/5**（≥4 达标）。深色覆盖层+卡片风格统一、分组清晰、反馈明确；扣分于 webhook 为 text 输入框、无前端预校验（依赖后端中文 400）。

- [x] CP-12: 实现简洁性与架构契合度（零新依赖、最小挂钩、风格一致）
  - **Type**: `rubric`
  - **Covers**: AC-12, TR-8.2
  - **Scale**: 1-5
  - **Anchors**: 1 = 大改现有模块/多余抽象；3 = 可用但有不必要重构；5 = 模块自包含、改动最小、无新依赖、风格一致
  - **Pass Threshold**: >= 4
  - **Evidence**: R2 评分 **4/5**（≥4 达标）。glances/notifications/ 包自包含、纯标准库零新依赖；events_list 纯新增挂钩、stats ~10 行防御性钩子、API 沿用 GlancesJSONResponse/HTTPException；ruff check/format 干净。

## Review History

### Review R1
- **Result**: `fail`
- **Evidence**: 30 测试通过、220 回归通过、ruff 通过；但发现 2 个 actionable：
  - F1（medium）：钉钉/飞书 webhook 为 capability URL（token 在 query/path），secure_option 仅脱敏 userinfo，GET 明文返回，违反 NFR-3。
  - F2（medium）：GET 返回硬编码默认阈值而非运行态值，前端保存全量 8 指标会静默覆盖 glances.conf 阈值。
  - advisory：F3 空值无法清空密码；F4 测试监听器只增不减；F5 /test 同步串行、签名测试同义反复、quit 掩盖异常等。
- **Remediation**: F1→Issue I-1（webhook 纳入 _SENSITIVE_FIELDS 脱敏与占位保留）；F2→Issue I-2（effective_thresholds 从插件 get_limit 回填运行态值）；F4 等→Issue I-3（remove_listener/unregister/测试 fixture 清理、硬编码签名向量）。

### Review R2
- **Result**: `pass`
- **Evidence**: 全新上下文评审者通读全部文件、实跑 35 个单测（全绿）、回归 276 测试通过、ruff/eslint 干净、TestClient+真实 GlancesStats 端到端冒烟；R1 的 F1/F2/F4 修复全部独立验证有效。CP-1~CP-10 rule 全 pass，CP-11/CP-12 rubric 均 4/5（≥4）。
- **Advisory（不阻塞，部分已顺手修复）**：
  - A1 webhook 粘贴尾随空格可致错误信息泄漏 token → **已修复**（入库前 strip + 测试）。
  - A2 /test 同步阻塞事件循环（最坏 ~30s）→ **已修复**（asyncio.to_thread 线程池）。
  - A3 SMTP 不校验证书（MITM 面）→ **已修复**（ssl.create_default_context()）。
  - A4 空串无法清空已存凭据；POST 保存失败时内存态轻微不一致 → 接受现状（可用停用渠道替代，属边界情况）。
