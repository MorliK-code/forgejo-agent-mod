/* agent-statusbar mod: фоновый индикатор статуса агента.
 *
 * Маленький бейдж в правом нижнем углу: цветная точка + значение state.service
 * из GET /agent-admin/api/state (опрос раз в 10с).
 *   active      -> зелёная точка
 *   unavailable -> серая точка
 *   иначе       -> красная точка
 * 401 (нет cookie-логина)  -> бейдж не показываем и опрос останавливаем:
 *   это фоновый индикатор, а не панель — prompt-логин НЕ дёргаем.
 * API недоступен / ошибка сети -> ничего не рендерим (try/catch), страницу не ломаем.
 */
(function () {
  'use strict';

  // sub-URL Forgejo: чтобы API работал и при '/', и при '/sub/'
  var SUB = (window.config && window.config.appSubUrl) || '';
  var BASE = SUB + '/agent-admin';

  var badge = null;   // корневой <div> бейджа
  var dot = null;     // <span> — точка
  var text = null;    // <span> — текст service
  var timer = null;   // setInterval

  function build() {
    badge = document.createElement('div');
    badge.id = 'agent-statusbar';
    badge.className = 'agent-statusbar';
    badge.style.display = 'none';

    dot = document.createElement('span');
    dot.className = 'agent-statusbar-dot';

    text = document.createElement('span');
    text.className = 'agent-statusbar-text';

    badge.appendChild(dot);
    badge.appendChild(text);
    (document.body || document.documentElement).appendChild(badge);
  }

  function hide() {
    if (badge) badge.style.display = 'none';
  }

  function show(service) {
    var cls = 'err';
    if (service === 'active') cls = 'ok';
    else if (service === 'unavailable') cls = 'off';
    badge.className = 'agent-statusbar ' + cls;
    text.textContent = service;
    badge.style.display = '';
  }

  function poll() {
    if (!badge) return;
    fetch(BASE + '/api/state', { credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) {
          // фоновый индикатор: без логина не показываем и не опрашиваем дальше
          if (timer) { clearInterval(timer); timer = null; }
          hide();
          return null;
        }
        return r.json().then(function (j) {
          if (r.status >= 400) throw new Error('HTTP ' + r.status);
          show((j && j.service) || 'unavailable');
        });
      })
      .catch(function () {
        // API недоступен / ошибка сети / не-JSON: просто не показываем,
        // страницу не трогаем. Опрос продолжаем (сервис мог вернуться).
        hide();
      });
  }

  function start() {
    build();
    poll();
    timer = setInterval(poll, 10000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
