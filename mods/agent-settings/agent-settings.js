/* agent-settings — панель «Агент» в настройках Forgejo (/user/settings#agent).
 * Injection-мод: включается тумблером во вкладке «Моды» (bootstrap подхватит без рестарта).
 * Оригинал-контент настраиваемой страницы СКРЫВАЕМ, а не заменяем —
 * обработчики Gitea остаются живыми, возврат без потерь.
 */
(function () {
  'use strict';
  var SUB = (window.config && window.config.appSubUrl) || '';
  var BASE = SUB + '/agent-admin';

  var state = null;      // /api/state
  var models = null;     // /api/models
  var tab = 'status';
  var panelEl = null;
  var timer = null;
  var modelEdit = null;    // имя модели (или '' для новой), пока открыта форма правки

  // ---------- api ----------
  // auth = сессия Forgejo; отдельного пароля нет. 401 = не залогинен в Forgejo.
  function api(path, body) {
    var opt = { method: body ? 'POST' : 'GET', credentials: 'same-origin' };
    if (body) { opt.headers = { 'Content-Type': 'application/json' }; opt.body = JSON.stringify(body); }
    return fetch(BASE + path, opt).then(function (r) {
      if (r.status === 401) throw { auth: true };
      // 5xx = nginx отдаёт HTML-502, пока панель перезапускается: читаем как текст,
      // чтобы вместо "Unexpected token '<'" была человеческая ошибка (shortcut: авто-ретая нет, чтобы POST не дублировался)
      return r.text().then(function (t) {
        if (r.status >= 500) throw 'Сервер панели перезапускается — попробуй ещё раз через пару секунд';
        var j;
        try { j = JSON.parse(t); } catch (e) { throw 'Сервер ответил не JSON (HTTP ' + r.status + ') — обнови страницу'; }
        if (r.status >= 400) throw (j && j.error) || ('HTTP ' + r.status);
        return j;
      });
    });
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>\"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }

  // ---------- nav ----------
  // пункт «Агент» добавляет core-мод agent-mods (живёт, пока включён этот мод)
  function agentNav() {
    return document.querySelector('.flex-container-nav .ui.vertical.menu a[href$="#agent"]');
  }
  function route() {
    var nav = agentNav();
    if (nav) nav.classList.toggle('active', location.hash === '#agent');
    if (location.hash === '#agent') openPanel(); else closePanel();
  }

  function openPanel() {
    var content = document.querySelector('.user-setting-content');
    if (!content) return;
    if (!panelEl) { panelEl = buildPanel(); content.parentNode.insertBefore(panelEl, content.nextSibling); }
    content.style.display = 'none';
    panelEl.style.display = '';
    panelEl.querySelectorAll('.agent-tab').forEach(function (t) {
      t.classList.toggle('active', t.getAttribute('data-tab') === tab);
    });
    panelEl.querySelectorAll('.agent-section').forEach(function (s) {
      s.style.display = s.id === 'agent-tab-' + tab ? '' : 'none';
    });
    refresh();
    if (!timer) timer = setInterval(refresh, 5000);
  }
  function closePanel() {
    if (panelEl) panelEl.style.display = 'none';
    if (timer) { clearInterval(timer); timer = null; }
    // контент возвращаем, только если панель «Моды» не открыта —
    // оба скрипта живут на одной странице и дергают .user-setting-content
    var mods = document.getElementById('mods-panel');
    var modsOpen = mods && mods.style.display !== 'none';
    var content = document.querySelector('.user-setting-content');
    if (content && !modsOpen) content.style.display = '';
  }

  // ---------- panel ----------
  function buildPanel() {
    var el = document.createElement('div');
    el.id = 'agent-panel';
    el.innerHTML =
      '<h4 class="ui top attached header" id="agent-panel-header">Агент</h4>' +
      '<div class="ui bottom attached segment">' +
      '<div class="ui top attached tabular menu">' +
      ['status|Статус', 'models|Модели', 'tasks|Задачи', 'tools|Инструменты'].map(function (t) {
        var p = t.split('|');
        return '<a class="item agent-tab ' + (p[0] === 'status' ? 'active' : '') + '" data-tab="' + p[0] + '">' + p[1] + '</a>';
      }).join('') + '</div>' +
      '<div id="agent-tab-status" class="agent-section"></div>' +
      '<div id="agent-tab-models" class="agent-section" style="display:none"></div>' +
      '<div id="agent-tab-tasks" class="agent-section" style="display:none"></div>' +
      '<div id="agent-tab-tools" class="agent-section" style="display:none"></div>' +
      '</div>';
    el.querySelectorAll('.agent-tab').forEach(function (t) {
      t.addEventListener('click', function () {
        tab = t.getAttribute('data-tab');
        el.querySelectorAll('.agent-tab').forEach(function (x) { x.classList.toggle('active', x === t); });
        el.querySelectorAll('.agent-section').forEach(function (s) {
          s.style.display = s.id === 'agent-tab-' + tab ? '' : 'none'; });
        renderTab();
      });
    });
    return el;
  }

  function showTab(id, html) {
    var s = panelEl.querySelector('#agent-tab-' + id);
    if (s) s.innerHTML = html;
  }

  function renderTab() {
    // guard: данные ещё не загружены (refresh() асинхронный) — не рвём renderXxx на null
    if (!state || !models) {
      showTab(tab, '<div class="agent-empty">Загрузка…</div>');
      return;
    }
    if (tab === 'status') renderStatus();
    else if (tab === 'models') renderModels();
    else if (tab === 'tasks') renderTasks();
    else renderTools();
  }

  // ---------- status ----------
  function renderStatus() {
    var st = state;
    if (!st || !st.effective) return;
    var dot = st.service === 'active' ? 'ok' : 'err';
    showTab('status',
      '<div style="max-width:560px">' +
      kv('agent-loop', '<span class="agent-status-dot ' + dot + '"></span>' + esc(st.service)) +
      (st.since ? kv('в работе с', esc(st.since)) : '') +
      kv('интервал опроса', '<span class="agent-mono">' + esc(st.effective.forgejo.poll_interval) + 's</span>') +
      kv('дефолтная модель', '<span class="agent-mono">' + esc(st.effective.default_model || '—') + '</span>') +
      kv('открытых задач', '<span class="agent-mono">' + (st.open_issues < 0 ? '—' : esc(st.open_issues)) + '</span>') +
      kv('последняя активность', '<span class="agent-mono">' + esc(st.last_activity || '—') + '</span>') +
      '</div><pre class="ui compact segment agent-mono agent-log">' + esc((st.log || []).join('\n') || '(пусто)') + '</pre>'
    );
  }
  function kv(l, v) { return '<div class="agent-kv"><span class="lbl">' + l + '</span><span>' + v + '</span></div>'; }

  // ---------- models ----------
  function renderModels() {
    var m = models;
    if (!m) return;
    var rows = Object.keys(m.providers || {}).map(function (n) {
      var p = m.providers[n];
      var isDef = m.default === n;
      return '<tr class="agent-mrow" data-name="' + esc(n) + '">' +
        '<td>' + (isDef ? '<span class="agent-badgedef">default</span>' : '') + '</td>' +
        '<td class="agent-mono">' + esc(n) + '</td>' +
        '<td><span class="agent-dim agent-mono">' + esc(p.base_url) + '</span><br>' +
        '<span class="agent-mono">' + esc(p.model) + '</span></td>' +
        '<td class="agent-mono">' + esc(p.max_tokens || '') + '</td>' +
        '<td class="agent-mono">' + esc(p.timeout || '') + 's</td>' +
        '<td class="agent-actions">' +
        '<button class="ui mini button" data-act="edit">изм</button>' +
        '<button class="ui mini button" data-act="test">тест</button>' +
        (isDef ? '' : '<button class="ui mini button" data-act="def">→def</button>') +
        '<button class="ui mini negative button" data-act="del">✕</button>' +
        '</td></tr>';
    }).join('');
    showTab('models',
      '<table class="ui compact table agent-models"><thead><tr>' +
      '<th style="width:60px"></th><th>Имя</th><th>Endpoint / модель</th><th>max_tokens</th><th>timeout</th><th></th>' +
      '</tr></thead><tbody>' +
      (rows || '<tr><td colspan="6" class="agent-empty">нет моделей — добавьте</td></tr>') +
      '</tbody></table>' +
      '<div style="margin-top:12px">' +
      '<button class="ui small button" id="agent-add-model">+ Добавить модель</button> ' +
      '<span class="agent-dim">Ключ хранится в models.json (chmod 600), ${ENV} поддерживается</span>' +
      '</div>' +
      '<div id="agent-model-edit"></div>' +
      '<div class="agent-msg" id="agent-model-msg"></div>'
    );
    panelEl.querySelectorAll('#agent-tab-models .agent-mrow [data-act]').forEach(function (b) {
      b.addEventListener('click', function () {
        var name = b.closest('tr').getAttribute('data-name');
        var act = b.getAttribute('data-act');
        if (act === 'edit') modelForm(name);
        else if (act === 'test') modelTest(name, b);
        else if (act === 'def') api('/api/models/default', { name: name }).then(refresh);
        else if (act === 'del' && confirm('Удалить модель ' + name + '?'))
          api('/api/models/delete', { name: name }).then(refresh);
      });
    });
    var add = panelEl.querySelector('#agent-add-model');
    if (add) add.addEventListener('click', function () { modelForm(null); });
    if (modelEdit == null) {
      var eb = panelEl.querySelector('#agent-model-edit');
      if (eb) eb.innerHTML = '';
    }
  }

  function modelForm(orig) {
    // orig — исходное имя модели (ключ в models.json) или null для новой.
    // Имя редактируемо: при смене backend делает rename (old_name -> name).
    modelEdit = orig == null ? '' : orig;
    var p = orig ? models.providers[orig] : {};
    var box = panelEl.querySelector('#agent-model-edit');
    box.innerHTML =
      '<div class="ui segment agent-form" style="margin-top:10px;max-width:640px">' +
      '<div style="font-size:13px;font-weight:600;margin-bottom:8px">' + (orig ? 'Модель: ' + esc(orig) : 'Новая модель') + '</div>' +
      fld('Имя', '<input class="agent-in-name" placeholder="qwen-27b" value="' + esc(orig || '') + '">') +
      fld('Base URL', '<input class="agent-in-base agent-mono" placeholder="http://192.168.110.3:8080/v1" value="' + esc(p.base_url || '') + '">') +
      fld('Model ID', '<input class="agent-in-model agent-mono" placeholder="Qwen 3.8 27B KV4 C500" value="' + esc(p.model || '') + '">') +
      fld('API Key', '<input class="agent-in-key" type="password" placeholder="' + (p.api_key ? '••••• (пусто = не менять)' : 'пусто = not-needed') + '">') +
      '<div class="inline">' +
      fld('max_tokens', '<input class="agent-in-max" type="number" min="1" value="' + esc(p.max_tokens || 4096) + '">') +
      fld('temperature', '<input class="agent-in-temp" type="number" step="0.05" min="0" max="2" value="' + esc(p.temperature == null ? 0.3 : p.temperature) + '">') +
      fld('timeout, с', '<input class="agent-in-to" type="number" min="1" value="' + esc(p.timeout || 600) + '">') +
      '</div>' +
      '<label style="font-size:12px;color:var(--color-text-light,#7d8aa0);cursor:pointer">' +
      '<input type="checkbox" class="agent-in-def" ' + (models.default === orig ? 'checked' : '') + '> дефолтная модель</label>' +
      '<div style="margin-top:10px">' +
      '<button class="ui small primary button" id="agent-m-save">Сохранить</button> ' +
      '<button class="ui small button" id="agent-m-test">Тест (без сохранения)</button> ' +
      '<button class="ui small button" id="agent-m-cancel">Отмена</button>' +
      '</div></div>';
    function fld(l, h) { return '<div class="field"><label>' + l + '</label>' + h + '</div>'; }
    function g(cls) { return box.querySelector(cls).value; }
    panelEl.querySelector('#agent-m-cancel').addEventListener('click', function () { modelEdit = null; renderModels(); });
    panelEl.querySelector('#agent-m-save').addEventListener('click', function () {
      var n = g('.agent-in-name').trim();
      if (!n) { modelMsg('нужно имя', 1); return; }
      api('/api/models', {
        name: n, old_name: orig || undefined,
        set_default: box.querySelector('.agent-in-def').checked,
        provider: {
          base_url: g('.agent-in-base').trim(), model: g('.agent-in-model').trim(),
          api_key: g('.agent-in-key') || undefined,
          max_tokens: +g('.agent-in-max') || 4096,
          temperature: +g('.agent-in-temp') || 0.3,
          timeout: +g('.agent-in-to') || 600
        }
      }).then(function () { modelEdit = null; refresh(); }).catch(function (e) { modelMsg(e, 1); });
    });
    panelEl.querySelector('#agent-m-test').addEventListener('click', function (e) {
      var btn = e.target; btn.disabled = true; modelMsg('тест…', 0);
      api('/api/models/test', {
        name: g('.agent-in-name').trim(),
        provider: {
          base_url: g('.agent-in-base').trim(), model: g('.agent-in-model').trim(),
          api_key: g('.agent-in-key') || (orig ? models.providers[orig].api_key : ''),
          timeout: +g('.agent-in-to') || 60
        }
      }).then(function (r) {
        modelMsg(r.ok ? 'ok: ' + r.model + ' (' + (r.usage && r.usage.total_tokens != null ? r.usage.total_tokens + ' tok' : '?') + ')' : (r.error || 'fail'), r.ok ? 0 : 1);
      }).catch(function (e) { modelMsg(e, 1); }).finally(function () { btn.disabled = false; });
    });
  }
  function modelTest(name, btn) {
    btn.disabled = true; modelMsg('тест ' + name + '…', 0);
    api('/api/models/test', { name: name }).then(function (r) {
      modelMsg(r.ok ? 'ok: ' + r.model + ' (' + (r.usage && r.usage.total_tokens != null ? r.usage.total_tokens + ' tok' : '?') + ')' : (r.error || 'fail'), r.ok ? 0 : 1);
    }).catch(function (e) { modelMsg(e, 1); }).finally(function () { btn.disabled = false; });
  }
  function modelMsg(msg, isErr) {
    var el = panelEl.querySelector('#agent-model-msg');
    if (!el) return;
    el.className = 'agent-msg' + (isErr ? ' err' : ' ok');
    el.textContent = typeof msg === 'string' ? msg : String(msg);
  }

  // ---------- tasks ----------
  function renderTasks() {
    if (!state || !state.effective) return;
    var f = state.effective.forgejo;
    if (!f) return;
    var labels = (f.trigger_labels || []).join(', ');
    showTab('tasks',
      '<div style="max-width:480px" class="agent-form">' +
      fld('Интервал опроса, с', '<input class="agent-t-int" type="number" min="5" max="3600" value="' + esc(f.poll_interval) + '">') +
      fld('Trigger labels (через запятую)', '<input class="agent-t-labels agent-mono" value="' + esc(labels) + '">') +
      fld('Чекбокс-триггер', '<input class="agent-t-chk" value="' + esc(f.checkbox || 'Агент') + '">') +
      '<div class="agent-dim" style="margin:6px 0 10px">Задача подхватывается, если есть label из списка ИЛИ снят чекбокс в issue</div>' +
      '<button class="ui small primary button" id="agent-t-save">Сохранить (без рестарта)</button>' +
      '<div class="agent-msg" id="agent-t-msg"></div>' +
      '</div>'
    );
    function fld(l, h) { return '<div class="field"><label>' + l + '</label>' + h + '</div>'; }
    panelEl.querySelector('#agent-t-save').addEventListener('click', function () {
      var msg = panelEl.querySelector('#agent-t-msg');
      msg.className = 'agent-msg'; msg.textContent = 'сохранение…';
      api('/api/overlay', {
        forgejo: {
          poll_interval: +panelEl.querySelector('.agent-t-int').value,
          trigger_labels: panelEl.querySelector('.agent-t-labels').value
            .split(',').map(function (s) { return s.trim(); }).filter(Boolean),
          checkbox: panelEl.querySelector('.agent-t-chk').value.trim()
        }
      }).then(function () { msg.className = 'agent-msg ok'; msg.textContent = 'ок — агент подхватит в следующем цикле'; refresh(); })
        .catch(function (e) { msg.className = 'agent-msg err'; msg.textContent = String(e); });
    });
  }

  // ---------- tools ----------
  function renderTools() {
    if (!state || !state.effective) return;
    var a = state.effective.agent || {};
    var h = state.effective.ssh_hosts || {};
    var rows = Object.keys(h).map(function (k) {
      return '<tr data-host="' + esc(k) + '"><td class="agent-mono">' + esc(k) + '</td>' +
        '<td class="agent-mono">' + esc(h[k]) + '</td>' +
        '<td class="agent-actions"><button class="ui mini negative button" data-act="del">✕</button></td></tr>';
    }).join('');
    var hon = state.effective.honcho || {};
    var sk = state.effective.skills || {};
    showTab('tools',
      '<div class="agent-form" style="max-width:520px">' +
      fld('Лимит итераций LLM на задачу', '<input class="agent-x-iter" type="number" min="1" max="500" value="' + esc(a.max_iterations) + '">') +
      '</div>' +
      '<div style="margin-top:14px;font-size:13px;font-weight:600;margin-bottom:6px">SSH-хосты (инструмент ssh_exec)</div>' +
      '<table class="ui compact table" style="max-width:520px"><thead><tr><th>Имя</th><th>IP</th><th></th></tr></thead><tbody>' +
      (rows || '<tr><td colspan="3" class="agent-empty">нет хостов</td></tr>') +
      '</tbody></table>' +
      '<div style="max-width:520px;display:flex;gap:8px;margin:6px 0 12px">' +
      '<input class="agent-x-hname agent-mono" placeholder="имя" style="max-width:130px">' +
      '<input class="agent-x-hip agent-mono" placeholder="192.168.110.x" style="max-width:150px">' +
      '<button class="ui small button" id="agent-x-hadd">+</button></div>' +
      '<div style="margin-top:14px">' +
      kv('Honcho', '<span class="agent-mono">' + esc(hon.base_url || '—') + ' · ws: ' + esc(hon.workspace || '—') + ' · ' + esc(hon.context_chars || '?') + ' chars</span>') +
      kv('Скиллы', '<span class="agent-mono">' + esc(sk.dir || '—') + ' · max ' + esc(sk.max_in_prompt != null ? sk.max_in_prompt : '?') + ' в промпт</span>') +
      '</div>' +
      '<div style="margin-top:12px">' +
      '<button class="ui small primary button" id="agent-x-save">Сохранить (без рестарта)</button>' +
      '<div class="agent-msg" id="agent-x-msg"></div></div>'
    );
    function fld(l, h) { return '<div class="field"><label>' + l + '</label>' + h + '</div>'; }
    panelEl.querySelectorAll('#agent-tab-tools .agent-mrow [data-act="del"]').forEach(function (b) {
      b.addEventListener('click', function () {
        b.closest('tr').remove();
      });
    });
    panelEl.querySelector('#agent-x-hadd').addEventListener('click', function () {
      var name = panelEl.querySelector('.agent-x-hname').value.trim();
      var ip = panelEl.querySelector('.agent-x-hip').value.trim();
      if (!name || !ip) return;
      var tb = panelEl.querySelector('#agent-tab-tools table tbody');
      var tr = document.createElement('tr');
      tr.className = 'agent-mrow'; tr.setAttribute('data-host', name);
      tr.innerHTML = '<td class="agent-mono">' + esc(name) + '</td><td class="agent-mono">' + esc(ip) + '</td>' +
        '<td class="agent-actions"><button class="ui mini negative button" data-act="del">✕</button></td>';
      tr.querySelector('[data-act="del"]').addEventListener('click', function () { tr.remove(); });
      tb.appendChild(tr);
      panelEl.querySelector('.agent-x-hname').value = '';
      panelEl.querySelector('.agent-x-hip').value = '';
    });
    panelEl.querySelector('#agent-x-save').addEventListener('click', function () {
      var msg = panelEl.querySelector('#agent-x-msg');
      msg.className = 'agent-msg'; msg.textContent = 'сохранение…';
      var hosts = {};
      panelEl.querySelectorAll('#agent-tab-tools .agent-mrow').forEach(function (tr) {
        hosts[tr.getAttribute('data-host')] = tr.children[1].textContent;
      });
      api('/api/overlay', {
        agent: { max_iterations: +panelEl.querySelector('.agent-x-iter').value },
        ssh_hosts: hosts
      }).then(function () { msg.className = 'agent-msg ok'; msg.textContent = 'ок — действует с следующей задачи'; refresh(); })
        .catch(function (e) { msg.className = 'agent-msg err'; msg.textContent = String(e); });
    });
  }

  // ---------- refresh ----------
  function activeSection() {
    return panelEl.querySelector('#agent-tab-' + tab);
  }
  function errText(e) {
    if (e && e.auth) return 'нужен вход в Forgejo';
    return String(e == null ? '' : (typeof e === 'object' ? JSON.stringify(e) : e));
  }
  function refresh() {
    if (location.hash !== '#agent' || !panelEl) return;
    Promise.all([api('/api/state'), api('/api/models')])
      .then(function (r) {
        state = r[0]; models = r[1];
        // не перерисовывать вкладку, пока открыта форма правки модели —
        // иначе таймер (5с) сносит введённые значения
        if (!(tab === 'models' && modelEdit != null)) renderTab();
      })
      .catch(function (e) {
        var s = activeSection();
        if (!s) return;
        s.innerHTML = e && e.auth
          ? '<div class="agent-empty">нужен вход в Forgejo (логин) — после входа панель заработает сама</div>'
          : '<div class="agent-empty">ошибка: ' + esc(errText(e)) + '</div>';
      });
  }

  // ---------- issue form: выбор модели + галочка «Агент» ----------
  // Живут только когда включён этот мод (bootstrap грузит JS на /issues/new
  // только тогда). При сабмите дописываем в поле описания (textarea) строки,
  // которые сервер сериализует в тело issue: pick_model_from_body и поллер их читают.
  function initIssueForm() {
    var form = document.getElementById('new-issue');
    var ta = form && form.querySelector('textarea[name="form-field-task"]');
    if (!form || !ta) return;
    var field = ta.closest('.field');

    var wrap = document.createElement('div');
    wrap.className = 'field agent-issue-model';
    wrap.innerHTML =
      '<select id="agent-issue-model" aria-label="Модель"></select>' +
      '<label class="agent-issue-chk"><input type="checkbox" id="agent-issue-agent" checked>Агент</label>';
    if (field) field.parentNode.insertBefore(wrap, field);
    else form.insertBefore(wrap, form.firstChild);

    api('/api/models').then(function (m) {
      var sel = wrap.querySelector('#agent-issue-model');
      var provs = Object.keys(m.providers || {});
      sel.innerHTML = provs.map(function (n) {
        return '<option value="' + esc(n) + '"' + (n === m.default ? ' selected' : '') + '>' + esc(n) + '</option>';
      }).join('');
    }).catch(function () { /* без моделей — просто не показываем селект */
      var s = wrap.querySelector('#agent-issue-model'); if (s) s.style.display = 'none';
    });

    // capture — раньше, чем formFetchAction соберёт FormData
    form.addEventListener('submit', function () {
      var sel = wrap.querySelector('#agent-issue-model');
      var chk = wrap.querySelector('#agent-issue-agent');
      var lines = [];
      var model = sel && sel.value;
      if (model) lines.push('### Модель\n' + model);
      if (chk && chk.checked) lines.push('- [x] Агент');
      if (lines.length) {
        var cur = ta.value.replace(/\s+$/, '');
        ta.value = (cur ? cur + '\n\n' : '') + lines.join('\n') + '\n';
      }
    }, true);
  }

  // ---------- init ----------
  if (location.pathname.startsWith(SUB + '/user/settings')) {
    route();
    window.addEventListener('hashchange', route);
  } else if (/\/issues\/new$/.test(location.pathname)) {
    initIssueForm();
  }
})();
