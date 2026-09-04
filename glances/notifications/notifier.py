#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2026 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Alert notifier.

Send alert notifications (email, DingTalk and Feishu webhooks) when the
Glances events list creates a new WARNING/CRITICAL event or when an event
escalates to CRITICAL.

Only Python standard library is used (smtplib, urllib, hmac...).
"""

import base64
import hashlib
import hmac
import json
import smtplib
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage

from glances.events_list import glances_events
from glances.logger import logger
from glances.notifications.config import THRESHOLD_METRICS, GlancesNotificationConfig

# Network operations timeout (seconds)
TIMEOUT = 10

# Default SSL context verifies the server certificate (MITM hardening)
SSL_CONTEXT = ssl.create_default_context()

# Max number of send results kept in memory
HISTORY_MAX = 10


def sign(timestamp, secret):
    """Compute the DingTalk/Feishu webhook signature.

    HmacSHA256(timestamp + "\\n" + secret, key=secret), Base64 then URL-encode.
    """
    string_to_sign = f'{timestamp}\n{secret}'
    hmac_code = hmac.new(
        secret.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        digestmod=hashlib.sha256,
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(hmac_code))


class GlancesNotifier:
    """Manage alert notifications for the Glances events."""

    def __init__(self, config=None):
        # Notification configuration (channels + thresholds)
        self.config = config if config is not None else GlancesNotificationConfig()

        # Last notification timestamp per metric type: {type: (timestamp, state)}
        self._last_sent = {}

        # In memory send history (most recent first)
        self.history = []

        # Listen to Glances events
        glances_events.add_listener(self._on_event)

    def unregister(self):
        """Unregister the notifier from the Glances events.

        Mainly useful for tests so a notifier instance never sends
        notifications from unrelated events fired after the test ends.
        """
        glances_events.remove_listener(self._on_event)

    def _on_event(self, event):
        """Listener callback: fired on new event or CRITICAL escalation.

        Runs in the stats update thread; the actual sends are performed in
        a background daemon thread to never block the stats loop.
        """
        metric = event.get('type', '').lower()
        state = event.get('state', '')

        if not self.config.is_metric_enabled(metric):
            return

        now = time.time()
        last = self._last_sent.get(metric)
        min_interval = self.config.get('min_interval', 300)
        # CRITICAL escalation bypasses the cooldown
        if last is not None and now - last[0] < min_interval and not (state == 'CRITICAL' and last[1] != 'CRITICAL'):
            logger.debug(f"Notification for {metric} suppressed by cooldown")
            return

        # Record immediately to avoid duplicate sends from concurrent events
        self._last_sent[metric] = (now, state)

        thread = threading.Thread(target=self._fan_out, args=(event,), daemon=True)
        thread.start()

    def _fan_out(self, event, channels=None):
        """Send the notification to every enabled (or selected) channel.

        Return a dict: {channel_name: {"ok": bool, "error": str?}}
        """
        subject = self._build_subject(event)
        text = self._build_text(event)
        results = {}

        channel_names = channels or ['email', 'dingtalk', 'feishu']
        for name in channel_names:
            channel = self.config.get(f'channels.{name}', {})
            if not channel or not channel.get('enabled'):
                continue
            try:
                if name == 'email':
                    self._send_email(channel, subject, text)
                elif name == 'dingtalk':
                    self._send_dingtalk(channel, subject, text)
                elif name == 'feishu':
                    self._send_feishu(channel, subject, text)
                else:
                    continue
            except Exception as e:
                logger.error(f"Notification via {name} failed: {e}")
                results[name] = {'ok': False, 'error': str(e)}
                self._add_history(name, event, ok=False, error=str(e))
            else:
                logger.info(f"Notification sent via {name}: {subject}")
                results[name] = {'ok': True}
                self._add_history(name, event, ok=True)

        return results

    def send_test(self, channels=None):
        """Send a test notification to enabled (or selected) channels."""
        test_event = {
            'state': 'TEST',
            'type': 'TEST',
            'begin': time.time(),
            'max': 0,
            'avg': 0,
            'top': [],
            'global_msg': '这是一条来自 Glances 的测试通知',
        }
        return self._fan_out(test_event, channels=channels)

    def _add_history(self, channel, event, ok, error=''):
        """Record a send result in the in-memory history."""
        self.history.insert(
            0,
            {
                'time': datetime.now().isoformat(),
                'channel': channel,
                'type': event.get('type', ''),
                'state': event.get('state', ''),
                'ok': ok,
                'error': error,
            },
        )
        del self.history[HISTORY_MAX:]

    @staticmethod
    def _build_subject(event):
        """Build the notification subject/title."""
        hostname = socket.gethostname()
        return f"[Glances告警][{event.get('state', '')}] {event.get('type', '')} on {hostname}"

    @staticmethod
    def _build_text(event):
        """Build the notification body (plain text)."""
        hostname = socket.gethostname()
        begin = event.get('begin')
        try:
            event_time = datetime.fromtimestamp(begin).strftime('%Y-%m-%d %H:%M:%S')
        except (TypeError, OSError):
            event_time = str(begin)

        lines = [
            'Glances 告警通知',
            f'主机: {hostname}',
            f"级别: {event.get('state', '')}",
            f"指标: {event.get('type', '')}",
            f"当前值: 最大 {event.get('max', 0):.1f} / 平均 {event.get('avg', 0):.1f}",
            f'时间: {event_time}',
        ]
        if event.get('global_msg'):
            lines.append(f"概要: {event['global_msg']}")
        if event.get('top'):
            lines.append('Top 进程: ' + ', '.join(event['top']))
        return '\n'.join(lines)

    def _send_email(self, channel, subject, text):
        """Send the notification by email (SMTP)."""
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = channel['sender']
        msg['To'] = ', '.join(channel['recipients'])
        msg.set_content(text)

        if channel.get('smtp_ssl'):
            smtp = smtplib.SMTP_SSL(
                channel['smtp_host'],
                channel.get('smtp_port', 465),
                timeout=TIMEOUT,
                context=SSL_CONTEXT,
            )
        else:
            smtp = smtplib.SMTP(channel['smtp_host'], channel.get('smtp_port', 587), timeout=TIMEOUT)
            if channel.get('smtp_tls'):
                smtp.starttls(context=SSL_CONTEXT)
        try:
            if channel.get('smtp_user'):
                smtp.login(channel['smtp_user'], channel.get('smtp_password', ''))
            smtp.send_message(msg)
        finally:
            smtp.quit()

    @staticmethod
    def _http_post_json(url, payload):
        """POST a JSON payload to the given URL and return the parsed response."""
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode('utf-8'))

    def _send_dingtalk(self, channel, subject, text):
        """Send the notification through a DingTalk custom robot webhook."""
        url = channel['webhook']
        secret = channel.get('secret')
        if secret:
            timestamp = str(round(time.time() * 1000))
            sep = '&' if '?' in url else '?'
            url = f"{url}{sep}timestamp={timestamp}&sign={sign(timestamp, secret)}"

        payload = {
            'msgtype': 'markdown',
            'markdown': {
                'title': subject,
                'text': f'### {subject}\n\n' + '\n\n'.join(f'- {line}' for line in text.split('\n')[1:]),
            },
        }
        result = self._http_post_json(url, payload)
        if result.get('errcode') != 0:
            raise RuntimeError(f"DingTalk error: {result.get('errmsg', result)}")

    def _send_feishu(self, channel, subject, text):
        """Send the notification through a Feishu custom robot webhook."""
        url = channel['webhook']
        secret = channel.get('secret')
        if secret:
            timestamp = str(round(time.time()))
            sep = '&' if '?' in url else '?'
            url = f"{url}{sep}timestamp={timestamp}&sign={sign(timestamp, secret)}"

        payload = {
            'msg_type': 'text',
            'content': {'text': f'{subject}\n{text}'},
        }
        result = self._http_post_json(url, payload)
        # Feishu returns {"code": 0, "msg": "success"} on success (older
        # robots may return {"StatusCode": 0})
        if result.get('code', result.get('StatusCode', 0)) != 0:
            raise RuntimeError(f"Feishu error: {result.get('msg', result)}")

    def effective_thresholds(self, stats):
        """Return the alert thresholds currently effective for each metric.

        Values come from the live plugins limits (glances.conf defaults and
        web-configured overrides merged), so the WebUI displays the real
        thresholds instead of hard-coded defaults. The notification enabled
        flag comes from the notification configuration.
        """
        configured = self.config.get('thresholds', {})
        effective = {}
        for metric_name, (plugin_name, _prefix, _label, _unit) in THRESHOLD_METRICS.items():
            cfg_metric = configured.get(metric_name, {})
            metric = {'enabled': cfg_metric.get('enabled', True)}
            plugin = stats.get_plugin(plugin_name) if stats is not None else None
            for level in ('careful', 'warning', 'critical'):
                value = plugin.get_limit(level, stat_name=metric_name) if plugin is not None else None
                metric[level] = value if value is not None else cfg_metric.get(level)
            effective[metric_name] = metric
        return effective

    def apply_thresholds(self, stats):
        """Apply the configured thresholds to the plugins limits (in memory).

        Only metrics explicitly customized by the user (loaded from the
        notification configuration file or saved through the WebUI) are
        applied, so the glances.conf / default behavior is unchanged until
        then. Makes web-configured thresholds effective immediately without
        restart.
        """
        thresholds = self.config.get('thresholds', {})
        for metric_name in self.config.threshold_overrides():
            if metric_name not in THRESHOLD_METRICS:
                continue
            metric = thresholds.get(metric_name)
            if metric is None:
                continue
            plugin_name, prefix, label, _unit = THRESHOLD_METRICS[metric_name]
            plugin = stats.get_plugin(plugin_name)
            if plugin is None:
                logger.debug(f"Cannot apply thresholds for {label}: plugin {plugin_name} not available")
                continue
            for level in ('careful', 'warning', 'critical'):
                item = f'{prefix}_{level}' if prefix else level
                try:
                    plugin.set_limits(item, float(metric[level]))
                except (KeyError, TypeError, ValueError) as e:
                    logger.debug(f"Cannot apply threshold {metric_name}_{level}: {e}")


# Global notifier instance, registers itself on the Glances events
glances_notifier = GlancesNotifier()
