#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2026 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Alert notification configuration.

Manage the notification configuration (channels and alert thresholds)
persisted in a standalone JSON file (notifications.json) stored in the
Glances user configuration directory.
"""

import contextlib
import copy
import json
import os
import tempfile

from glances.config import secure_option, user_config_dir
from glances.logger import logger

CONFIG_FILENAME = 'notifications.json'

# Value returned by the API for redacted secrets and expected back when
# the user did not modify the field in the WebUI.
REDACTED = '********'

# Alertable metrics metadata.
# key: metric name (lowercase event type, also plugin stat name)
# value: (plugin_name, limit_prefix, UI label, unit)
# limit_prefix is the key part between the plugin name and the level:
#   plugin limit key = plugin_name + '_' + limit_prefix + ('_' if prefix else '') + level
#   e.g. cpu + total + critical -> cpu_total_critical
#        mem + ''    + warning  -> mem_warning
THRESHOLD_METRICS = {
    'cpu_total': ('cpu', 'total', 'CPU 总使用率', '%'),
    'cpu_user': ('cpu', 'user', 'CPU 用户态使用率', '%'),
    'cpu_system': ('cpu', 'system', 'CPU 内核态使用率', '%'),
    'cpu_iowait': ('cpu', 'iowait', 'CPU I/O 等待', '%'),
    'cpu_steal': ('cpu', 'steal', 'CPU 被虚拟化窃取', '%'),
    'mem': ('mem', '', '内存使用率', '%'),
    'memswap': ('memswap', '', 'Swap 使用率', '%'),
    'load': ('load', '', '系统负载（每核）', ''),
}

# Default thresholds match the glances.conf defaults.
# CPU / mem / memswap are percentages, load is a per-core load ratio.
_DEFAULT_THRESHOLDS = {
    'cpu_total': {'careful': 50, 'warning': 70, 'critical': 90},
    'cpu_user': {'careful': 50, 'warning': 70, 'critical': 90},
    'cpu_system': {'careful': 50, 'warning': 70, 'critical': 90},
    'cpu_iowait': {'careful': 50, 'warning': 70, 'critical': 90},
    'cpu_steal': {'careful': 50, 'warning': 70, 'critical': 90},
    'mem': {'careful': 50, 'warning': 70, 'critical': 90},
    'memswap': {'careful': 50, 'warning': 70, 'critical': 90},
    'load': {'careful': 0.7, 'warning': 1.0, 'critical': 5.0},
}

DEFAULT_CONFIG = {
    'version': 1,
    # Minimal interval between two notifications for the same metric (seconds)
    'min_interval': 300,
    'channels': {
        'email': {
            'enabled': False,
            'smtp_host': '',
            'smtp_port': 465,
            'smtp_ssl': True,
            'smtp_tls': False,
            'smtp_user': '',
            'smtp_password': '',
            'sender': '',
            'recipients': [],
        },
        'dingtalk': {
            'enabled': False,
            'webhook': '',
            'secret': '',
        },
        'feishu': {
            'enabled': False,
            'webhook': '',
            'secret': '',
        },
    },
    'thresholds': {metric: {'enabled': True, **levels} for metric, levels in _DEFAULT_THRESHOLDS.items()},
}

# Channel fields that must never be returned in clear text.
# webhook URLs are capability links: they embed the robot token in the
# query string (DingTalk access_token) or in the URL path (Feishu hook id),
# so they are redacted like passwords.
_SENSITIVE_FIELDS = {'smtp_password', 'secret', 'webhook'}


def default_config_file():
    """Return the default path of the notifications.json file."""
    for config_dir in user_config_dir():
        if config_dir:
            return os.path.join(config_dir, CONFIG_FILENAME)
    return None


def _deep_merge(base, override):
    """Merge override into a deep copy of base (dict only, overrides lists)."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes', 'on')
    if value is None:
        return default
    return bool(value)


def _to_number(value):
    """Convert value to int or float, raise ValueError if not possible."""
    if isinstance(value, bool):
        raise ValueError('boolean is not a number')
    if isinstance(value, (int, float)):
        return value
    return float(str(value).strip())


class GlancesNotificationConfig:
    """Load, validate, persist and expose the notification configuration."""

    def __init__(self, config_file=None):
        self._config_file = config_file or default_config_file()
        self._config = copy.deepcopy(DEFAULT_CONFIG)
        # Metrics whose thresholds were explicitly customized (loaded from
        # file or submitted through the API). Only these are applied to the
        # plugins limits, so default Glances behavior is unchanged until the
        # user actually saves thresholds.
        self._threshold_overrides = set()
        self.load()

    def threshold_overrides(self):
        """Return the set of metric names with user-customized thresholds."""
        return set(self._threshold_overrides)

    @property
    def config_file(self):
        """Return the configuration file path."""
        return self._config_file

    def load(self):
        """Load the configuration from the JSON file.

        Use the default values if the file does not exist or is invalid.
        """
        if not self._config_file or not os.path.exists(self._config_file):
            return False
        try:
            with open(self._config_file, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning(f"Cannot read notification configuration '{self._config_file}': {e}")
            return False
        self._config = _deep_merge(DEFAULT_CONFIG, data)
        self._threshold_overrides = {metric for metric in data.get('thresholds', {}) if metric in THRESHOLD_METRICS}
        logger.info(f"Read notification configuration file '{self._config_file}'")
        return True

    def save(self):
        """Persist the configuration to the JSON file (atomic write).

        Return True on success, False otherwise (never raises).
        """
        if not self._config_file:
            logger.warning("No notification configuration directory available")
            return False
        config_dir = os.path.dirname(self._config_file)
        try:
            os.makedirs(config_dir, exist_ok=True)
            # Atomic write: temp file in the same directory then rename
            fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(self._config, f, indent=4, ensure_ascii=False)
                os.replace(tmp_path, self._config_file)
            except Exception:
                # Clean up the temp file on error
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            logger.warning(f"Cannot write notification configuration '{self._config_file}': {e}")
            return False
        logger.info(f"Notification configuration saved in '{self._config_file}'")
        return True

    def as_dict(self):
        """Return a deep copy of the whole configuration."""
        return copy.deepcopy(self._config)

    def as_dict_secure(self):
        """Return a sanitised copy of the configuration for the API.

        Sensitive fields are redacted when non empty. Credentials embedded
        in URLs are also redacted.
        """
        secure = self.as_dict()
        for channel in secure['channels'].values():
            for field in list(channel.keys()):
                value = channel[field]
                if field in _SENSITIVE_FIELDS:
                    # Redact only non-empty secrets
                    channel[field] = REDACTED if value else value
                elif isinstance(value, str):
                    channel[field] = secure_option(field, value)
        return secure

    def get(self, path, default=None):
        """Get a configuration value from a dotted path (e.g. channels.email.smtp_host)."""
        node = self._config
        for part in path.split('.'):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def is_metric_enabled(self, metric_name):
        """Return True if notifications are enabled for the given metric."""
        metric = self._config['thresholds'].get(metric_name)
        return bool(metric and metric.get('enabled'))

    def update(self, data):
        """Update the configuration from a (API submitted) dictionary.

        Redacted secrets (********) or missing sensitive fields keep the
        previously stored value.
        """
        if not isinstance(data, dict):
            return

        if 'min_interval' in data:
            try:
                self._config['min_interval'] = max(0, int(_to_number(data['min_interval'])))
            except (ValueError, TypeError):
                pass

        channels = data.get('channels')
        if isinstance(channels, dict):
            for channel_name, channel_data in channels.items():
                if channel_name in self._config['channels'] and isinstance(channel_data, dict):
                    self._update_channel(self._config['channels'][channel_name], channel_data)

        thresholds = data.get('thresholds')
        if isinstance(thresholds, dict):
            self._update_thresholds(thresholds)

    def _update_channel(self, target, channel_data):
        """Merge one submitted channel configuration into target."""
        for field, value in channel_data.items():
            if field not in target:
                continue
            if field in _SENSITIVE_FIELDS:
                # Keep the stored secret if the field was not modified
                if value is not None and value != '' and value != REDACTED:
                    # Strip pasted whitespace/newlines: a trailing control
                    # character would make send errors leak the full URL
                    # (and its token) in the notification history
                    target[field] = str(value).strip()
            elif field == 'enabled' or field.endswith('_ssl') or field.endswith('_tls'):
                target[field] = _to_bool(value, default=target[field])
            elif field == 'recipients':
                target[field] = self._parse_recipients(value)
            elif field == 'smtp_port':
                try:
                    target[field] = int(_to_number(value))
                except (ValueError, TypeError):
                    pass
            else:
                target[field] = str(value).strip() if value is not None else ''

    def _update_thresholds(self, thresholds):
        """Merge submitted thresholds and mark them as runtime overrides."""
        for metric_name, metric_data in thresholds.items():
            if metric_name not in THRESHOLD_METRICS or not isinstance(metric_data, dict):
                continue
            # Explicitly submitted metrics become runtime overrides
            self._threshold_overrides.add(metric_name)
            target = self._config['thresholds'][metric_name]
            if 'enabled' in metric_data:
                target['enabled'] = _to_bool(metric_data['enabled'], default=target['enabled'])
            for level in ('careful', 'warning', 'critical'):
                if level in metric_data and metric_data[level] != '':
                    try:
                        target[level] = _to_number(metric_data[level])
                    except (ValueError, TypeError):
                        # Keep the raw value so validate() can reject it
                        target[level] = metric_data[level]

    @contextlib.contextmanager
    def temporary_update(self, data):
        """Context manager applying an update then restoring the previous state."""
        config_snapshot = copy.deepcopy(self._config)
        overrides_snapshot = set(self._threshold_overrides)
        try:
            self.update(data)
            yield
        finally:
            self._config = config_snapshot
            self._threshold_overrides = overrides_snapshot

    def check_update(self, data):
        """Validate an update without mutating the current state.

        Apply the update on a temporary state, run validate() and always
        roll back, so a rejected request never leaves partial data in the
        live configuration.
        """
        with self.temporary_update(data):
            return self.validate()

    @staticmethod
    def _parse_recipients(value):
        """Parse recipients from a list or a comma separated string."""
        if isinstance(value, (list, tuple)):
            items = value
        elif isinstance(value, str):
            items = value.split(',')
        else:
            return []
        return [str(item).strip() for item in items if str(item).strip()]

    def validate(self):
        """Validate the configuration.

        Return a list of human readable error strings (empty list = valid).
        """
        errors = []
        config = self._config

        try:
            if int(config['min_interval']) < 0:
                errors.append('min_interval 必须为非负整数')
        except (ValueError, TypeError):
            errors.append('min_interval 必须为非负整数')

        for metric_name, metric in config['thresholds'].items():
            label = THRESHOLD_METRICS.get(metric_name, (None, None, metric_name))[2]
            try:
                careful = float(metric['careful'])
                warning = float(metric['warning'])
                critical = float(metric['critical'])
            except (ValueError, TypeError, KeyError):
                errors.append(f'指标 {label} 的阈值必须为数字')
                continue
            if not (0 <= careful < warning < critical):
                errors.append(f'指标 {label} 的阈值需满足 0 ≤ careful < warning < critical')

        email = config['channels']['email']
        if email['enabled']:
            if not email['smtp_host']:
                errors.append('邮件渠道已启用但未配置 SMTP 服务器')
            if not email['sender']:
                errors.append('邮件渠道已启用但未配置发件人')
            if not email['recipients']:
                errors.append('邮件渠道已启用但未配置收件人')

        for name in ('dingtalk', 'feishu'):
            channel = config['channels'][name]
            if channel['enabled']:
                webhook = channel['webhook']
                if not webhook:
                    errors.append(f'{name} 渠道已启用但未配置 webhook')
                elif not webhook.startswith(('http://', 'https://')):
                    errors.append(f'{name} webhook 必须是合法的 http(s) URL')

        return errors
