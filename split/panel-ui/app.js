'use strict';

const byId = id => document.getElementById(id);
const panel = byId('panel');
const entries = byId('entries');
const heading = byId('heading');
const dialog = byId('dialog');
const issue = byId('issue');
const cover = byId('cover');
const coverPrint = cover.querySelector('.cover-print');
const initialUser = new URLSearchParams(location.search).get('user');
const dateFormat = new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric', month: 'long', year: 'numeric',
});
const timeFormat = new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  second: '2-digit', timeZoneName: 'short',
});
const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
const kinds = {
  phone: 'Телефон', laptop: 'Компьютер', tablet: 'Планшет', other: 'Устройство',
};

let state;
let selectedId = initialUser;
let listSignature;
let intent;
let busy = false;
let polling = false;
let revision = 0;
let introTimer;
let issuedDevice;
let routeCheckPending = false;
let lastRouteCheck;
let lastRouteError = '';
let pollFailed = false;
const deviceStats = new Map();
let priorActivity = new Map();

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(text, classes, listener) {
  const node = element('button', classes, text);
  node.type = 'button';
  node.addEventListener('click', listener);
  return node;
}

function link(text, classes, href) {
  const node = element('a', classes, text);
  node.href = href;
  return node;
}

function timestamp(value) {
  if (value == null || value === '') return null;
  const date = typeof value === 'number'
    ? new Date(value < 1e12 ? value * 1000 : value)
    : new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
}

function creationDate(value) {
  const date = timestamp(value);
  return date ? dateFormat.format(date) : 'Дата неизвестна';
}

function activityText(date) {
  if (!date) return 'Пока нет данных';
  const minutes = Math.max(0, Math.floor((Date.now() - date) / 60000));
  if (minutes < 1) return 'Только что';
  if (minutes < 60) return `${minutes} мин назад`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} ч назад`;
  return `${Math.floor(minutes / 1440)} дн. назад`;
}

function trafficText(value) {
  if (!Number.isFinite(value) || value < 0) return '—';
  const units = ['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'];
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${numberFormat.format(value)} ${units[unit]}`;
}

function deviceCount(count) {
  const tail = count % 100;
  const last = count % 10;
  if (tail < 11 || tail > 14) {
    if (last === 1) return `${count} устройство`;
    if (last >= 2 && last <= 4) return `${count} устройства`;
  }
  return `${count} устройств`;
}

function user() {
  return state?.users.find(item => String(item.id) === selectedId);
}

function navigate(id, focus = true) {
  selectedId = id == null ? null : String(id);
  const url = new URL(location.href);
  if (selectedId === null) url.searchParams.delete('user');
  else url.searchParams.set('user', selectedId);
  history.replaceState(null, '', url);
  render();
  if (focus) heading.focus();
}

function userTile(item) {
  const tile = button('', 'user-tile', () => navigate(item.id));
  const letter = element('span', 'user-letter', Array.from(item.name.trim())[0]?.toLocaleUpperCase('ru') || '•');
  letter.setAttribute('aria-hidden', 'true');
  const label = element('span', 'user-label');
  label.append(element('strong', null, item.name), element('small', null, deviceCount(item.devices.length)));
  tile.append(letter, label);
  return tile;
}

function fact(title, value, list) {
  const group = element('div');
  const output = element('dd', null, value);
  group.append(element('dt', null, title), output);
  list.append(group);
  return output;
}

function profileLink(path, device) {
  return `/${path}?${new URLSearchParams({ device: device.id })}`;
}

function deviceCard(item, device) {
  const kind = Object.hasOwn(kinds, device.kind) ? device.kind : 'other';
  const card = element('article', 'device');
  const art = element('figure', 'device-art');
  art.dataset.kind = kind;
  art.setAttribute('aria-hidden', 'true');
  if (kind === 'other') {
    art.append(element('span', null, '↗'));
  } else {
    const image = element('img');
    image.src = `/panel-ui/${kind}.webp`;
    image.alt = '';
    art.append(image);
  }
  const content = element('div', 'device-content');
  const name = element('div', 'card-head');
  name.append(element('h2', null, device.name), element('small', null, kinds[kind]));
  const facts = element('dl', 'facts');
  fact('Создано', creationDate(device.created_at), facts);
  const activityField = fact('Последняя активность', undefined, facts);
  const activity = element('span', 'activity-label', '—');
  const exactTime = element('time', 'reading-time');
  exactTime.hidden = true;
  activityField.append(activity, exactTime);
  const download = fact('Получено', '—', facts);
  const upload = fact('Отправлено', '—', facts);
  deviceStats.set(String(device.id), { activity, exactTime, download, upload });
  const actions = element('div', 'actions');
  actions.append(
    link('Скачать JSON', 'button', profileLink('profile', device)),
    link('Показать QR', 'button button-primary', profileLink('qr', device)),
  );
  if (!device.legacy) {
    const rotate = button('Перевыпустить', 'button button-quiet', () => {
      openDialog('rotate_device', item, device);
    });
    rotate.setAttribute('aria-label', `Перевыпустить профиль «${device.name}»`);
    const remove = button('Удалить', 'button button-quiet device-delete', () => {
      openDialog('delete_device', item, device);
    });
    remove.setAttribute('aria-label', `Удалить ${device.name}`);
    actions.append(rotate, remove);
  }
  content.append(name, facts, actions);
  card.append(art, content);
  return card;
}

function telemetryStatus() {
  const updated = timestamp(state.telemetry_updated_at);
  if (state.telemetry && !pollFailed) return { status: 'fresh', updated };
  if (updated) return { status: 'stale', updated };
  return { status: 'unavailable', updated: null };
}

function displayTime(node, date) {
  node.hidden = !date;
  node.textContent = date ? timeFormat.format(date) : '';
  if (node.tagName === 'TIME') node.dateTime = date ? date.toISOString() : '';
}

function refreshStats(item) {
  const telemetry = telemetryStatus();
  byId('telemetry-note').hidden = !item || telemetry.status === 'fresh';
  byId('telemetry-note').textContent = telemetry.status === 'stale'
    ? `Показана сохранённая статистика за ${timeFormat.format(telemetry.updated)}.`
    : 'Статистика активности недоступна.';
  if (!item) {
    priorActivity.clear();
    return;
  }
  const nextActivity = new Map();
  for (const device of item.devices) {
    const id = String(device.id);
    const stats = deviceStats.get(id);
    if (!stats) continue;
    const date = timestamp(device.last_seen);
    const firstTraffic = date && priorActivity.get(id) === null
      && telemetry.status === 'fresh' && !document.hidden;
    const label = date ? activityText(date)
      : telemetry.status === 'fresh' ? 'Трафика пока не было'
      : telemetry.status === 'stale' ? 'Нет сохранённых данных' : 'Недоступна';
    if (stats.activity.textContent !== label) stats.activity.textContent = label;
    displayTime(stats.exactTime, date);
    stats.activity.classList.toggle('recent', Boolean(
      date && telemetry.status === 'fresh' && Date.now() - date < 5 * 60 * 1000
    ));
    if (firstTraffic) stats.activity.classList.add('first-traffic');
    stats.upload.textContent = trafficText(device.upload);
    stats.download.textContent = trafficText(device.download);
    if (date || telemetry.status === 'fresh') nextActivity.set(id, date);
    else if (priorActivity.has(id)) nextActivity.set(id, priorActivity.get(id));
  }
  priorActivity = nextActivity;
}

function render() {
  const item = user();
  byId('loading').hidden = true;
  byId('screen').hidden = false;
  byId('settings').hidden = false;
  const signature = JSON.stringify(item
    ? [item.id, item.name, item.devices.map(d => [d.id, d.name, d.kind, d.created_at, d.legacy])]
    : state.users.map(u => [u.id, u.name, u.devices.length]));
  if (signature !== listSignature) {
    listSignature = signature;
    entries.replaceChildren();
    deviceStats.clear();
    entries.classList.toggle('entries-users', !item);
    heading.textContent = item ? item.name : 'Пользователи';
    byId('back').hidden = !item;
    byId('add').textContent = item ? '+ Устройство' : '+ Пользователь';
    if (item) {
      if (!item.devices.length) entries.append(element('p', 'empty', 'У этого пользователя пока нет устройств.'));
      for (const device of item.devices) entries.append(deviceCard(item, device));
      if (!item.devices.some(device => device.legacy)) {
        const tools = element('div', 'user-tools');
        tools.append(button('Удалить пользователя', 'button button-quiet', () => openDialog('delete_user', item)));
        entries.append(tools);
      }
    } else {
      if (!state.users.length) entries.append(element('p', 'empty', 'Добавьте пользователя, чтобы выдать профили.'));
      for (const entry of state.users) entries.append(userTile(entry));
    }
  }
  refreshStats(item);
  if (dialog.open && intent?.action === 'settings') updateStatsHealth();
  if (issue.open) updateIssue();
}

function showError(node, message) {
  node.textContent = message;
  node.hidden = !message;
}

class ConnectionError extends Error {}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { cache: 'no-store', ...options });
  } catch {
    throw new ConnectionError('Нет связи с панелью. На iPhone отключите Traveler в sing-box и заново запустите правило Local Forwarding в Termius.');
  }
  let result;
  try {
    result = await response.json();
  } catch {
    throw new ConnectionError('Панель получила неполный или неожиданный ответ сервера.');
  }
  if (!response.ok || result.error) {
    throw new Error(typeof result.error === 'string' ? result.error : 'Сервер не выполнил запрос.');
  }
  return result;
}

function showCover(focus = false) {
  panel.inert = true;
  cover.hidden = false;
  if (focus) byId('open').focus({ preventScroll: true });
}

function finishCover() {
  clearTimeout(introTimer);
  cover.hidden = true;
  cover.classList.remove('cover-playing');
  panel.inert = false;
  heading.focus({ preventScroll: true });
}

async function loadState(reportFailure = false) {
  if (polling || busy) return;
  polling = true;
  const initial = !state;
  const requestedAt = revision;
  try {
    const result = await request('/api/state');
    if (revision !== requestedAt) return;
    state = result.state ?? result;
    pollFailed = false;
    if (selectedId !== null && !user()) navigate(null, false);
    else render();
    showError(byId('error'), '');
    byId('retry').hidden = true;
    if (initial && state.settings.intro && !initialUser) showCover();
  } catch (error) {
    if (revision === requestedAt) {
      showError(byId('error'), error.message);
      byId('retry').hidden = false;
      if (initial) {
        byId('loading').hidden = true;
      } else {
        pollFailed = true;
        render();
      }
    }
    if (reportFailure) throw error;
  } finally {
    polling = false;
  }
}

function openDialog(action, item, device) {
  const create = action.startsWith('create_');
  const settings = action === 'settings';
  intent = { action, item, device };
  const titles = {
    create_user: 'Добавить пользователя',
    create_device: 'Добавить устройство',
    delete_user: 'Удалить пользователя?',
    delete_device: 'Удалить устройство?',
    rotate_device: 'Перевыпустить профиль?',
    settings: 'Настройки',
  };
  byId('dialog-title').textContent = titles[action];
  byId('name-field').hidden = !create;
  byId('name-input').required = create;
  byId('name-input').disabled = false;
  byId('name-input').value = '';
  byId('kind-field').hidden = action !== 'create_device';
  byId('kind').disabled = false;
  byId('kind').value = 'phone';
  byId('intro-field').hidden = !settings;
  byId('intro').disabled = false;
  byId('intro').checked = state.settings.intro;
  byId('replay').hidden = !settings;
  byId('stats-health').hidden = !settings;
  byId('refresh-stats').hidden = !settings;
  byId('diagnostics').hidden = !settings;
  if (settings) updateStatsHealth();
  intent.submitLabel = create ? 'Добавить' : settings ? 'Сохранить'
    : action === 'rotate_device' ? 'Перевыпустить' : 'Удалить';
  byId('submit').textContent = intent.submitLabel;
  byId('submit').hidden = false;
  const note = action === 'delete_user'
    ? `Профили пользователя «${item.name}» потеряют доступ к Traveler.`
    : action === 'delete_device' ? `Профиль «${device.name}» потеряет доступ к Traveler.`
    : action === 'rotate_device'
      ? `Старый профиль «${device.name}» сразу потеряет доступ. Установите новый профиль на этом устройстве.` : '';
  showError(byId('dialog-note'), note);
  showError(byId('dialog-error'), '');
  dialog.showModal();
  if (create) byId('name-input').focus();
  else byId('cancel').focus();
}

function completeAction(action, issuedId) {
  pollFailed = false;
  showError(byId('error'), '');
  byId('retry').hidden = true;
  dialog.close();
  if (selectedId !== null && !user()) navigate(null);
  else {
    render();
    if (action === 'delete_device') heading.focus();
  }
  if (issuedId != null && ['create_device', 'rotate_device'].includes(action)) {
    const nextDevice = user()?.devices.find(device => String(device.id) === String(issuedId));
    if (nextDevice) showIssue(nextDevice, action === 'rotate_device');
  }
}

async function checkActionOutcome() {
  const { payload, knownIds } = intent.uncertain;
  let result;
  try {
    result = await request('/api/state');
  } catch (error) {
    showError(byId('dialog-error'),
      `Результат операции пока неизвестен. ${error.message} Затем нажмите «Проверить результат».`);
    return;
  }
  state = result.state ?? result;
  pollFailed = false;
  showError(byId('error'), '');
  byId('retry').hidden = true;
  const item = state.users.find(user => user.id === payload.user_id);
  const device = item?.devices.find(device => device.id === payload.device_id);
  const match = entry => entry.name === payload.name && !knownIds.has(entry.id);
  let created;
  if (payload.action === 'create_device') {
    created = item?.devices.find(device => match(device) && device.kind === payload.kind);
  } else if (payload.action === 'create_user') {
    created = state.users.find(match);
  }
  const completed = {
    create_user: Boolean(created),
    create_device: Boolean(created),
    delete_device: !device,
    delete_user: !item,
    settings: state.settings.intro === payload.intro,
  };
  if (completed[payload.action]) {
    completeAction(payload.action, created?.id);
  } else if (payload.action === 'rotate_device') {
    byId('submit').hidden = true;
    showError(byId('dialog-error'),
      'Связь восстановлена. Чтобы получить действующий профиль, закройте окно и скачайте JSON с карточки устройства.');
    render();
  } else {
    intent.uncertain = undefined;
    byId('submit').textContent = intent.submitLabel;
    byId('name-input').disabled = false;
    byId('kind').disabled = false;
    byId('intro').disabled = false;
    showError(byId('dialog-error'), 'Изменение не найдено на сервере. Проверьте данные и повторите действие.');
    render();
  }
}

async function submitAction(event) {
  event.preventDefault();
  if (!intent || busy || byId('submit').hidden) return;
  const { action, item, device } = intent;
  const payload = { csrf: state.csrf, action };
  if (item) payload.user_id = item.id;
  if (device) payload.device_id = device.id;
  if (!intent.uncertain && action.startsWith('create_')) {
    payload.name = byId('name-input').value.trim();
    if (!payload.name) {
      showError(byId('dialog-error'), 'Введите название.');
      byId('name-input').focus();
      return;
    }
  }
  if (action === 'create_device') payload.kind = byId('kind').value;
  if (action === 'settings') payload.intro = byId('intro').checked;
  busy = true;
  revision += 1;
  byId('submit').disabled = true;
  byId('cancel').disabled = true;
  try {
    if (intent.uncertain) {
      await checkActionOutcome();
      return;
    }
    const entriesBefore = action === 'create_device' ? item.devices : state.users;
    const knownIds = new Set(entriesBefore.map(entry => entry.id));
    let result;
    try {
      result = await request('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    } catch (error) {
      if (!(error instanceof ConnectionError)) throw error;
      intent.uncertain = { payload, knownIds };
      byId('submit').textContent = 'Проверить результат';
      byId('name-input').disabled = true;
      byId('kind').disabled = true;
      byId('intro').disabled = true;
      showError(byId('dialog-error'), 'Проверяем результат операции…');
      await checkActionOutcome();
      return;
    }
    state = result.state ?? result;
    completeAction(action, result.created_device_id);
  } catch (error) {
    showError(byId('dialog-error'), error.message);
  } finally {
    busy = false;
    byId('submit').disabled = false;
    byId('cancel').disabled = false;
  }
}

function updateStatsHealth() {
  const { status, updated } = telemetryStatus();
  byId('stats-health').textContent = status === 'fresh'
    ? 'Сбор статистики работает.'
    : status === 'stale' ? `Последнее обновление статистики: ${timeFormat.format(updated)}.`
    : 'Статистика недоступна. Проверьте сборщик на сервере.';
}

function showIssue(device, rotated) {
  issuedDevice = { id: String(device.id), rotated, waiting: false };
  byId('issue-label').textContent = rotated ? 'Профиль перевыпущен' : 'Профиль выдан';
  byId('issue-title').textContent = device.name;
  byId('issue-json').href = profileLink('profile', device);
  byId('issue-qr').href = profileLink('qr', device);
  byId('issue-status').classList.remove('recent', 'first-traffic');
  updateIssue();
  issue.showModal();
  byId('issue-title').focus();
}

function updateIssue() {
  const device = user()?.devices.find(d => String(d.id) === issuedDevice?.id);
  const message = byId('issue-status');
  const { status, updated } = telemetryStatus();
  const date = timestamp(device?.last_seen);
  let text;
  let observed = false;
  if (!device) {
    text = 'Профиль больше не доступен.';
    byId('issue-json').hidden = true;
    byId('issue-qr').hidden = true;
  } else if (issuedDevice.rotated) {
    text = 'Импортируйте новый профиль, чтобы снова подключиться.';
  } else if (status === 'stale') {
    text = `Статистика обновлялась ${timeFormat.format(updated)}.`;
  } else if (status === 'unavailable') {
    text = 'Статистика активности недоступна.';
  } else if (date) {
    text = 'Первый трафик подтверждён';
    observed = true;
    if (issuedDevice.waiting && !document.hidden) message.classList.add('first-traffic');
    issuedDevice.waiting = false;
  } else {
    text = 'Ждём первый трафик после подключения.';
    issuedDevice.waiting = true;
  }
  if (device) {
    byId('issue-json').hidden = false;
    byId('issue-qr').hidden = false;
  }
  message.classList.toggle('recent', observed);
  if (message.textContent !== text) message.textContent = text;
  displayTime(byId('issue-time'), observed ? date : null);
}

function showRouteCheck() {
  const status = byId('diagnostics-status');
  const checks = byId('diagnostics-checks');
  checks.replaceChildren();
  byId('check-route').disabled = routeCheckPending;
  if (routeCheckPending) {
    showError(status, 'Проверяем маршрут. Это может занять несколько секунд.');
    return;
  }
  if (lastRouteError) {
    showError(status, lastRouteError);
    return;
  }
  if (!lastRouteCheck) {
    showError(status, '');
    return;
  }
  const date = timestamp(lastRouteCheck.checked_at);
  const where = lastRouteCheck.vantage || 'Проверка с сервера';
  showError(status, date ? `${where} · ${timeFormat.format(date)}` : where);
  const labels = { pass: 'Работает', fail: 'Ошибка', skipped: 'Пропущено' };
  for (const check of lastRouteCheck.checks) {
    const entry = element('li');
    const title = element('strong', null, check.label);
    const outcome = element('span', 'check-status', labels[check.status] || 'Неизвестно');
    outcome.dataset.status = check.status;
    entry.append(title, outcome);
    if (check.detail) entry.append(element('p', 'reading-time', check.detail));
    checks.append(entry);
  }
}

byId('retry').addEventListener('click', () => loadState());
byId('back').addEventListener('click', () => navigate(null));
byId('add').addEventListener('click', () => {
  const item = user();
  openDialog(item ? 'create_device' : 'create_user', item);
});
byId('settings').addEventListener('click', () => openDialog('settings'));
byId('check-route').addEventListener('click', async () => {
  if (routeCheckPending) return;
  routeCheckPending = true;
  lastRouteError = '';
  showRouteCheck();
  try {
    lastRouteCheck = await request('/api/diagnostics', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ csrf: state.csrf }),
    });
  } catch (error) {
    lastRouteError = error.message;
  } finally {
    routeCheckPending = false;
    showRouteCheck();
  }
});
byId('refresh-stats').addEventListener('click', async event => {
  const refresh = event.currentTarget;
  refresh.disabled = true;
  showError(byId('dialog-error'), '');
  try {
    await loadState(true);
    updateStatsHealth();
  } catch (error) {
    showError(byId('dialog-error'), error.message);
  } finally {
    refresh.disabled = false;
  }
});
byId('dialog-form').addEventListener('submit', submitAction);
byId('cancel').addEventListener('click', () => dialog.close());
dialog.addEventListener('cancel', event => {
  if (busy) event.preventDefault();
});
byId('issue-close').addEventListener('click', () => issue.close());
issue.addEventListener('close', () => {
  issuedDevice = undefined;
  heading.focus();
});
byId('replay').addEventListener('click', () => {
  dialog.close();
  showCover(true);
});
byId('open').addEventListener('click', () => {
  if (cover.classList.contains('cover-playing')) return;
  cover.classList.add('cover-playing');
  introTimer = setTimeout(finishCover, 2100);
});
coverPrint.addEventListener('animationend', event => {
  if (event.animationName === 'print-up') finishCover();
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) loadState();
});
setInterval(() => {
  if (!document.hidden) loadState();
}, 15000);
loadState();
