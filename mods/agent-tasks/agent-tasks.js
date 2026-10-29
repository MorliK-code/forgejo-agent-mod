(function () {
'use strict';
/* Мод «Задачи».

   1) /issues (и /<user>/inbox/issues) — кнопка «Создать задачу», которая открывает
      РОДНОЕ окно создания задачи Forgejo в inbox-репо пользователя. Автор = тот,
      кто создал, задача видна на /issues во вкладке «созданные».

   2) /<owner>/<repo>/issues/new в ЛЮБОМ репо — в родное окно создания задачи
      встраиваются наши поля: «использовать агента», «форматировать», приоритет,
      дедлайн (рядом с выбором модели из мода «Агент»). Форму и её кнопку не
      трогаем — отправка обычная.

   3) /<owner>/<repo>/issues/<n> — сразу после создания применяем выбранные
      приоритет/дедлайн/метку через бэкенд (у браузера нет API-токена); на самой
      задаче показываем панель правки (тот же набор полей), чтобы поменять их
      позже. Панель отправляет только то, что реально изменили — существующие
      метки/дедлайн не затираются. */

var API = '/agent-admin/api/tasks';
var STASH = 'at-pending';
var STASH_TTL_MS = 180000;
var MARK = 'Форматированный:';
var FMT_CIDS = [];  // id отформатированных комментариев этой задачи (с сервера)

function getJSON(url) {
  return fetch(url, {credentials: 'same-origin', headers: {'Accept': 'application/json'}})
    .then(function (r) { return r.ok ? r.json() : null; })
    .catch(function () { return null; });
}

function signedUser() {
  var strong = document.querySelector('details.dropdown .content .header strong');
  if (strong && strong.textContent.trim()) return strong.textContent.trim();
  var links = document.querySelectorAll('details.dropdown .content ul a[href]');
  for (var i = 0; i < links.length; i++) {
    var m = (links[i].getAttribute('href') || '').match(/^\/([^/?]+)\/?$/);
    if (m && ['user','issues','pulls','notifications','explore','admin','assets','__mods__'].indexOf(m[1]) < 0) {
      return m[1];
    }
  }
  return null;
}

function prioOptions(cfg, sel) {
  return '<option value=""' + (sel ? '' : ' selected') + '>нет</option>' +
    (cfg.priorities || []).map(function (p) {
      return '<option value="' + p + '"' + (p === sel ? ' selected' : '') + '>' + p + '</option>';
    }).join('');
}

// opts: {priority, agent, allowFormat} — панель правки: без формата; форма создания: allowFormat
// (формат описания — при создании и в окне правки тела; форматирование сообщений живёт у комментариев)
function controlsHtml(cfg, opts) {
  opts = opts || {};
  var fmt = opts.allowFormat
    ? '<label class="at-ctl"><input class="at-format" type="checkbox"> Форматировать описание</label>'
    : '';
  return '<label class="at-ctl"><input class="at-agent" type="checkbox"' + (opts.agent ? ' checked' : '') + '> Использовать агента</label>' +
         fmt +
         '<label class="at-ctl"><select class="at-prio" title="Приоритет: 5 — самый важный, 1 — выполнять последним">' + prioOptions(cfg, opts.priority) + '</select></label>';
}

function readControls(box) {
  var f = box.querySelector('.at-format');
  return {
    priority: box.querySelector('.at-prio').value,
    agent: box.querySelector('.at-agent').checked,
    format: !!(f && f.checked)
  };
}

function tagTask(repo, number, extra) {
  var body = {repo: repo, number: number,
              priority: extra.priority, agent: extra.agent};
  if (extra.pause !== undefined) body.pause = extra.pause;
  return fetch(API + '/tag', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  }).then(function (r) {
    return r.text().then(function (t) {
      var d;
      try { d = JSON.parse(t); }
      catch (e) {
        throw new Error(r.status === 401 ? 'сессия истекла — обновите страницу'
                                         : 'панель недоступна (' + r.status + ')');
      }
      if (d && d.error) throw new Error(d.error);
      return d;
    });
  });
}

/* ---------- 1. кнопка «Создать задачу» на странице задач ---------- */
function buildCreateButton(repo) {
  if (document.querySelector('.at-new')) return;
  var anchor = document.querySelector('.list-header-issues') || document.querySelector('#issue-list');
  if (!anchor || !anchor.parentNode) return;
  var a = document.createElement('a');
  a.className = 'at-new primary button';
  a.href = '/' + repo + '/issues/new';
  a.textContent = 'Создать задачу';
  anchor.parentNode.insertBefore(a, anchor.nextSibling);
}

/* ---------- 2. наши поля внутри родного окна создания задачи ---------- */
function injectIntoNewForm(cfg, repo) {
  var form = document.getElementById('new-issue') ||
             document.querySelector('form.issue-content, form[action$="/issues/new"]');
  if (!form || document.querySelector('.at-form')) return;

  var box = document.createElement('div');
  box.className = 'at-form';
  box.innerHTML = controlsHtml(cfg, {allowFormat: true});

  // рядом с блоком выбора модели (мод «Агент»), если он есть; иначе под заголовком
  var agentBlock = form.querySelector('.agent-issue-model');
  if (agentBlock && agentBlock.parentNode) {
    agentBlock.parentNode.insertBefore(box, agentBlock.nextSibling);
  } else {
    var title = form.querySelector('input[name="title"]');
    var anchor = title ? (title.closest('.field') || title) : form;
    if (anchor.parentNode) anchor.parentNode.insertBefore(box, anchor.nextSibling);
    else form.appendChild(box);
  }

  // capture: раньше, чем Forgejo соберёт FormData — саму отправку не трогаем
  form.addEventListener('submit', function () {
    var extra = readControls(box);
    extra.repo = repo;
    extra.ts = Date.now();
    try { sessionStorage.setItem(STASH, JSON.stringify(extra)); } catch (e) {}
  }, true);
}

/* ---------- 3a. stash выбора со страницы создания (применяется в панели) ---------- */
function getPending(repo, number) {
  var raw;
  try { raw = sessionStorage.getItem(STASH); } catch (e) { return null; }
  if (!raw) return null;
  var p;
  try { p = JSON.parse(raw); } catch (e) { return null; }
  if (String(p.repo).toLowerCase() !== repo.toLowerCase() || p.number !== number ||
      !p.ts || (Date.now() - p.ts) > STASH_TTL_MS) return null;
  return p;
}

/* ---------- 3b. панель правки задачи: текущие метки -> контролы -> tag_task ---------- */
function currentLabels() {
  var out = {}, nodes = document.querySelectorAll('.labels a, .labels .label, a.ui.label, .label-text');
  for (var i = 0; i < nodes.length; i++) {
    var t = (nodes[i].textContent || '').trim();
    if (t) out[t] = true;
  }
  return Object.keys(out);
}

function seedFromPage(cfg) {
  var labels = currentLabels();
  // приоритет = наивысший из найденных (5 важнее 1), а не последний в списке
  var sel = '';
  (cfg.priorities || []).forEach(function (p) { if (labels.indexOf(p) >= 0 && +p > +sel) sel = p; });
  return {priority: sel, agent: labels.indexOf('agent') >= 0, pause: labels.indexOf('pause') >= 0};
}

// DOM-селекторы меток на странице задачи врут (редактор меток показывает весь
// список репо) — сид берём из API, DOM только как fallback
function seedFromLabels(cfg, apiLabels) {
  if (!apiLabels) return seedFromPage(cfg);
  var sel = '';
  (cfg.priorities || []).forEach(function (p) { if (apiLabels.indexOf(p) >= 0 && +p > +sel) sel = p; });
  return {priority: sel, agent: apiLabels.indexOf('agent') >= 0, pause: apiLabels.indexOf('pause') >= 0};
}

/* лёгкая замена перезагрузке страницы: ответ /tag уже содержит итоговые метки —
   перекрашиваем сайдбар Forgejo (видимые чипы и чекбокс-редактор) под них.
   false — если сетки меток на странице нет (вызывающий даёт текстовый fallback) */
function syncLabels(labels) {
  var chips = document.querySelectorAll('.ui.labels.list a[id^="label_"]');
  var editor = document.querySelectorAll('a.item[data-id-selector]');
  if (!chips.length && !editor.length) return false;
  if (!labels) return true;  // сервер ответил без списка — сетку не трогаем, но она на месте
  var want = {};
  for (var i = 0; i < labels.length; i++) want[('' + labels[i]).trim().toLowerCase()] = true;
  var shown = 0;
  for (var j = 0; j < chips.length; j++) {
    var on = !!want[chipName(chips[j])];
    chips[j].classList.toggle('tw-hidden', !on);
    if (on) shown++;
  }
  var nl = document.querySelector('.ui.labels.list .no-select');
  if (nl) nl.classList.toggle('tw-hidden', shown > 0);
  for (var k = 0; k < editor.length; k++) {
    var on2 = !!want[chipName(editor[k])];
    editor[k].classList.toggle('checked', on2);
    var ic = editor[k].querySelector('.octicon-check');
    if (ic) ic.classList.toggle('tw-invisible', !on2);
  }
  return true;
}
function chipName(a) {
  var c = a.querySelector('.ui.label') || a;
  return (c.textContent || '').replace(/\u00a0/g, ' ').trim().toLowerCase();
}

function injectEditPanel(cfg, repo, number, pend, apiLabels) {
  if (document.querySelector('.at-edit')) return;
  var seed = pend ? {priority: pend.priority || '', agent: !!pend.agent} : seedFromLabels(cfg, apiLabels);
  var box = document.createElement('div');
  box.className = 'at-edit';
  box.innerHTML = controlsHtml(cfg, seed) +
    '<div class="at-row"><button class="at-taskpause ui icon button" type="button" title="Пауза этой задачи"></button>' +
    '<button class="at-apply primary mini button" type="button">Применить</button>' +
    '<span class="at-edit-msg"></span></div>';

  // в сайдбар рядом с метками; иначе — под заголовком задачи
  var anchor = document.querySelector('.labels, .labels-container, .ui.labels') ||
               (document.querySelector('h1') && document.querySelector('h1').closest('div'));
  if (!anchor || !anchor.parentNode) return;
  anchor.parentNode.insertBefore(box, anchor.nextSibling);

  var msg = box.querySelector('.at-edit-msg');

  // пауза конкретной задачи: та же кнопка, что и в списке; метка='действие' —
  // бэкенд при pause=true сам рвёт живой прогон этой задачи
  var tp = box.querySelector('.at-taskpause');
  setPauseLabel(tp, !!seed.pause);
  tp.addEventListener('click', function () {
    var next = !seed.pause;
    msg.textContent = '…';
    tagTask(repo, number, {pause: next}).then(function (d) {
      seed.pause = next;
      setPauseLabel(tp, next);
      msg.textContent = syncLabels(d && d.labels) ? '' : 'применено';
    }, function (e) { msg.textContent = 'не применено: ' + (e.message || 'сбой'); });
  });

  // выбор со страницы создания: применяем здесь; при сбое stash живёт до TTL —
  // повторится при следующем открытии задачи, «Применить» работает сразу
  if (pend) {
    tagTask(repo, number, pend).then(function (d) {
      try { sessionStorage.removeItem(STASH); } catch (e) {}
      syncLabels(d && d.labels);  // нативная форма создала задачу без меток — показать серверный итог сразу
      if (pend.format) {
        fetch(API + '/fmt_body', {
          method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({repo: repo, number: number})
        }).catch(function () {});
        msg.textContent = 'применено; запрошено форматирование описания';
      } else {
        msg.textContent = 'применено к созданной задаче';
      }
    }, function (e) {
      msg.textContent = 'не применено: ' + (e.message || 'сбой') + ' — нажмите «Применить»';
    });
  }
  box.querySelector('.at-apply').addEventListener('click', function () {
    var cur = readControls(box);
    var extra = {};                       // отправляем только изменения
    if (cur.priority !== seed.priority) extra.priority = cur.priority;
    if (cur.agent !== seed.agent) extra.agent = cur.agent;
    if (!('priority' in extra) && !('agent' in extra)) {
      msg.textContent = 'без изменений'; return;
    }
    msg.textContent = '…';
    tagTask(repo, number, extra).then(function (d) {
      msg.textContent = syncLabels(d && d.labels) ? '' : 'применено';  // сайдбар обновлён — текст не нужен; fallback если сетки нет
      seed.priority = cur.priority; seed.agent = cur.agent;  // след. правка сверяется с новым состоянием
    }, function (e) { msg.textContent = 'не применено: ' + (e.message || 'сбой'); });
  });
}

/* ---------- 3c. «Форматировать» у каждого окна комментария (новое + правка) ---------- */
var STASHF = 'at-fmtc';

// «Память чата» галки (#14, вариант А): после первого использования форматирования
// в комментариях этой задачи галка предзаполнена (включена) в новых окнах комментов.
// Сохранение нового комментария без галки = «не хочу» — предложение выключается.
// После 3-го нового комментария: запоминаем, но не предлагаем. Хранится в localStorage
// этого браузера, на задачу. shortcut: память локальная в браузере; при переходе между
// машинами/профилями — сброс, апгрейд = ключ на сервере через agent-admin.
function fmtMemKey(repo, number) { return 'at-fmtmem-' + String(repo).toLowerCase() + '-' + number; }
function fmtMem(repo, number) {
  try { return JSON.parse(localStorage.getItem(fmtMemKey(repo, number))) || {}; } catch (e) { return {}; }
}
function fmtMemSet(repo, number, m) {
  try { localStorage.setItem(fmtMemKey(repo, number), JSON.stringify(m)); } catch (e) {}
}
function fmtPropose(repo, number) {
  var m = fmtMem(repo, number);
  return !!(m.used && !m.off && (m.count || 0) < 3);
}

function commentFormInfo(f) {
  // новое окно: action .../issues/N/comments; правка: зона с data-update-url .../comments/<id>
  var a = f.getAttribute('action') || '';
  var m = a.match(/\/comments\/(\d+)/) || a.match(/\/comments\/?$/);
  var cid = null;
  if (a.indexOf('/comments') > -1) {
    cid = m && m[1] ? Number(m[1]) : null;
    return {cid: cid, isNew: !m || !m[1]};
  }
  var zone = f.closest('[data-update-url]');
  if (zone) {
    var u = zone.getAttribute('data-update-url') || '';
    var mu = u.match(/\/comments\/(\d+)/);
    if (mu) return {cid: Number(mu[1]), isNew: false};
    return null; // правка тела задачи (/content) — не трогать
  }
  return null;
}

// цель fmt-галки для save-edit: комментарий -> {cid}; тело задачи (zone /content без /comments/) -> {body}; иначе null
function fmtEditTarget(btn) {
  const zone = btn.closest('[data-update-url]');
  if (zone) {
    const u = zone.getAttribute('data-update-url') || '';
    const mu = u.match(/\/comments\/(\d+)/);
    if (mu) return {cid: Number(mu[1])};
    if (/\/content$/.test(u)) return {body: true};
  }
  const a = btn.closest('[id^="issuecomment-"]');
  if (a) { const n = Number(a.id.replace('issuecomment-', '')); if (n) return {cid: n}; }
  return null;
}

// точное вертикальное центрирование галки между зоной загрузки и кнопками:
// компенсируем чужие отступы/padding — равные margin в CSS не дают «по середине»,
// когда у соседей свои зазоры
function centerCfmtRow(row, topEl, bottomEl) {
  if (!row || !topEl || !bottomEl) return;
  var tries = 0;
  var apply = function () {
    var rr = row.getBoundingClientRect(), tr = topEl.getBoundingClientRect(), br = bottomEl.getBoundingClientRect();
    var above = rr.top - tr.bottom, below = br.top - rr.bottom;
    if (above < 0 || below < 0) return;
    var shift = below - above;  // нижний зазор фиксирован соседом — доводим одним кадром
    if (Math.abs(shift) < 2 || tries++ > 3) return;
    var cur = parseFloat(getComputedStyle(row).marginTop) || 0;
    row.style.marginTop = Math.max(-20, cur + shift) + "px";
    requestAnimationFrame(apply);
  };
  requestAnimationFrame(apply);
}

function dropzoneOf(el, form) {
  if (form) { var d = form.querySelector('.dropzone'); if (d) return d; }
  var p = el;
  while (p && p.querySelector) {
    var d2 = p.querySelector('.dropzone');
    if (d2) return d2;
    p = p.parentElement;
  }
  return null;
}

function injectCommentFormat(repo, number) {
  [...document.querySelectorAll('form')].forEach(function (f) {
    if (f.querySelector('.at-cfmt-row') || !f.querySelector('textarea')) return;
    var info = commentFormInfo(f);
    if (!info) return;
    var seq = f.querySelector('.button-sequence') ||
              (function () {
                var b = [...f.querySelectorAll('button')].find(function (x) { return x.type === 'submit'; });
                return b ? b.parentElement : null;
              })();
    var row = document.createElement('div');
    row.className = 'at-cfmt-row';
    row.innerHTML = '<input type="checkbox" class="at-cfmt-box"' +
                    (info.isNew && fmtPropose(repo, number) ? ' checked' : '') + '><span>Форматировать</span>';
    if (seq) seq.insertAdjacentElement('beforebegin', row);
    else f.appendChild(row);
    centerCfmtRow(row, dropzoneOf(f, f), seq || f);
    f.addEventListener('submit', function () {
      var cb = f.querySelector('.at-cfmt-box');
      var on = !!(cb && cb.checked);
      var mem = fmtMem(repo, number);
      if (on) { mem.used = true; mem.off = false; }
      if (info.isNew) { mem.count = (mem.count || 0) + 1; if (!on) mem.off = true; }
      fmtMemSet(repo, number, mem);
      if (!on) return;
      var payload = {repo: repo, number: number, cid: info.cid, ts: Date.now()};
      try { sessionStorage.setItem(STASHF, JSON.stringify(payload)); } catch (e) {}
      setTimeout(function () {
        fetch(API + '/fmt_comment', {
          method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({repo: repo, number: number, cid: payload.cid})
        }).catch(function () {});
      }, 2000);
    }, true);
  });
  // правка комментария строится из <template> без <form>: цепляемся за Save-кнопку
  [...document.querySelectorAll('button[data-button-name="save-edit"]')].forEach(function (btn) {
    if (btn.__atcf) return;
    var t = fmtEditTarget(btn);
    if (!t) return;
    btn.__atcf = true;
    var ta = (btn.closest('form') || btn.closest('.ui.comment.form') || btn.closest('.ui.form') || btn).querySelector('textarea') || {value: ''};
    var box = document.createElement('input');
    box.type = 'checkbox'; box.className = 'at-cfmt-box'; box.checked = false;
    var lbl = document.createElement('span'); lbl.textContent = 'Форматировать';
    var row = document.createElement('div');
    row.className = 'at-cfmt-row'; row.appendChild(box); row.appendChild(lbl);
    var seq = btn.parentElement;
    if (seq) seq.insertAdjacentElement('beforebegin', row);
    centerCfmtRow(row, dropzoneOf(btn, btn.closest('form') || btn.closest('.ui.form')), seq);
    // «Переформатировать» — только там, где форматирование уже есть:
    // тело — MARK в тексте; комментарий — его id в списке отформатированных (сервер).
    var showRb = t.body ? ta.value.indexOf(MARK) !== -1 : FMT_CIDS.indexOf(t.cid) !== -1;
    if (showRb) {
      var rb = document.createElement('button');
      rb.type = 'button'; rb.className = 'ui basic button at-cfmt-btn'; rb.textContent = 'Переформатировать';
      row.appendChild(rb);
      rb.addEventListener('click', function () {
        var url = t.body ? API + '/reformat_body' : API + '/fmt_comment';
        var payload = t.body ? {repo: repo, number: number}
                             : {repo: repo, number: number, cid: t.cid};
        if (!t.body && FMT_CIDS.indexOf(t.cid) === -1) FMT_CIDS.push(t.cid);
        fetch(url, {
          method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload)
        }).catch(function () {});
      });
    }
    btn.addEventListener('click', function () {
      if (!box.checked) return;
      if (t.cid) {
        var mem = fmtMem(repo, number); mem.used = true; mem.off = false; fmtMemSet(repo, number, mem);
        if (FMT_CIDS.indexOf(t.cid) === -1) FMT_CIDS.push(t.cid);
      }
      // тело с готовым MARK-блоком: галка снимает форматирование (возврат оригинала)
      var hasMark = t.body && ta.value.indexOf(MARK) !== -1;
      var url = t.body ? (hasMark ? API + '/restore_body' : API + '/fmt_body')
                       : API + '/fmt_comment';
      var payload = t.body ? {repo: repo, number: number}
                           : {repo: repo, number: number, cid: t.cid};
      setTimeout(function () {
        fetch(url, {
          method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload)
        }).catch(function () {});
      }, 2000);
    }, true);
  });
  // формы правки создаются динамически — досеиваем наблюдателем
  if (!document.__atcfObs) {
    document.__atcfObs = true;
    new MutationObserver(function () { injectCommentFormat(repo, number); })
      .observe(document.body, {childList: true, subtree: true});
  }
}

function flushFmtComment(repo, number) {
  var raw;
  try { raw = sessionStorage.getItem(STASHF); } catch (e) { return; }
  if (!raw) return;
  var p;
  try { p = JSON.parse(raw); } catch (e) { return; }
  if (String(p.repo).toLowerCase() !== repo.toLowerCase() || p.number !== number ||
      !p.ts || (Date.now() - p.ts) > STASH_TTL_MS) return;
  try { sessionStorage.removeItem(STASHF); } catch (e) {}
  fetch(API + '/fmt_comment', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({repo: repo, number: number, cid: p.cid || null})
  }).catch(function () {});
}

/* ---------- 4. пауза/возобновление всей очереди (вместо FAQ у поиска) ---------- */
var PAUSE_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true">' +
  '<rect x="3.9" y="2.8" width="2.9" height="10.4" rx="1.4" fill="currentColor"></rect>' +
  '<rect x="9.2" y="2.8" width="2.9" height="10.4" rx="1.4" fill="currentColor"></rect></svg>';
var PLAY_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true">' +
  '<path d="M5.2 3 12.4 8 5.2 13Z" fill="currentColor" stroke="currentColor" ' +
  'stroke-width="1.6" stroke-linejoin="round"></path></svg>';

function setPauseLabel(b, paused) {
  b.innerHTML = paused ? PLAY_ICON : PAUSE_ICON;
  var tip = paused ? 'Очередь на паузе — Возобновить' : 'Поставить очередь на паузу';
  b.title = tip;                       // Forgejo-тултип съедает title -> дублируем attrs
  b.setAttribute('data-tooltip-content', tip);
  b.setAttribute('aria-label', tip);
  b.classList.toggle('at-paused', !!paused);
}

function buildPauseButton(cfg) {
  if (document.querySelector('.at-pauseall')) return;
  var search = document.querySelector('.list-header-search') || document.querySelector('.list-header');
  if (!search || !search.parentNode) return;
  var b = document.createElement('button');
  b.type = 'button';
  b.className = 'at-pauseall ui icon button';
  setPauseLabel(b, !!cfg.paused);
  b.addEventListener('click', function () {
    var next = !b.classList.contains('at-paused');
    fetch(API.replace('/tasks', '') + '/overlay', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({forgejo: {paused: next}})
    }).then(function (r) {
      if (!r.ok) throw new Error(r.status === 401 ? 'сессия истекла' : 'HTTP ' + r.status);
      return r.json();
    }).then(function (d) {
      if (!d || d.error) throw new Error((d && d.error) || 'нет ответа');
      cfg.paused = next;
      setPauseLabel(b, next);
    }).catch(function (e) {
      // раньше ошибка молчала: кнопка «работала», а пауза — нет
      window.alert('Пауза очереди: ' + (e.message || e));
    });
  });
  var faq = document.querySelector('a.show-modal[data-modal="#search-syntax-modal"]');
  if (faq) faq.replaceWith(b);
  else search.parentNode.insertBefore(b, search.nextSibling);
}

/* ---------- 5. лента комментов: скрыть события смены меток (#feed) ----------
   Forgejo рендерит смену метки строкой .timeline-item.event (текст НЕ в .desc;
   чипы меток — span.labels-list). В ленте заказчику они не нужны: метки видно
   в блоке меток сверху задачи. Комменты (.timeline-item.comment) не трогаем. */
var LABEL_EVENT_RE = /(added|removed|changed)\s+(the\s+)?labels?|added\s+label|removed\s+label|добавил[а]?[^.]{0,60}метк|убрал[а]?[^.]{0,60}метк|изменил[а]?[^.]{0,60}метк/i;

function hideLabelEvents() {
  var n = 0;
  document.querySelectorAll('.timeline-item.event').forEach(function (row) {
    var t = (row.textContent || '').replace(/\s+/g, ' ').trim();
    if (row.querySelector('.labels-list') || LABEL_EVENT_RE.test(t)) {
      row.style.display = 'none';
      n++;
    }
  });
  return n;
}

/* ---------- init ---------- */
getJSON(API + '/config').then(function (cfg) {
  if (!cfg) return;
  var user = signedUser();
  var inbox = user ? user + '/' + (cfg.inbox_name || 'inbox') : null;
  var path = location.pathname;

  if (path === '/issues' || path === '/issues/') {
    buildPauseButton(cfg);
    if (inbox) buildCreateButton(inbox);
    return;
  }

  var m = path.match(/^\/([^/]+)\/([^/]+)\/issues(?:\/(new|(\d+)))?\/?$/);
  if (!m) return;
  var repo = decodeURIComponent(m[1]) + '/' + decodeURIComponent(m[2]);
  if (!m[3]) {
    buildPauseButton(cfg);
    if (repo === inbox) buildCreateButton(repo);
    return;
  }
  if (m[3] === 'new') { injectIntoNewForm(cfg, repo); return; }
  var num = Number(m[3]);
  hideLabelEvents();
  flushFmtComment(repo, num);
  getJSON(API + '/fmt_cids?repo=' + encodeURIComponent(repo) + '&number=' + num)
    .then(function (r) {
      FMT_CIDS = (r && r.cids) || [];
      injectCommentFormat(repo, num);
    });
  getJSON(API + '/labels?repo=' + encodeURIComponent(repo) + '&number=' + num)
    .then(function (ls) {
      injectEditPanel(cfg, repo, num, getPending(repo, num), (ls && ls.labels) || null);
    });
});
})();
