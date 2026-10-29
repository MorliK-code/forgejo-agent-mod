/* agent-mods — core-мод: менеджер модов. Глава «Моды» в /user/settings#mods.
 * Динамически добавляет пункт «Агент» в сайдбар (если agent-settings включён).
 * Управление: injection-моды — вкл/выкл (bootstrap), template-моды — deploy/undeploy.
 */
(function () {
  'use strict';
  var SUB = (window.config && window.config.appSubUrl) || '';
  var BASE = SUB + '/agent-admin';
  var state = null;
  var panelEl = null;
  var agentNavItem = null;

  function api(path, body) {
    var opt = { method: body ? 'POST' : 'GET', credentials: 'same-origin' };
    if (body) { opt.headers = { 'Content-Type': 'application/json' }; opt.body = JSON.stringify(body); }
    return fetch(BASE + path, opt).then(function (r) {
      if (r.status === 401) throw { auth: true };
      return r.json().then(function (j) {
        if (r.status >= 400) throw (j && j.error) || ('HTTP ' + r.status);
        return j;
      });
    });
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>\"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }

  // ---------- nav: «Агент» появляется/исчезает вместе с модом agent-settings ----------
  function syncAgentNav() {
    var on = false, m = (state || {}).mods || [];
    for (var i = 0; i < m.length; i++) {
      if (m[i].dir === 'agent-settings' && m[i].enabled) on = true;
    }
    var menu = document.querySelector('.flex-container-nav .ui.vertical.menu');
    if (!menu) return;
    if (on && !agentNavItem) {
      var a = document.createElement('a');
      a.className = 'item';
      a.href = SUB + '/user/settings#agent';
      a.textContent = 'Агент';
      var repos = menu.querySelector('a[href$="/user/settings/repos"]');
      if (repos) menu.insertBefore(a, repos); else menu.appendChild(a);
      agentNavItem = a;
    } else if (!on && agentNavItem) {
      agentNavItem.remove(); agentNavItem = null;
    }
  }

  function route() {
    var nav = document.querySelector('.flex-container-nav .ui.vertical.menu a[href$="#mods"]');
    if (nav) nav.classList.toggle('active', location.hash === '#mods');
    if (location.hash === '#mods') openPanel(); else closePanel();
  }

  function openPanel() {
    var content = document.querySelector('.user-setting-content');
    if (!content) return;
    if (!panelEl) {
      panelEl = document.createElement('div');
      panelEl.id = 'mods-panel';
      panelEl.innerHTML =
        '<h4 class="ui top attached header">Моды</h4>' +
        '<div class="ui bottom attached segment">' +
        '<div id="mods-tab-mods" class="agent-section"></div>' +
        '</div>';
      content.parentNode.insertBefore(panelEl, content.nextSibling);
    }
    content.style.display = 'none';
    panelEl.style.display = '';
    refresh();
  }
  function closePanel() {
    if (panelEl) panelEl.style.display = 'none';
    // контент возвращаем, только если панель «Агента» не открыта —
    // оба скрипта живут на одной странице и дергают .user-setting-content
    var agent = document.getElementById('agent-panel');
    var agentOpen = agent && agent.style.display !== 'none';
    var content = document.querySelector('.user-setting-content');
    if (content && !agentOpen) content.style.display = '';
  }

  // ---------- mods ----------
  var TRASH_SVG = '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4h11"/><path d="M5.5 4V2.8c0-.4.3-.8.8-.8h3.4c.5 0 .8.4.8.8V4"/><path d="M4 4l.7 9.2c0 .4.4.8.8.8h5c.4 0 .8-.4.8-.8L12 4"/><path d="M6.5 7v4M9.5 7v4"/></svg>';
  function switchHtml(name, on, busy) {
    return '<label class="agent-switch" data-mod="' + esc(name) + '" data-on="' + (on ? '1' : '0') + '">' +
      '<input type="checkbox" ' + (on ? 'checked' : '') + (busy ? ' disabled' : '') + '>' +
      '<span class="agent-switch-slider"></span></label>';
  }
  function renderMods() {
    var ms = (state && state.mods) || [];
    var rows = ms.map(function (m) {
      var native = m.has_templates || m.has_public;
      // тумблер: injection-моды — флаг в bootstrap (без рестарта);
      // template-моды — deploy/undeploy (рестарт forgejo)
      var ctrl = m.core
        ? '<span class="agent-dim" title="core-мод: выключить нельзя">core</span>'
        : switchHtml(m.dir, native ? m.deployed : m.enabled);
      var del = m.core
        ? ''
        : '<button class="agent-iconbtn" title="Удалить мод ' + esc(m.dir) + '" data-moddel="' + esc(m.dir) + '">' + TRASH_SVG + '</button>';
      return '<tr>' +
        '<td><div class="agent-modname">' + esc(m.title || m.dir) + '</div>' +
        '<div class="agent-moddir">' + esc(m.dir) + '</div></td>' +
        '<td class="agent-dim agent-mono">' + (native ? 'template-оверраи' : esc((m.pages || []).join(' | '))) + '</td>' +
        '<td class="agent-actions">' + ctrl + '</td>' +
        '<td class="agent-actions">' + del + '</td></tr>';
    }).join('');
    panelEl.querySelector('#mods-tab-mods').innerHTML =
      '<table class="ui compact table" style="max-width:680px"><thead><tr>' +
      '<th>Мод</th><th>Тип</th><th style="width:70px"></th><th style="width:40px"></th>' +
      '</tr></thead><tbody>' +
      (rows || '<tr><td colspan="4" class="agent-empty">модов нет</td></tr>') +
      '</tbody></table>';
    var scope = panelEl.querySelector('#mods-tab-mods');
    // тумблеры (без статусных сообщений; при ошибке откатывается)
    scope.querySelectorAll('.agent-switch input').forEach(function (inp) {
      inp.addEventListener('change', function () {
        var lbl = inp.closest('.agent-switch');
        var name = lbl.getAttribute('data-mod');
        var on = inp.checked;
        inp.disabled = true;
        var isTpl = false, m0 = (state && state.mods || []).find(function (x) { return x.dir === name; });
        isTpl = m0 && (m0.has_templates || m0.has_public);
        var endpoint = isTpl ? (on ? '/api/mods/deploy' : '/api/mods/undeploy') : '/api/mods/enable';
        api(endpoint, isTpl ? { name: name } : { name: name, enabled: on })
          .then(function (r) {
            if (r.error) throw r.error;
            refresh();
          })
          .catch(function () {
            inp.checked = on; // откат
            inp.disabled = false;
          });
      });
    });
    // удаление
    scope.querySelectorAll('[data-moddel]').forEach(function (b) {
      b.addEventListener('click', function () {
        var name = b.getAttribute('data-moddel');
        if (!confirm('Удалить мод ' + name + '?\nКаталог будет удалён из репо (commit + push), файлы — из custom/ (forgejo рестартует).')) return;
        b.disabled = true;
        api('/api/mods/delete', { name: name })
          .then(function (r) {
            if (r.error) throw r.error;
            refresh();
          })
          .catch(function () { b.disabled = false; })
          .finally(function () { b.disabled = false; });
      });
    });
  }

  function refresh() {
    if (location.hash !== '#mods' || !panelEl) return;
    api('/api/state')
      .then(function (st) {
        state = st;
        renderMods();
        syncAgentNav();
      })
      .catch(function (e) {
        var s = panelEl.querySelector('#mods-tab-mods');
        if (!s) return;
        s.innerHTML = e && e.auth
          ? '<div class="agent-empty">нужен вход в Forgejo (логин) — после входа панель заработает сама</div>'
          : '<div class="agent-empty">ошибка: ' + esc(String(e)) + '</div>';
      });
  }

  // скрипт с defer из navbar.tmpl — DOM готов; работаем только на /user/settings/*
  if (location.pathname.startsWith(SUB + '/user/settings')) {
    route();
    window.addEventListener('hashchange', route);
    api('/api/state').then(function (st) { state = st; syncAgentNav(); refresh(); }).catch(function () {});
  }
})();
