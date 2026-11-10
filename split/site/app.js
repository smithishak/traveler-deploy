'use strict';

const cast = window.CINEMA_CAST;
const byId = new Map(cast.map(person => [person.slug, person]));
const element = id => document.getElementById(id);
const storageKey = 'kinolitsa:choices:v1';
const formatter = new Intl.NumberFormat('ru-RU');
const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
const sides = ['left', 'right'];
const cards = sides.map(side => element('card-' + side));
let choices = loadChoices();
let round = 0;
let deck = [];
let pair;
let changingPair = false;

function loadChoices() {
  try {
    const data = JSON.parse(localStorage.getItem(storageKey));
    if (!Array.isArray(data)) return [];
    return data.filter(choice => choice && byId.has(choice.winner) &&
      byId.has(choice.loser) && choice.winner !== choice.loser);
  } catch {
    return [];
  }
}

function saveChoices() {
  const warning = element('storage-warning');
  try {
    localStorage.setItem(storageKey, JSON.stringify(choices));
    warning.hidden = true;
  } catch {
    warning.textContent = 'Браузер запретил сохранение. Результаты доступны до закрытия страницы.';
    warning.hidden = false;
  }
}

function calculateRanking() {
  const stats = new Map(cast.map(person => [person.slug, { score: 1000, wins: 0, played: 0 }]));
  for (const choice of choices) {
    const winner = stats.get(choice.winner);
    const loser = stats.get(choice.loser);
    const expected = 1 / (1 + 10 ** ((loser.score - winner.score) / 400));
    const delta = 32 * (1 - expected);
    winner.score += delta;
    loser.score -= delta;
    winner.wins++;
    winner.played++;
    loser.played++;
  }
  return cast.map(person => ({ ...person, ...stats.get(person.slug) })).sort((a, b) =>
    Number(b.played > 0) - Number(a.played > 0) || b.score - a.score ||
    b.wins - a.wins || a.label.localeCompare(b.label, 'ru'));
}

function node(tag, className, value) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (value !== undefined) result.textContent = value;
  return result;
}

function renderRanking() {
  const list = element('ranking');
  const rows = calculateRanking().map((person, index) => {
    const row = node('li', person.played ? 'rank-row' : 'rank-row unplayed');
    const identity = node('div', 'rank-person');
    const position = node('span', 'rank-position' + (person.played ? ' ranked' : ''),
      person.played ? String(index + 1).padStart(2, '0') : '—');
    const photo = node('img', 'rank-photo');
    photo.src = photoPath(person);
    photo.alt = '';
    photo.loading = 'lazy';
    const name = node('div', 'rank-name', person.label);
    name.append(node('span', 'rank-english', person.name));
    identity.append(position, photo, name);
    const wins = node('span', 'rank-wins', formatter.format(person.wins));
    wins.append(node('small', '', 'из ' + formatter.format(person.played)));
    const score = node('span', 'rank-score', formatter.format(Math.round(person.score)));
    row.append(identity, wins, score);
    return row;
  });
  list.replaceChildren(...rows);
  const total = formatter.format(choices.length);
  element('nav-count').textContent = total;
  element('total-votes').textContent = total;
  element('vote-counter').textContent = 'Ваших выборов: ' + total;
  element('undo').disabled = choices.length === 0 || changingPair;
  element('reset').disabled = choices.length === 0 || changingPair;
}

function photoPath(person) {
  return 'assets/' + person.slug + '.jpg';
}

function pairKey(slugs) {
  return [...slugs].sort().join('|');
}

function refillDeck() {
  for (let first = 0; first < cast.length; first++) {
    for (let second = first + 1; second < cast.length; second++) {
      const duo = [cast[first].slug, cast[second].slug];
      deck.push(Math.random() < 0.5 ? duo : duo.reverse());
    }
  }
  for (let i = deck.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [deck[i], deck[j]] = [deck[j], deck[i]];
  }
  if (pair && pairKey(deck.at(-1)) === pairKey(pair)) {
    [deck[0], deck[deck.length - 1]] = [deck[deck.length - 1], deck[0]];
  }
}

function showPair(nextPair) {
  pair = nextPair;
  round++;
  element('round-number').textContent = String(round).padStart(2, '0');
  element('duel').classList.remove('has-choice');
  sides.forEach((side, index) => {
    const person = byId.get(pair[index]);
    const image = element('image-' + side);
    image.src = photoPath(person);
    image.alt = person.label + ' на публичном мероприятии';
    image.style.objectPosition = person.position || '';
    element('name-' + side).textContent = person.label;
    element('context-' + side).textContent = person.context;
    cards[index].setAttribute('aria-label', 'Выбрать портрет: ' + person.label);
    cards[index].classList.remove('chosen');
    cards[index].removeAttribute('aria-disabled');
  });
  const next = deck.at(-1);
  if (next) {
    for (const slug of next) {
      const preview = new Image();
      preview.src = photoPath(byId.get(slug));
    }
  }
}

function nextPair() {
  if (deck.length === 0) refillDeck();
  showPair(deck.pop());
}

function announce(message) {
  element('live-status').textContent = message;
}

function vote(index) {
  if (changingPair) return;
  changingPair = true;
  const winner = pair[index];
  const loser = pair[1 - index];
  choices.push({ winner, loser, pair: [...pair] });
  saveChoices();
  renderRanking();
  element('duel').classList.add('has-choice');
  element('skip').disabled = true;
  cards[index].classList.add('chosen');
  cards.forEach(card => card.setAttribute('aria-disabled', 'true'));
  announce('Ваш выбор — ' + byId.get(winner).label + '.');
  window.setTimeout(() => {
    nextPair();
    changingPair = false;
    element('skip').disabled = false;
    renderRanking();
  }, motion.matches ? 0 : 460);
}

function undo() {
  if (changingPair || !choices.length) return;
  const choice = choices.pop();
  const previousPair = Array.isArray(choice.pair) && choice.pair.length === 2 &&
    choice.pair.includes(choice.winner) && choice.pair.includes(choice.loser)
    ? choice.pair : [choice.winner, choice.loser];
  const previousKey = pairKey(previousPair);
  deck = deck.filter(candidate => pairKey(candidate) !== previousKey);
  if (pairKey(pair) !== previousKey) deck.push(pair);
  showPair(previousPair);
  saveChoices();
  renderRanking();
  announce('Последний выбор отменён. Можно выбрать снова.');
}

function setUpDialog(dialogId, openId) {
  const dialog = element(dialogId);
  element(openId).addEventListener('click', () => dialog.showModal());
  dialog.querySelectorAll('.dialog-close, .dialog-dismiss').forEach(button =>
    button.addEventListener('click', () => dialog.close()));
  return dialog;
}

function renderCredits() {
  const items = cast.map(person => {
    const row = node('div', 'credit-row');
    const photo = node('img');
    photo.src = photoPath(person);
    photo.alt = '';
    photo.loading = 'lazy';
    const details = node('div');
    const title = node('strong', '', person.label);
    const source = node('a', '', 'Wikimedia Commons');
    source.href = person.source;
    const license = node('a', '', person.license);
    license.href = person.licenseUrl;
    details.append(title, person.author + ' · ', source, ' · ', license);
    row.append(photo, details);
    return row;
  });
  element('credits-list').replaceChildren(...items);
}

cards.forEach((card, index) => card.addEventListener('click', () => vote(index)));
element('undo').addEventListener('click', undo);
element('skip').addEventListener('click', () => {
  if (changingPair) return;
  nextPair();
  announce('Новая пара. Выберите понравившийся портрет.');
});
document.addEventListener('keydown', event => {
  if (event.repeat || event.altKey || event.ctrlKey || event.metaKey ||
    document.querySelector('dialog[open]') ||
    !element('duel').getBoundingClientRect().height) return;
  const duel = element('duel').getBoundingClientRect();
  if (duel.bottom < 0 || duel.top > window.innerHeight) return;
  const index = { ArrowLeft: 0, ArrowRight: 1, '1': 0, '2': 1 }[event.key];
  if (index === undefined) return;
  event.preventDefault();
  vote(index);
});

setUpDialog('info-dialog', 'elo-info');
setUpDialog('credits-dialog', 'credits-open');
const resetDialog = setUpDialog('reset-dialog', 'reset');
element('reset-confirm').addEventListener('click', () => {
  choices = [];
  saveChoices();
  renderRanking();
  resetDialog.close();
  element('rating-title').setAttribute('tabindex', '-1');
  element('rating-title').focus();
  announce('Рейтинг сброшен.');
});

renderCredits();
renderRanking();
refillDeck();
const featured = ['emma-stone', 'scarlett-johansson'];
const featuredIndex = deck.findIndex(candidate => pairKey(candidate) === pairKey(featured));
deck.splice(featuredIndex, 1);
showPair(featured);
