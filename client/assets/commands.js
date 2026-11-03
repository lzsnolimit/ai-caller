// 指令状态的人话。注入回执不等于 AI 已经说出或照做。
import { has, t } from './i18n.js';

export function commandLabel(command) {
  const pad = n => String(n).padStart(2, '0'), at = new Date(command.updatedAtMs);
  const time = Number.isFinite(command.updatedAtMs) ? ` · ${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}` : '';
  const status = has('command.' + command.status) ? t('command.' + command.status) : t('command.unknownStatus');
  return `${status}${time}${command.reason ? ` · ${command.reason}` : ''}${t('command.note')}`;
}
