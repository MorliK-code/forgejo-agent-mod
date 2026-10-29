# Mod-система Forgejo

## 1. Что это

Система, которая позволяет делать собственные правки UI Forgejo без единой строки в
исходниках Gitea. Два типа модов:

- **injection-моды** — js/css, которые bootstrap подгружает в живую DOM-страницу
  (см. §2). Вкл/выкл — тумблер, без рестарта forgejo.
- **template-моды** — `templates/` (нативные go-оверраи страниц) и `public/`
  (статика под `/assets/`). Вкл/выкл = deploy/undeploy в `$FORGEJO_CUSTOM/`
  + рестарт forgejo.

Каждый мод — каталог в `mods/` со своим манифестом `mod.json`.

## 2. Как устроено

```
mods/<name>/
  mod.json      # манифест: pages (regex), assets, enabled
  main.js       # код мода (vanilla JS, IIFE, без зависимостей)
  main.css      # стили мода (scoped под свой селектор)
```

Поток:

1. **nginx** на каждой HTML-странице Forgejo подставляет перед `</body>`:
   `<script defer src="/__mods__/bootstrap.js"></script>` (через `sub_filter`).
2. **bootstrap** генерируется на каждый запрос функцией
   `agent/mods.py::bootstrap_js(base_url="/__mods__")`. Он:
   - берёт список **активных** модов (`enabled: true`) через `list_mods()`;
   - на `DOMContentLoaded` прогоняет `location.pathname` по `pages`-regex каждого мода
     (`new RegExp(page).test(location.pathname)`);
   - по **первому** совпадению дописывает в `<head>` `<link rel="stylesheet">` для css
     и `<script defer>` для js; URL собирается как `BASE + '/mods/' + dir + '/' + file`,
     где `BASE = "/__mods__"`.
3. **Ассеты** раздаются без авторизации: браузер просит `/__mods__/mods/<dir>/<file>`,
   nginx (`location /__mods__/` → `proxy_pass …/agent-admin/`) мапит это в
   `/agent-admin/mods/<dir>/<file>` на backend, а `admin_server.py` отвечает
   `mod_asset(name, path)`. Этот роут обрабатывается **до** проверки cookie — то есть
   публично.
4. **API** (`/agent-admin/api/*`) — то же `admin_server.py`, но **за** cookie-проверкой.

> **Имя файла bootstrap.** nginx подставляет `__mods__/bootstrap.js`, а
> `location /__mods__/` мапит его в `/agent-admin/bootstrap.js`. Backend раздаёт
> bootstrap по обоим роутам — `/agent-admin/bootstrap.js` и
> `/agent-admin/agent-ui.js` (см. `do_GET` в `admin_server.py`).

## 3. Формат `mod.json`

| Поле       | Тип        | По умолчанию | Описание |
|------------|------------|--------------|----------|
| `name`     | string     | —            | Идентификатор мода. Должен совпадать с именем каталога (каталог — канон, `list_mods()` сам подставляет `dir` = имя каталога). |
| `title`    | string     | —            | Человекочитаемое название; показывается в панели «Моды». |
| `enabled`  | bool       | `false`      | Включён ли мод. Только `true`-моды попадают в bootstrap (injection-часть). Для template-модов вкл/выкл = deploy/undeploy. |
| `core`     | bool       | `false`      | Core-мод: нельзя undeploy из панели (это сам мод-менеджер — `agent-mods`). В UI показывается меткой `core` вместо кнопки. |
| `pages`    | string[]   | `[]`         | Массив regex на `location.pathname` (не на полный URL, не на hash). Мод грузится на первом совпавшем. |
| `assets.js`| string[]   | `[]`         | js-файлы мода (относительно каталога мода), подключаются как `<script defer>`. |
| `assets.css`| string[]  | `[]`         | css-файлы, подключаются как `<link rel="stylesheet">`. |

Поля `enabled`/`pages`/`assets` опциональны — `list_mods()` сам заполняет недостающие
значениями по умолчанию. `dir` в файл писать не нужно — он выводится из имени каталога.

Пример (как в `agent-settings`):

```json
{
  "name": "agent-statusbar",
  "title": "Статусбар агента",
  "enabled": false,
  "pages": ["^/"],
  "assets": {
    "js": ["main.js"],
    "css": ["main.css"]
  }
}
```

Примеры `pages`:
- `"^/"` — все страницы (pathname всегда начинается с `/`);
- `"^/user/settings"` — только страница настроек;
- `"^/org/.*/settings"` — настройки любого org.

Regex проверяется на сервере при генерации bootstrap (`re.compile` в `list_mods()`);
битый паттерн **отбрасывается**, а не ломает генерацию. На клиенте `new RegExp(...)`
обёрнут в `try/catch`.

## 4. Как написать мод

1. **Каталог.** Создай `mods/<name>/` (имя — буквы/цифры/`_`/`-`, без пробелов и `..`).
2. **`mod.json`.** Заполни манифест (см. §3). Для нового мода ставь `enabled: false` —
   включать будешь из панели.
3. **`main.js`.** Vanilla JS в IIFE. Ниже — минимальный рабочий шаблон с доступом к
   `window.config.appSubUrl` и обращением к API с обработкой 401 (prompt-логин, как в
   `agent-settings`):

```js
/* <name> mod */
(function () {
  'use strict';

  // sub-URL Forgejo (например '' или '/forgejo'), чтобы API работал при любом адресе
  var SUB  = (window.config && window.config.appSubUrl) || '';
  var BASE = SUB + '/agent-admin';

  // ---- API: GET/POST с cookie, 401 -> prompt-логин -> одна попытка повторить ----
  function api(path, body, retried) {
    var opt = { method: body ? 'POST' : 'GET', credentials: 'same-origin' };
    if (body) {
      opt.headers = { 'Content-Type': 'application/json' };
      opt.body = JSON.stringify(body);
    }
    return fetch(BASE + path, opt).then(function (r) {
      if (r.status === 401 && !retried) {
        return askLogin().then(function (ok) {
          if (!ok) throw { auth: true };
          return api(path, body, true);
        });
      }
      return r.json().then(function (j) {
        if (r.status >= 400) throw (j && j.error) || ('HTTP ' + r.status);
        return j;
      });
    });
  }

  function askLogin() {
    return new Promise(function (resolve) {
      var pw = prompt('Пароль панели «Агент»:');
      if (pw === null) { resolve(false); return; }
      fetch(BASE + '/api/login', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pw })
      })
        .then(function (r) { resolve(!!r.ok); })
        .catch(function () { resolve(false); });
    });
  }

  // ---- твой код ----
  function init() {
    // сюда: DOM-правки, render, api('/api/...')...
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
```

   Ключевые детали:
   - **`window.config.appSubUrl`** — префикс, под которым висит Forgejo. `BASE =
     SUB + '/agent-admin'` — единственный правильный способ собрать URL на API,
     чтобы мод работал и при `https://host/`, и при `https://host/sub/`.
   - **`credentials: 'same-origin'`** — fetch шлёт cookie `admin` (из логина). Без
     него API ответит 401.
   - **401 → prompt-логин → одна повторная попытка** (`retried`-флаг). Это для
     интерактивных панелей. Фоновые индикаторы так не должны делать (см.
     `agent-statusbar`): там при 401 просто не показывают UI.

4. **`main.css`.** Стили, scoped под собственный селектор/ID (например
   `#agent-statusbar …`), чтобы не задеть разметку Forgejo.

5. **Проверь синтаксис** `main.js` (например `node --check main.js`) перед деплоем.

## 5. Безопасность

- **Ассеты публичные** (`/agent-admin/mods/<name>/<file>` отдаётся **до** cookie-проверки) —
  в js/css **никаких секретов**: ни токенов, ни паролей, ни ключей. Всё, что там есть,
  видит любой, кто открыл страницу Forgejo.
- **API — по сессии Forgejo.** `/agent-admin/api/*` доступен только залогиненным
  в Forgejo: backend перепроверяет браузерный cookie через `GET /user/login`
  (3xx = залогинен; кэш 30с). Отдельного пароля панели нет — аутентификация =
  аккаунт Forgejo. Не залогинен → 401, панель показывает «нужен вход в Forgejo».
- **Path-traversal-защита в `mod_asset(name, path)`**:
  - `name` должен fullmatch `^[\w.\-/]+$` и не содержать `..`;
  - `path` — непустой, без `..`, не начинается с `/`;
  - итоговый путь резолвится и проверяется, что он **остаётся внутри** `mods/<name>/`
    и является файлом. Иначе `None` (404).
- **Regex в `pages` валидируются** при генерации bootstrap (`re.compile` в `list_mods()`);
  битый отбрасывается — не может сломать загрузку страницы. На клиенте `new RegExp`
  в `try/catch`.

## 6. Включение / выключение мода

Два способа:

1. **Через API** (то, что делает панель «Моды»):
   ```
   POST /agent-admin/api/mods/enable
   { "name": "agent-statusbar", "enabled": true }
   ```
   `name` — имя **каталога** (не `name` из mod.json, хотя обычно совпадает).
   Внутри — `mods.py::set_enabled(name, enabled)`, который пишет `enabled` в `mod.json`.
2. **Вручную** — поправь `"enabled": true` в `mods/<name>/mod.json` и перезагрузи
   страницу (bootstrap генерируется на каждый запрос).

Список модов (для отладки) — `GET`/`POST /agent-admin/api/mods` → `list_mods()`.

После включения мод **появляется в подглаве «Моды»** панели агента
(`/user/settings#agent` → вкладка «Моды»). Таблица берётся из `state.mods`
(тот же `list_mods()`) и показывает: статус (on/off), каталог, название, `pages` и
кнопку вкл/выкл. Кнопка дёргает `/agent-admin/api/mods/enable`.

## 7. Мод «Задачи» (agent-tasks) — туду-лист без выбора репозитория

Forgejo не умеет задачи вне репозитория (API требует `owner/repo`) и не умеет
переносить задачу между репо. Поэтому туду-лист живёт в одном **служебном**
репо, а UI репозиторий не показывает.

- **Служебный репо** — `forgejo.tasks_repo` в `config.yaml` (сейчас
  `hermes/inbox`). Меняется одной строкой + рестарт `agent-loop`; мод репо не
  хардкодит — он спрашивает его у бэкенда.
- **API бэкенда** (`/agent-admin/api/tasks*`, по сессии Forgejo):
  - `GET  /api/tasks/config` → `{repo, priorities, labels}` — мод по нему
    понимает, какой репозиторий считать «Задачами»;
  - `GET  /api/tasks?state=open|closed|all` → список задач;
  - `POST /api/tasks` `{title, body, priority, agent, due}` → создать задачу;
  - `POST /api/tasks/close|reopen` `{number}`;
  - `POST /api/tasks/attach` `{number, repo}` → привязка к реальному репо:
    label `repo:<owner>/<repo>` + комментарий, **номер задачи не меняется**.
- **Метки**: `todo` (по умолчанию), `agent` (взять агенту — триггер agent-loop),
  `p1..p4` (приоритет), `repo:<name>` (привязка), `done`/`review` (ставит агент).
  Все метки создаются автоматически (`ForgejoClient.ensure_labels`).
- **Что делает мод**: на странице задач служебного репо (`/<owner>/<repo>/issues`)
  рисует панель быстрого добавления (название + приоритет + «агенту» + дедлайн);
  на нативной странице `/issues` даёт ссылку «Общие задачи →». Выключенный мод
  исчезает из bootstrap — страница снова чистый Forgejo, ничего не чинить.
- **Agent-loop** опрашивает список репо (`tasks.targets()`): рабочий репо +
  служебный туду-репо. Ключ состояния поллера — `owner/repo#number`.

