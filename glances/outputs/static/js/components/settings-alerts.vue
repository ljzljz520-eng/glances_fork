<template>
	<div class="settings-alerts-overlay" @click.self="$emit('close')">
		<div class="card settings-alerts-modal">
			<div class="card-header d-flex justify-content-between align-items-center">
				<span class="title">告警通知设置</span>
				<button class="button" @click="$emit('close')">关闭</button>
			</div>
			<div class="card-body">
				<!-- Notification channels -->
				<h6 class="title">通知渠道</h6>

				<!-- Email -->
				<div class="border rounded p-2 mb-2">
					<div class="form-check">
						<input id="notify-email-enable" v-model="form.channels.email.enabled" class="form-check-input" type="checkbox">
						<label class="form-check-label title" for="notify-email-enable">邮件 (SMTP)</label>
					</div>
					<div class="row g-2 mt-0">
						<div class="col-4">
							<label class="small">SMTP 服务器</label>
							<input v-model="form.channels.email.smtp_host" type="text" class="form-control form-control-sm" placeholder="smtp.example.com">
						</div>
						<div class="col-2">
							<label class="small">端口</label>
							<input v-model.number="form.channels.email.smtp_port" type="number" class="form-control form-control-sm">
						</div>
						<div class="col-3 d-flex align-items-end pb-1">
							<div class="form-check me-3">
								<input id="notify-email-ssl" v-model="form.channels.email.smtp_ssl" class="form-check-input" type="checkbox">
								<label class="form-check-label small" for="notify-email-ssl">SSL (465)</label>
							</div>
							<div class="form-check">
								<input id="notify-email-tls" v-model="form.channels.email.smtp_tls" class="form-check-input" type="checkbox">
								<label class="form-check-label small" for="notify-email-tls">STARTTLS (587)</label>
							</div>
						</div>
						<div class="col-3">
							<label class="small">发件人</label>
							<input v-model="form.channels.email.sender" type="text" class="form-control form-control-sm" placeholder="glances@example.com">
						</div>
						<div class="col-4">
							<label class="small">用户名</label>
							<input v-model="form.channels.email.smtp_user" type="text" class="form-control form-control-sm" autocomplete="off">
						</div>
						<div class="col-3">
							<label class="small">密码 / 授权码</label>
							<input v-model="form.channels.email.smtp_password" type="password" class="form-control form-control-sm" autocomplete="new-password" placeholder="********">
						</div>
						<div class="col-5">
							<label class="small">收件人（多个用逗号分隔）</label>
							<input v-model="emailRecipients" type="text" class="form-control form-control-sm" placeholder="a@example.com, b@example.com">
						</div>
					</div>
				</div>

				<!-- DingTalk -->
				<div class="border rounded p-2 mb-2">
					<div class="form-check">
						<input id="notify-dingtalk-enable" v-model="form.channels.dingtalk.enabled" class="form-check-input" type="checkbox">
						<label class="form-check-label title" for="notify-dingtalk-enable">钉钉群机器人</label>
					</div>
					<div class="row g-2 mt-0">
						<div class="col-8">
							<label class="small">Webhook 地址</label>
							<input v-model="form.channels.dingtalk.webhook" type="text" class="form-control form-control-sm" placeholder="https://oapi.dingtalk.com/robot/send?access_token=...">
						</div>
						<div class="col-4">
							<label class="small">加签密钥（可选）</label>
							<input v-model="form.channels.dingtalk.secret" type="password" class="form-control form-control-sm" autocomplete="new-password" placeholder="SEC...">
						</div>
					</div>
				</div>

				<!-- Feishu -->
				<div class="border rounded p-2 mb-2">
					<div class="form-check">
						<input id="notify-feishu-enable" v-model="form.channels.feishu.enabled" class="form-check-input" type="checkbox">
						<label class="form-check-label title" for="notify-feishu-enable">飞书群机器人</label>
					</div>
					<div class="row g-2 mt-0">
						<div class="col-8">
							<label class="small">Webhook 地址</label>
							<input v-model="form.channels.feishu.webhook" type="text" class="form-control form-control-sm" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/...">
						</div>
						<div class="col-4">
							<label class="small">加签密钥（可选）</label>
							<input v-model="form.channels.feishu.secret" type="password" class="form-control form-control-sm" autocomplete="new-password" placeholder="SEC...">
						</div>
					</div>
				</div>

				<!-- Thresholds -->
				<h6 class="title mt-3">告警阈值</h6>
				<table class="table table-sm table-bordered align-middle text-center">
					<thead>
						<tr>
							<th class="text-start">指标</th>
							<th>启用通知</th>
							<th>Careful</th>
							<th>Warning</th>
							<th>Critical</th>
						</tr>
					</thead>
					<tbody>
						<tr v-for="metric in metrics" :key="metric.key">
							<td class="text-start">
								{{ metric.label }}
								<span class="text-muted">({{ metric.unit || '比值' }})</span>
							</td>
							<td>
								<input v-model="form.thresholds[metric.key].enabled" type="checkbox" class="form-check-input">
							</td>
							<td><input v-model.number="form.thresholds[metric.key].careful" type="number" step="any" class="form-control form-control-sm text-end"></td>
							<td><input v-model.number="form.thresholds[metric.key].warning" type="number" step="any" class="form-control form-control-sm text-end"></td>
							<td><input v-model.number="form.thresholds[metric.key].critical" type="number" step="any" class="form-control form-control-sm text-end"></td>
						</tr>
					</tbody>
				</table>

				<!-- General settings -->
				<h6 class="title mt-3">通知设置</h6>
				<div class="row g-2">
					<div class="col-4">
						<label class="small">同类告警最小通知间隔（秒）</label>
						<input v-model.number="form.min_interval" type="number" min="0" class="form-control form-control-sm">
					</div>
				</div>

				<!-- Feedback -->
				<div v-if="message" class="mt-2" :class="message.type === 'error' ? 'warning' : 'ok'">
					{{ message.text }}
				</div>
				<div v-for="(result, channel) in testResults" :key="channel" class="mt-1">
					{{ channelLabels[channel] || channel }}:
					<span :class="result.ok ? 'ok' : 'warning'">
						{{ result.ok ? '发送成功' : '发送失败: ' + result.error }}
					</span>
				</div>
				<div v-if="testMessage" class="text-muted mt-1">{{ testMessage }}</div>
			</div>
			<div class="card-footer">
				<button class="button me-2" :disabled="busy" @click="test">发送测试通知</button>
				<button class="button me-2" :disabled="busy" @click="save">保存配置</button>
				<button class="button" @click="$emit('close')">关闭</button>
			</div>
		</div>
	</div>
</template>

<script>
export default {
	name: "SettingsAlerts",
	emits: ["close"],
	data() {
		return {
			busy: false,
			metrics: [],
			emailRecipients: "",
			testResults: {},
			testMessage: "",
			message: null,
			form: {
				min_interval: 300,
				channels: {
					email: {
						enabled: false,
						smtp_host: "",
						smtp_port: 465,
						smtp_ssl: true,
						smtp_tls: false,
						smtp_user: "",
						smtp_password: "",
						sender: "",
						recipients: [],
					},
					dingtalk: { enabled: false, webhook: "", secret: "" },
					feishu: { enabled: false, webhook: "", secret: "" },
				},
				thresholds: {},
			},
			channelLabels: {
				email: "邮件",
				dingtalk: "钉钉",
				feishu: "飞书",
			},
		};
	},
	async mounted() {
		await this.load();
	},
	methods: {
		async load() {
			try {
				const response = await fetch("api/4/notifications", { method: "GET" });
				if (!response.ok) {
					this.message = { type: "error", text: `加载配置失败 (HTTP ${response.status})` };
					return;
				}
				const config = await response.json();
				this.metrics = config.metrics || [];
				this.form.min_interval = config.min_interval;
				this.form.channels = config.channels;
				this.form.thresholds = config.thresholds;
				this.emailRecipients = (config.channels.email.recipients || []).join(", ");
			} catch (error) {
				this.message = { type: "error", text: `加载配置失败: ${error}` };
			}
		},
		buildPayload() {
			return {
				min_interval: this.form.min_interval,
				channels: {
					email: {
						...this.form.channels.email,
						recipients: this.emailRecipients
							.split(",")
							.map((item) => item.trim())
							.filter((item) => item),
					},
					dingtalk: this.form.channels.dingtalk,
					feishu: this.form.channels.feishu,
				},
				thresholds: this.form.thresholds,
			};
		},
		async save() {
			this.busy = true;
			this.message = null;
			try {
				const response = await fetch("api/4/notifications", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify(this.buildPayload()),
				});
				const result = await response.json();
				if (response.ok) {
					this.message = { type: "success", text: "配置已保存并立即生效" };
					// Refresh redacted secrets and recipients echo
					this.form.channels = result.channels;
					this.emailRecipients = (result.channels.email.recipients || []).join(", ");
				} else {
					this.message = { type: "error", text: `保存失败: ${result.detail || response.status}` };
				}
			} catch (error) {
				this.message = { type: "error", text: `保存失败: ${error}` };
			} finally {
				this.busy = false;
			}
		},
		async test() {
			this.busy = true;
			this.message = null;
			this.testResults = {};
			this.testMessage = "";
			try {
				const response = await fetch("api/4/notifications/test", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify(this.buildPayload()),
				});
				const result = await response.json();
				if (response.ok) {
					this.testResults = result.results || {};
					this.testMessage = result.message || "";
					if (!result.message && Object.keys(this.testResults).length === 0) {
						this.message = { type: "error", text: "没有已启用的通知渠道" };
					}
				} else {
					this.message = { type: "error", text: `测试失败: ${result.detail || response.status}` };
				}
			} catch (error) {
				this.message = { type: "error", text: `测试失败: ${error}` };
			} finally {
				this.busy = false;
			}
		},
	},
};
</script>
