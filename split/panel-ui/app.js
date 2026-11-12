'use strict';

const byId = id => document.getElementById(id);
const panel = byId('panel');
const entries = byId('entries');
const heading = byId('heading');
const dialog = byId('dialog');
const cover = byId('cover');
const coverPrint = cover.querySelector('.cover-print');
const initialUser = new URLSearchParams(location.search).get('user');
const dateFormat = new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric', month: 'long', year: 'numeric',
});
const timeFormat = new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
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
const deviceStats = new Map();

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
  return timeFormat.format(date);
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
  const activity = fact('Последняя активность', '—', facts);
  const download = fact('Получено', '—', facts);
  const upload = fact('Отправлено', '—', facts);
  deviceStats.set(String(device.id), { activity, download, upload });
  const actions = element('div', 'actions');
  const query = new URLSearchParams({ device: device.id });
  actions.append(
    link('Скачать JSON', 'button', `/profile?${query}`),
    link('Показать QR', 'button button-primary', `/qr?${query}`),
  );
  if (!device.legacy) {
    const remove = button('Удалить', 'button button-quiet device-delete', () => {
      openDialog('delete_device', item, device);
    });
    remove.setAttribute('aria-label', `Удалить ${device.name}`);
    actions.append(remove);
  }
  content.append(name, facts, actions);
  card.append(art, content);
  return card;
}

function refreshStats(item) {
  if (!item) return;
  for (const device of item.devices) {
    const stats = deviceStats.get(String(device.id));
    if (!stats) continue;
    const date = timestamp(device.last_seen);
    stats.activity.textContent = date ? activityText(date) : state.telemetry ? 'Пока нет данных' : 'Недоступна';
    stats.activity.title = date ? timeFormat.format(date) : '';
    stats.activity.classList.toggle('recent', Boolean(date && Date.now() - date < 5 * 60 * 1000));
    stats.upload.textContent = trafficText(device.upload);
    stats.download.textContent = trafficText(device.download);
  }
}

function render() {
  const item = user();
  byId('loading').hidden = true;
  byId('screen').hidden = false;
  byId('settings').hidden = false;
  const signature = JSON.stringify(item
    ? [item.id, item.name, state.telemetry, item.devices.map(d => [d.id, d.name, d.kind, d.created_at, d.legacy])]
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
      if (!state.telemetry) entries.append(element('p', 'telemetry-note', 'Сервер не передаёт статистику активности.'));
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
}

function showError(node, message) {
  node.textContent = message;
  node.hidden = !message;
}

async function request(path, options = {}) {
  const response = await fetch(path, { cache: 'no-store', ...options });
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error('Панель получила неожиданный ответ сервера.');
  }
  if (!response.ok || result.error) {
    throw new Error(typeof result.error === 'string' ? result.error : 'Сервер не выполнил запрос.');
  }
  return result.state ?? result;
}

function showCover() {
  panel.inert = true;
  cover.hidden = false;
  byId('open').focus({ preventScroll: true });
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
    const next = await request('/api/state');
    if (revision !== requestedAt) return;
    state = next;
    if (selectedId !== null && !user()) navigate(null, false);
    else render();
    showError(byId('error'), '');
    byId('retry').hidden = true;
    if (initial && state.settings.intro && !initialUser) showCover();
  } catch (error) {
    if (initial) {
      byId('loading').hidden = true;
      showError(byId('error'), error.message);
      byId('retry').hidden = false;
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
    settings: 'Настройки',
  };
  byId('dialog-title').textContent = titles[action];
  byId('name-field').hidden = !create;
  byId('name-input').required = create;
  byId('name-input').value = '';
  byId('kind-field').hidden = action !== 'create_device';
  byId('kind').value = 'phone';
  byId('intro-field').hidden = !settings;
  byId('intro').checked = state.settings.intro;
  byId('replay').hidden = !settings;
  byId('stats-health').hidden = !settings;
  byId('refresh-stats').hidden = !settings;
  if (settings) updateStatsHealth();
  byId('submit').textContent = create ? 'Добавить' : settings ? 'Сохранить' : 'Удалить';
  const note = action === 'delete_user'
    ? `Профили пользователя «${item.name}» потеряют доступ к Traveler.`
    : action === 'delete_device' ? `Профиль «${device.name}» потеряет доступ к Traveler.` : '';
  showError(byId('dialog-note'), note);
  showError(byId('dialog-error'), '');
  dialog.showModal();
  if (create) byId('name-input').focus();
  else byId('cancel').focus();
}

async function submitAction(event) {
  event.preventDefault();
  if (!intent || busy) return;
  const { action, item, device } = intent;
  const payload = { csrf: state.csrf, action };
  if (item) payload.user_id = item.id;
  if (device) payload.device_id = device.id;
  if (action.startsWith('create_')) {
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
    state = await request('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    dialog.close();
    if (selectedId !== null && !user()) navigate(null);
    else {
      render();
      if (action === 'delete_device') heading.focus();
    }
  } catch (error) {
    showError(byId('dialog-error'), error.message);
  } finally {
    busy = false;
    byId('submit').disabled = false;
    byId('cancel').disabled = false;
  }
}

function updateStatsHealth() {
  byId('stats-health').textContent = state.telemetry
    ? 'Сбор статистики работает.'
    : 'Статистика недоступна. Проверьте сборщик на сервере.';
}

byId('retry').addEventListener('click', () => loadState());
byId('back').addEventListener('click', () => navigate(null));
byId('add').addEventListener('click', () => {
  const item = user();
  openDialog(item ? 'create_device' : 'create_user', item);
});
byId('settings').addEventListener('click', () => openDialog('settings'));
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
byId('replay').addEventListener('click', () => {
  dialog.close();
  showCover();
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
