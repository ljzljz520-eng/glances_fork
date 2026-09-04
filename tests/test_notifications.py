#!/usr/bin/env python
#
# Glances - An eye on your system
#
# SPDX-FileCopyrightText: 2026 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Glances alert notifications unitary tests suite (email / DingTalk / Feishu)."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from unittest import mock

import pytest
from fastapi import HTTPException

from glances.events_list import GlancesEventsList
from glances.notifications.config import (
    REDACTED,
    THRESHOLD_METRICS,
    GlancesNotificationConfig,
)
from glances.notifications.notifier import GlancesNotifier, sign

# =============================================================================
# GlancesNotificationConfig
# =============================================================================


def test_config_defaults(tmp_path):
    """Default configuration is complete and valid."""
    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'notifications.json'))
    data = cfg.as_dict()
    assert data['min_interval'] == 300
    assert set(data['channels'].keys()) == {'email', 'dingtalk', 'feishu'}
    # The 8 alertable metrics are present with the three levels
    assert len(data['thresholds']) == len(THRESHOLD_METRICS) == 8
    for metric, levels in data['thresholds'].items():
        assert levels['careful'] < levels['warning'] < levels['critical']
    assert cfg.validate() == []


def test_config_save_load_roundtrip(tmp_path):
    """Configuration persisted then reloaded is identical."""
    config_file = str(tmp_path / 'sub' / 'notifications.json')
    cfg = GlancesNotificationConfig(config_file=config_file)
    cfg.update(
        {
            'min_interval': 120,
            'channels': {
                'email': {
                    'enabled': True,
                    'smtp_host': 'smtp.example.com',
                    'sender': 'g@example.com',
                    'recipients': 'a@example.com, b@example.com',
                    'smtp_password': 'secret',
                },
                'dingtalk': {'enabled': True, 'webhook': 'https://oapi.dingtalk.com/robot/send?access_token=x'},
            },
            'thresholds': {'cpu_total': {'careful': 40, 'warning': 60, 'critical': 80}},
        }
    )
    assert cfg.validate() == []
    assert cfg.save() is True
    assert os.path.exists(config_file)

    reloaded = GlancesNotificationConfig(config_file=config_file)
    assert reloaded.as_dict() == cfg.as_dict()
    assert reloaded.get('channels.email.recipients') == ['a@example.com', 'b@example.com']


def test_config_load_missing_file(tmp_path):
    """A missing config file silently falls back to defaults."""
    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'nope' / 'notifications.json'))
    assert cfg.get('min_interval') == 300
    assert len(cfg.get('thresholds')) == 8


def test_config_save_unwritable_never_raises():
    """An unwritable configuration path returns False without raising."""
    cfg = GlancesNotificationConfig(config_file='/nonexistent-root-dir/glances/notifications.json')
    assert cfg.save() is False


def test_config_validate_thresholds(tmp_path):
    """Invalid thresholds are rejected with explicit errors."""
    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'notifications.json'))
    cfg.update({'thresholds': {'mem': {'careful': 90, 'warning': 80, 'critical': 70}}})
    errors = cfg.validate()
    assert any('内存' in e for e in errors)

    cfg2 = GlancesNotificationConfig(config_file=str(tmp_path / 'notifications2.json'))
    cfg2.update({'thresholds': {'mem': {'careful': 'abc', 'warning': 80, 'critical': 90}}})
    assert any('数字' in e for e in cfg2.validate())


def test_config_validate_enabled_channels(tmp_path):
    """Enabled channels require their mandatory fields."""
    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'notifications.json'))
    cfg.update({'channels': {'email': {'enabled': True, 'smtp_host': '', 'sender': '', 'recipients': []}}})
    errors = cfg.validate()
    assert any('SMTP' in e for e in errors)
    assert any('发件人' in e for e in errors)
    assert any('收件人' in e for e in errors)

    cfg.update({'channels': {'dingtalk': {'enabled': True, 'webhook': 'not-a-url'}}})
    assert any('http' in e for e in cfg.validate())


def test_config_secure_redaction(tmp_path):
    """Secrets are redacted in the secure view, empty values stay empty."""
    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'notifications.json'))
    cfg.update(
        {
            'channels': {
                'email': {'smtp_password': 'pw123'},
                'dingtalk': {
                    'secret': 'dtsec',
                    'webhook': 'https://oapi.dingtalk.com/robot/send?access_token=tok',
                },
            }
        }
    )
    secure = cfg.as_dict_secure()
    assert secure['channels']['email']['smtp_password'] == REDACTED
    assert secure['channels']['dingtalk']['secret'] == REDACTED
    # The webhook is a capability URL (token in query/path): it is redacted too
    assert secure['channels']['dingtalk']['webhook'] == REDACTED
    # Empty secrets stay empty (so the UI shows an empty input)
    assert secure['channels']['feishu']['secret'] == ''
    assert secure['channels']['feishu']['webhook'] == ''
    # Non sensitive fields are still visible
    assert secure['channels']['dingtalk']['enabled'] is False
    assert secure['channels']['email']['smtp_host'] == ''


def test_config_check_update_never_mutates(tmp_path):
    """A rejected or tentative update never leaves partial state behind."""
    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'notifications.json'))
    original = cfg.as_dict()

    # Invalid update: errors reported, state untouched
    errors = cfg.check_update({'thresholds': {'mem': {'careful': 90, 'warning': 80, 'critical': 70}}})
    assert errors
    assert cfg.as_dict() == original
    assert cfg.threshold_overrides() == set()

    # Valid tentative update: errors empty, state still untouched after
    assert cfg.check_update({'min_interval': 60}) == []
    assert cfg.get('min_interval') == 300

    # temporary_update restores state after the block
    with cfg.temporary_update({'min_interval': 99}):
        assert cfg.get('min_interval') == 99
    assert cfg.get('min_interval') == 300


def test_config_update_preserves_redacted_secret(tmp_path):
    """Submitting the redacted placeholder keeps the stored secret."""
    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'notifications.json'))
    cfg.update({'channels': {'email': {'smtp_password': 'real-password'}}})
    cfg.update({'channels': {'email': {'smtp_password': REDACTED}}})
    assert cfg.get('channels.email.smtp_password') == 'real-password'
    cfg.update({'channels': {'email': {'smtp_password': ''}}})
    assert cfg.get('channels.email.smtp_password') == 'real-password'


def test_config_update_preserves_redacted_webhook(tmp_path):
    """Submitting the redacted placeholder keeps the stored webhook URL."""
    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'notifications.json'))
    real_webhook = 'https://open.feishu.cn/open-apis/bot/v2/hook/real-hook-id'
    cfg.update({'channels': {'feishu': {'enabled': True, 'webhook': real_webhook, 'secret': 'fssec'}}})
    # User opens the settings dialog (webhook shown as ********) and saves
    # without modifying it
    cfg.update({'channels': {'feishu': {'enabled': True, 'webhook': REDACTED, 'secret': REDACTED}}})
    assert cfg.get('channels.feishu.webhook') == real_webhook
    assert cfg.get('channels.feishu.secret') == 'fssec'
    # A new URL replaces the old one
    cfg.update({'channels': {'feishu': {'webhook': 'https://open.feishu.cn/open-apis/bot/v2/hook/new-id'}}})
    assert cfg.get('channels.feishu.webhook').endswith('/new-id')


def test_config_webhook_is_stripped(tmp_path):
    """Pasted webhooks are stripped of surrounding whitespace/newlines."""
    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'notifications.json'))
    cfg.update({'channels': {'dingtalk': {'webhook': '  https://oapi.dingtalk.com/robot/send?access_token=tok\n'}}})
    assert cfg.get('channels.dingtalk.webhook') == 'https://oapi.dingtalk.com/robot/send?access_token=tok'
    assert cfg.validate() == []


# =============================================================================
# Events list listener hook
# =============================================================================


def test_event_listener_new_event_and_escalation():
    """Listeners fire on new WARNING events and on CRITICAL escalation only."""
    events = GlancesEventsList()
    received = []
    events.add_listener(lambda event: received.append(event))

    # A new WARNING event fires the listener once
    events.add('WARNING', 'CPU_TOTAL', 85.0, proc_list=[], proc_desc='')
    assert len(received) == 1
    assert received[0]['type'] == 'CPU_TOTAL'
    assert received[0]['state'] == 'WARNING'

    # Ongoing WARNING updates do not fire again
    events.add('WARNING', 'CPU_TOTAL', 86.0, proc_list=[], proc_desc='')
    assert len(received) == 1

    # Escalation to CRITICAL fires once more
    events.add('CRITICAL', 'CPU_TOTAL', 95.0, proc_list=[], proc_desc='')
    assert len(received) == 2
    assert received[1]['state'] == 'CRITICAL'

    # Sustained CRITICAL does not fire again
    events.add('CRITICAL', 'CPU_TOTAL', 96.0, proc_list=[], proc_desc='')
    assert len(received) == 2


def test_event_listener_exception_isolation():
    """A failing listener never breaks the events processing."""
    events = GlancesEventsList()
    received = []
    events.add_listener(lambda event: (_ for _ in ()).throw(RuntimeError('boom')))
    events.add_listener(lambda event: received.append(event))

    events.add('WARNING', 'MEM', 95.0, proc_list=[], proc_desc='')
    # The second listener still runs and add() returns normally
    assert len(received) == 1
    assert received[0]['type'] == 'MEM'


# =============================================================================
# Notifier: signing, cooldown, channels
# =============================================================================


@pytest.fixture(autouse=True)
def _unregister_test_notifiers():
    """Remove event listeners registered by notifiers created in each test.

    Prevents a test notifier (with test channels enabled) from receiving
    events fired by other test suites and attempting real external sends.
    """
    from glances.events_list import glances_events

    count_before = len(glances_events._listeners)
    yield
    del glances_events._listeners[count_before:]


def _test_notifier(tmp_path, **overrides):
    """Build a notifier backed by a tmp configuration file."""
    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'notifications.json'))
    if overrides:
        cfg.update(overrides)
    return GlancesNotifier(config=cfg)


def test_sign_algorithm_matches_official():
    """DingTalk/Feishu signature equals the documented HMAC-SHA256 derivation.

    Uses hard-coded expected values (computed once independently) so a
    missing base64/url-encode step cannot be hidden by recomputing.
    """
    # DingTalk: millisecond timestamp
    assert sign('1700000000000', 'SECtest123') == 'w3RMHXzixTMdzr8OHJUmVLS4IoPJVdu%2BUt1LE48MePE%3D'
    # Feishu: second timestamp
    assert sign('1700000000', 'FSsecret') == '7UeOZeYThEBlx2l7bJs6cYx6%2FIEEybzpUNXwbzUBiTo%3D'
    # Cross-check the millisecond vector against the documented primitive
    timestamp, secret = '1700000000000', 'SECtest123'
    expected = urllib.parse.quote_plus(
        base64.b64encode(
            hmac.new(
                secret.encode(),
                f'{timestamp}\n{secret}'.encode(),
                digestmod=hashlib.sha256,
            ).digest()
        )
    )
    assert sign(timestamp, secret) == expected


def test_notifier_disabled_metric_is_skipped(tmp_path):
    """Events on disabled metrics do not trigger any send."""
    notifier = _test_notifier(tmp_path, thresholds={'cpu_total': {'enabled': False}})
    with mock.patch.object(notifier, '_fan_out') as fan_out:
        notifier._on_event({'state': 'WARNING', 'type': 'CPU_TOTAL', 'begin': time.time(), 'max': 80, 'avg': 75})
        time.sleep(0.3)
        fan_out.assert_not_called()


def test_notifier_cooldown_and_critical_escalation(tmp_path):
    """Same metric within min_interval is suppressed; CRITICAL bypasses it."""
    notifier = _test_notifier(tmp_path, min_interval=300)
    states = []

    def record_fanout(event, channels=None):
        states.append(event['state'])

    with mock.patch.object(notifier, '_fan_out', side_effect=record_fanout):
        notifier._on_event({'state': 'WARNING', 'type': 'CPU_USER', 'begin': time.time(), 'max': 80, 'avg': 75})
        time.sleep(0.3)
        notifier._on_event({'state': 'WARNING', 'type': 'CPU_USER', 'begin': time.time(), 'max': 81, 'avg': 76})
        time.sleep(0.3)
        notifier._on_event({'state': 'CRITICAL', 'type': 'CPU_USER', 'begin': time.time(), 'max': 95, 'avg': 90})
        time.sleep(0.5)

    assert states == ['WARNING', 'CRITICAL']


def test_notifier_send_is_non_blocking(tmp_path):
    """Notification sends run in a background thread."""
    notifier = _test_notifier(tmp_path)

    def slow_fanout(event, channels=None):
        time.sleep(2)

    with mock.patch.object(notifier, '_fan_out', side_effect=slow_fanout):
        start = time.time()
        notifier._on_event({'state': 'WARNING', 'type': 'MEM', 'begin': time.time(), 'max': 90, 'avg': 88})
        elapsed = time.time() - start
    assert elapsed < 1.0


def test_notifier_message_content(tmp_path):
    """The notification body carries hostname, level, metric and details."""
    notifier = _test_notifier(tmp_path)
    event = {
        'state': 'CRITICAL',
        'type': 'CPU_TOTAL',
        'begin': 1700000000,
        'max': 95.5,
        'avg': 92.3,
        'top': ['chrome', 'python'],
        'global_msg': 'High CPU user mode',
    }
    subject = notifier._build_subject(event)
    text = notifier._build_text(event)
    assert 'CRITICAL' in subject and 'CPU_TOTAL' in subject
    assert 'CRITICAL' in text and 'CPU_TOTAL' in text
    assert '95.5' in text
    assert 'chrome, python' in text
    assert 'High CPU user mode' in text


def test_send_email_ssl_and_starttls(tmp_path):
    """Emails are built and sent through SMTP_SSL or SMTP+STARTTLS with timeout."""
    event = {'state': 'WARNING', 'type': 'MEM', 'begin': time.time(), 'max': 90, 'avg': 88, 'top': [], 'global_msg': ''}

    # SSL path
    notifier = _test_notifier(
        tmp_path,
        channels={
            'email': {
                'enabled': True,
                'smtp_host': 'smtp.example.com',
                'smtp_port': 465,
                'smtp_ssl': True,
                'smtp_user': 'user@example.com',
                'smtp_password': 'pw',
                'sender': 'g@example.com',
                'recipients': ['a@example.com', 'b@example.com'],
            }
        },
    )
    with mock.patch('smtplib.SMTP_SSL') as mock_ssl:
        notifier._fan_out(event)
        smtp = mock_ssl.return_value
        assert mock_ssl.call_args.args[:2] == ('smtp.example.com', 465)
        assert mock_ssl.call_args.kwargs['timeout'] == 10
        # The SMTP connection verifies the certificate by default
        assert mock_ssl.call_args.kwargs['context'].verify_mode is not None
        smtp.login.assert_called_once_with('user@example.com', 'pw')
        smtp.send_message.assert_called_once()
        smtp.quit.assert_called_once()
        msg = smtp.send_message.call_args[0][0]
        assert msg['From'] == 'g@example.com'
        assert msg['To'] == 'a@example.com, b@example.com'
        assert 'WARNING' in msg['Subject'] and 'MEM' in msg['Subject']

    # STARTTLS path
    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'n2.json'))
    cfg.update(
        {
            'channels': {
                'email': {
                    'enabled': True,
                    'smtp_host': 'smtp.example.com',
                    'smtp_port': 587,
                    'smtp_ssl': False,
                    'smtp_tls': True,
                    'smtp_user': '',
                    'sender': 'g@example.com',
                    'recipients': ['a@example.com'],
                }
            }
        }
    )
    notifier2 = GlancesNotifier(config=cfg)
    with mock.patch('smtplib.SMTP') as mock_smtp:
        notifier2._fan_out(event)
        smtp = mock_smtp.return_value
        # STARTTLS is called with a certificate-verifying context
        assert smtp.starttls.call_args.kwargs['context'].verify_mode is not None
        smtp.login.assert_not_called()
        smtp.send_message.assert_called_once()


class _FakeHTTPResponse:
    """Fake context manager returned by urlopen."""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode('utf-8')


def test_send_dingtalk_and_feishu_webhooks(tmp_path):
    """Webhooks are POSTed with signature params and provider payloads."""
    notifier = _test_notifier(
        tmp_path,
        channels={
            'dingtalk': {
                'enabled': True,
                'webhook': 'https://oapi.dingtalk.com/robot/send?access_token=tok',
                'secret': 'dtsec',
            },
            'feishu': {
                'enabled': True,
                'webhook': 'https://open.feishu.cn/open-apis/bot/v2/hook/hookid',
                'secret': 'fssec',
            },
        },
    )
    event = {
        'state': 'CRITICAL',
        'type': 'LOAD',
        'begin': time.time(),
        'max': 5.2,
        'avg': 4.8,
        'top': [],
        'global_msg': '',
    }

    with mock.patch(
        'urllib.request.urlopen',
        return_value=_FakeHTTPResponse({'errcode': 0, 'code': 0, 'errmsg': 'ok', 'msg': 'success'}),
    ) as mock_urlopen:
        results = notifier._fan_out(event)

    assert mock_urlopen.call_count == 2
    assert results['dingtalk']['ok'] is True
    assert results['feishu']['ok'] is True

    urls = [call.args[0].full_url for call in mock_urlopen.call_args_list]
    assert all('timestamp=' in u and 'sign=' in u for u in urls)

    payloads = [json.loads(call.args[0].data.decode('utf-8')) for call in mock_urlopen.call_args_list]
    assert payloads[0]['msgtype'] == 'markdown'
    assert 'CRITICAL' in payloads[0]['markdown']['title']
    assert payloads[1]['msg_type'] == 'text'
    assert 'Glances' in payloads[1]['content']['text']
    # Timeout is enforced on every call
    assert all(call.kwargs.get('timeout') == 10 for call in mock_urlopen.call_args_list)


def test_webhook_error_responses_raise(tmp_path):
    """Provider error codes are turned into exceptions."""
    notifier = _test_notifier(
        tmp_path,
        channels={'dingtalk': {'enabled': True, 'webhook': 'https://oapi.dingtalk.com/robot/send?access_token=tok'}},
    )
    error_response = _FakeHTTPResponse({'errcode': 310000, 'errmsg': 'bad sign'})
    with mock.patch('urllib.request.urlopen', return_value=error_response):
        mem_event = {
            'state': 'WARNING',
            'type': 'MEM',
            'begin': time.time(),
            'max': 90,
            'avg': 88,
            'top': [],
            'global_msg': '',
        }
        results = notifier._fan_out(mem_event)
    assert results['dingtalk']['ok'] is False
    assert 'bad sign' in results['dingtalk']['error']


def test_channel_failure_isolation_and_history(tmp_path):
    """One failing channel does not prevent the others; history is recorded."""
    notifier = _test_notifier(
        tmp_path,
        channels={
            'dingtalk': {'enabled': True, 'webhook': 'https://oapi.dingtalk.com/robot/send?access_token=tok'},
            'feishu': {'enabled': True, 'webhook': 'https://open.feishu.cn/open-apis/bot/v2/hook/hookid'},
        },
    )

    def boom(channel, subject, text):
        raise RuntimeError('connection refused')

    notifier._send_dingtalk = boom
    with mock.patch('urllib.request.urlopen', return_value=_FakeHTTPResponse({'code': 0, 'msg': 'success'})):
        results = notifier._fan_out(
            {'state': 'WARNING', 'type': 'MEM', 'begin': time.time(), 'max': 90, 'avg': 88, 'top': [], 'global_msg': ''}
        )

    assert results['dingtalk']['ok'] is False
    assert 'connection refused' in results['dingtalk']['error']
    assert results['feishu']['ok'] is True
    assert notifier.history[0]['ok'] is True
    assert any(h['channel'] == 'dingtalk' and h['ok'] is False for h in notifier.history)


def test_send_test(tmp_path):
    """send_test returns per-channel results and skips disabled channels."""
    notifier = _test_notifier(
        tmp_path,
        channels={'feishu': {'enabled': True, 'webhook': 'https://open.feishu.cn/open-apis/bot/v2/hook/hookid'}},
    )
    with mock.patch('urllib.request.urlopen', return_value=_FakeHTTPResponse({'code': 0, 'msg': 'success'})):
        results = notifier.send_test()
    assert set(results.keys()) == {'feishu'}
    assert results['feishu']['ok'] is True

    # No enabled channel at all
    idle = _test_notifier(tmp_path)
    assert idle.send_test() == {}


# =============================================================================
# Thresholds application
# =============================================================================


class _FakePlugin:
    def __init__(self, name):
        self.plugin_name = name
        self._limits = {}

    def set_limits(self, item, value):
        self._limits[f'{self.plugin_name}_{item}'] = value

    def get_limit(self, criticality, stat_name=''):
        return self._limits.get(f'{stat_name}_{criticality}') or self._limits.get(f'{self.plugin_name}_{criticality}')


class _FakeStats:
    def __init__(self, plugins):
        self._plugins = plugins

    def get_plugin(self, name):
        return self._plugins.get(name)


def test_apply_thresholds_to_plugins(tmp_path):
    """Configured thresholds are written on the matching plugin limits."""
    stats = _FakeStats({'cpu': _FakePlugin('cpu'), 'mem': _FakePlugin('mem')})
    notifier = _test_notifier(
        tmp_path,
        thresholds={'cpu_total': {'careful': 40, 'warning': 60, 'critical': 80}},
    )
    notifier.apply_thresholds(stats)
    cpu = stats.get_plugin('cpu')
    assert cpu._limits['cpu_total_careful'] == 40.0
    assert cpu._limits['cpu_total_warning'] == 60.0
    assert cpu._limits['cpu_total_critical'] == 80.0
    assert cpu.get_limit('critical', 'cpu_total') == 80.0
    # Non customized metrics are not touched
    assert stats.get_plugin('mem')._limits == {}


def test_apply_thresholds_on_real_stats(tmp_path, glances_stats):
    """apply_thresholds works against the real GlancesStats plugins."""
    notifier = _test_notifier(
        tmp_path,
        thresholds={
            'cpu_total': {'careful': 41, 'warning': 61, 'critical': 81},
            'mem': {'careful': 42, 'warning': 62, 'critical': 82},
            'load': {'careful': 0.4, 'warning': 0.9, 'critical': 4.0},
        },
    )
    notifier.apply_thresholds(glances_stats)
    cpu = glances_stats.get_plugin('cpu')
    mem = glances_stats.get_plugin('mem')
    load = glances_stats.get_plugin('load')
    assert cpu.get_limit('critical', 'cpu_total') == 81.0
    assert mem.get_limit('warning', 'mem') == 62.0
    assert load.get_limit('critical', 'load') == 4.0


def test_apply_thresholds_missing_plugin_never_raises(tmp_path):
    """Metrics whose plugin is unavailable are skipped silently."""
    stats = _FakeStats({})  # no plugin at all
    notifier = _test_notifier(tmp_path)
    notifier.apply_thresholds(stats)  # must not raise


def test_default_config_does_not_override_plugin_limits(tmp_path):
    """A notifier without any saved customization never touches plugin limits."""
    cpu = _FakePlugin('cpu')
    cpu._limits['cpu_total_critical'] = 85.0  # value coming from glances.conf
    stats = _FakeStats({'cpu': cpu})

    # Fresh config (no notifications.json, no update)
    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'notifications.json'))
    notifier = GlancesNotifier(config=cfg)
    assert notifier.config.threshold_overrides() == set()
    notifier.apply_thresholds(stats)
    # The glances.conf value is untouched
    assert cpu._limits['cpu_total_critical'] == 85.0

    # After submitting cpu_total thresholds, the override is applied
    notifier.config.update({'thresholds': {'cpu_total': {'careful': 40, 'warning': 60, 'critical': 80}}})
    assert 'cpu_total' in notifier.config.threshold_overrides()
    notifier.apply_thresholds(stats)
    assert cpu._limits['cpu_total_critical'] == 80.0


def test_effective_thresholds_reflect_plugin_limits(tmp_path):
    """The GET view reports the live plugin thresholds, not config defaults."""
    cpu = _FakePlugin('cpu')
    # Simulate glances.conf custom values
    cpu._limits.update({'cpu_total_careful': 65.0, 'cpu_total_warning': 75.0, 'cpu_total_critical': 85.0})
    mem = _FakePlugin('mem')
    mem._limits.update({'mem_careful': 50.0, 'mem_warning': 70.0, 'mem_critical': 90.0})
    stats = _FakeStats({'cpu': cpu, 'mem': mem})

    notifier = _test_notifier(tmp_path)  # config defaults 50/70/90
    effective = notifier.effective_thresholds(stats)
    # Live plugin values win over the hard-coded defaults
    assert effective['cpu_total'] == {
        'enabled': True,
        'careful': 65.0,
        'warning': 75.0,
        'critical': 85.0,
    }
    assert effective['mem']['warning'] == 70.0

    # The enabled flag comes from the notification configuration
    notifier.config.update({'thresholds': {'cpu_total': {'enabled': False}}})
    effective = notifier.effective_thresholds(stats)
    assert effective['cpu_total']['enabled'] is False
    assert effective['cpu_total']['critical'] == 85.0


def test_effective_thresholds_fallback_to_config_defaults(tmp_path):
    """Metrics without any plugin limit fall back to the config defaults."""
    notifier = _test_notifier(tmp_path)
    effective = notifier.effective_thresholds(_FakeStats({}))
    assert effective['cpu_total']['critical'] == 90
    assert effective['load']['critical'] == 5.0


# =============================================================================
# REST API handlers
# =============================================================================


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        if self._payload is None:
            raise ValueError('invalid json')
        return self._payload


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    """Patch the REST API module to use a tmp-file notifier with a fake stats."""
    import glances.outputs.glances_restful_api as api_mod

    cfg = GlancesNotificationConfig(config_file=str(tmp_path / 'notifications.json'))
    notifier = GlancesNotifier(config=cfg)
    fake_stats = _FakeStats({'cpu': _FakePlugin('cpu'), 'mem': _FakePlugin('mem')})

    api = api_mod.GlancesRestfulApi.__new__(api_mod.GlancesRestfulApi)
    api.stats = fake_stats

    monkeypatch.setattr(api_mod, 'glances_notifier', notifier)
    return api, notifier, fake_stats


def test_api_notifications_get(api_env):
    api, _notifier, _stats = api_env
    response = api._api_notifications_get()
    body = json.loads(response.body)
    assert len(body['metrics']) == 8
    assert body['history'] == []
    assert 'channels' in body and 'thresholds' in body
    assert body['channels']['email']['smtp_password'] == ''
    # Webhooks are redacted when set
    assert body['channels']['feishu']['webhook'] == ''


def test_api_notifications_get_returns_effective_thresholds(api_env):
    """GET thresholds reflect the live plugin limits and redacted webhooks."""
    api, notifier, stats = api_env
    # Simulate a glances.conf value present in the live plugin
    stats.get_plugin('cpu')._limits['cpu_total_critical'] = 85.0
    notifier.config.update(
        {
            'channels': {
                'dingtalk': {
                    'enabled': True,
                    'webhook': 'https://oapi.dingtalk.com/robot/send?access_token=tok',
                }
            },
            'thresholds': {'cpu_total': {'enabled': False}},
        }
    )
    body = json.loads(api._api_notifications_get().body)
    assert body['thresholds']['cpu_total']['critical'] == 85.0
    assert body['thresholds']['cpu_total']['enabled'] is False
    # Capability URL is never returned in clear text
    assert body['channels']['dingtalk']['webhook'] == REDACTED


def test_api_notifications_post_applies_and_persists(api_env, tmp_path):
    api, _notifier, stats = api_env
    payload = {
        'channels': {
            'dingtalk': {
                'enabled': True,
                'webhook': 'https://oapi.dingtalk.com/robot/send?access_token=abc',
                'secret': 'sec123',
            }
        },
        'thresholds': {'cpu_total': {'enabled': True, 'careful': 40, 'warning': 60, 'critical': 80}},
        'min_interval': 120,
    }
    response = asyncio.run(api._api_notifications_set(_FakeRequest(payload)))
    body = json.loads(response.body)
    assert body['channels']['dingtalk']['secret'] == REDACTED
    # Hot applied to the plugins
    assert stats.get_plugin('cpu')._limits['cpu_total_critical'] == 80.0
    # Persisted to disk
    saved = json.loads((tmp_path / 'notifications.json').read_text(encoding='utf-8'))
    assert saved['min_interval'] == 120
    assert saved['channels']['dingtalk']['secret'] == 'sec123'


def test_api_notifications_post_invalid_returns_400(api_env):
    api, _notifier, _stats = api_env
    payload = {'thresholds': {'mem': {'careful': 90, 'warning': 80, 'critical': 70}}}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api._api_notifications_set(_FakeRequest(payload)))
    assert exc.value.status_code == 400


def test_api_notifications_post_bad_json_returns_400(api_env):
    api, _notifier, _stats = api_env
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api._api_notifications_set(_FakeRequest(None)))
    assert exc.value.status_code == 400


def test_api_notifications_test(api_env):
    api, notifier, _stats = api_env
    notifier._send_dingtalk = lambda *a, **k: None
    payload = {
        'channels': {'dingtalk': {'enabled': True, 'webhook': 'https://oapi.dingtalk.com/robot/send?access_token=abc'}}
    }
    response = asyncio.run(api._api_notifications_test(_FakeRequest(payload)))
    body = json.loads(response.body)
    assert body['results']['dingtalk']['ok'] is True
