# AGENTS.md — forgejo-agent-mod (агент-мод для Forgejo)

## Что это
Forgejo как harness для агента: задача = issue, общение = комментарии, работу ведёт агент (poller будит Hermes). Здесь **канон кода мода**: `Hermes/forgejo-agent-mod` (public). Трекер задач отдельный: `morlik/forgejo-agents-tasks` (issues + песочницы). live-клон кода на CT106: `/opt/agent-loop/repo` (origin = этот репо).

## Структура
- `agent/` — сам агент-харнес: `poller.py` (очередь приоритетов), `agent.py`, `bridge.py`, `forgejo_client.py`, `llm.py`, `honcho_client.py`, `admin_server.py` (:8091 backend), `mods.py`, `config.py`/`config.yaml`, `requirements.txt`.
- `deploy/` — `agent-admin.service`, `agent-loop.service`, `agent-proxy.conf` (копия из Hermes/agent-proxy — синхронизировать), `deploy.sh`, `env.example`.
- `mods/` — JS-моды Forgejo (agent-mods, agent-settings, agent-statusbar, agent-tasks), подаются через инъекцию `/__mods__/bootstrap.js`.
- Models-panel: дефолтная модель — `models.json` в панели;override в описании issue блоком `### Модель`.

## Где live и как деплоить
CT106 (Forgejo-хост, 110.9): unit-ы `agent-admin` + `agent-loop` (из deploy/), nginx-прослойка agent-proxy (`Hermes/agent-proxy`, `git pull` + `agent-proxy-ctl reload`). Деплой: зайти на CT106 (через 110.2: `pct exec 106`), репо клонирован там в `/opt/agent-loop/repo` → `deploy/deploy.sh` (unit `agent-proxy.service` там НЕ числится, это не ошибка). Правки файлов — через `sftp_put` + `cp`.

## Правила задач (кратко, авторитет — README.md)
Состояния (ровно одно): `backlog` (запланировано) → `in-progress` (в работе) → `done` (готово) / `questions` (ждёт ответа) / `pause` (пауза). Метка `agent` — не состояние: «назначен агент». Приоритеты `5..1` (5 первый), дедлайн `due_date` — клиентские. Очередь одна, без параллельности: за цикл будится ОДНА задача; порядок — приоритет desc, равный — старее (created asc). Держит очередь только задача, ГОТОВАЯ к запуску сейчас; ждущая свой этап или кулдаун — не держит (#29). Этапы по времени: агент завершает прогон строкой `NEXT:<минуты>` — полкер планирует следующий wake и будит его вторым после текущей задачи (#stages). Чат-гейт: пока основной чат Hermes активен (< bridge.main_chat_idle_gate_s), wake держат, живой прогон стопается (c09a5cd). Лента комментариев задачи скрывает события смены меток (мод agent-tasks: hideLabelEvents). Агент меняет только свои labels. Перед `done` — end-to-end проверка (скилл `e2e-bug-sweep`). Закрытую задачу решает только автор; комментарии у closed читаются и возвращают её в `in-progress`.

## Как тестировать
Создать тест-issue с шаблоном «Агент» → агент подхватывает, работает в песочнице харнеса (харнес будит только свои задачи песочницы), результат — комментарий + label `done`.

## Подводные камни
Токены — в env (deploy/env.example → реальный env на CT106), в git не сливать. e2e-песочницы прошлого прогона не должны пачкать новый (см. коммит #19 e2e).

## Связи
Hermes/agent-proxy (nginx), скилл `forgejo-task-workflow`, одноразовые песочницы Hermes/agent-e2e*.