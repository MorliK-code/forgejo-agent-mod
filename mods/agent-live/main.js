(function () {
'use strict';
/* Мод «Живые обновления» (#22).

   Раз в INTERVAL страница перечитывает СЕБЯ (fetch location.href, GET, куки
   браузера) и, если серверный HTML динамических зон отличается от последнего
   виденного, заменяет эти зоны в живом DOM. Никакого своего рендера —
   вставляем ровно то, что Forgejo отдал бы при перезагрузке.

   Зоны:
   - задача:  таймлайн (комментарии+события), метки, исполнители, milestone,
              лейбл статуса Open/Closed;
   - список:  #issue-list.
   Сравнение — с предыдущим серверным снимком, а не с живым DOM: DOM правят
   Forgejo-скрипты и наши же моды — относительно них сравнение вечно «грязное».

   Паузы (переносим снимку, свежая версия придёт на следующем тике):
   - вкладка скрыта;
   - на задаче открыта инлайн-правка комментария (.edit-content-zone видима)
     или чей-то dropdown внутри таймлайна открыт;
   - в сайдбаре открыт dropdown редактора (метки/исполнители/milestone);
   - в списке выбраны чекбоксы (идёт bulk-операция). */

var INTERVAL = 3000;

function areas() {
  var p = location.pathname;
  if (/^\/[^/]+\/[^/]+\/issues\/\d+$/.test(p)) {
    return [
      {sel: '.issue-content-left .ui.timeline', guard: 'timeline'},
      {sel: '.issue-content-right .ui.labels.list', guard: 'sidebar'},
      {sel: '.issue-content-right .ui.assignees.list', guard: 'sidebar'},
      {sel: '.issue-content-right #milestone-section', guard: 'sidebar'},
      // статус: меняется класс элемента (green/red), поэтому меняем элемент целиком
      {sel: '.issue-title-header .issue-state-label', whole: true, guard: 'timeline'}
    ];
  }
  if (p === '/issues' || p === '/issues/' || /^\/[^/]+\/[^/]+\/issues\/?$/.test(p)) {
    return [{sel: '#issue-list', guard: 'selected'}];
  }
  return null;
}

var AREAS = areas();
if (!AREAS) return;

var busy = false;

function blocked(kind) {
  if (kind === 'timeline')
    return !!document.querySelector('.edit-content-zone:not(.tw-hidden)') ||
           !!document.querySelector('.issue-content-left details[open]');
  if (kind === 'sidebar')   // открыт редактор меток/исполнителей/milestone — не перезаписывать
    return !!document.querySelector('.issue-content-right details[open]');
  if (kind === 'selected')
    return !!document.querySelector('#issue-list input[type=checkbox]:checked');
  return false;
}

function tick() {
  if (busy || document.hidden) return;
  busy = true;
  fetch(location.href, {credentials: 'same-origin', headers: {Accept: 'text/html'}})
    .then(function (r) { return r.ok ? r.text() : null; })
    .then(function (h) {
      busy = false;
      if (!h) return;
      var doc = new DOMParser().parseFromString(h, 'text/html');
      AREAS.forEach(function (a) {
        var server = doc.querySelector(a.sel);
        if (!server) return;                    // чужая/неизвестная страница — тихий no-op
        var html = a.whole ? server.outerHTML : server.innerHTML;
        if (a.snap === undefined) { a.snap = html; return; }  // первый тик — базовый снимок
        if (html === a.snap) return;            // сервер не менялся — DOM не трогаем
        var live = document.querySelector(a.sel);
        if (!live || blocked(a.guard)) return;  // пауза: снимок не обновляем — swap дождётся
        a.snap = html;
        if (a.whole) live.replaceWith(doc.importNode(server, true));
        else live.innerHTML = html;
      });
    })
    .catch(function () { busy = false; });      // сеть/500 — попробуем на следующем тике
}

tick();
setInterval(tick, INTERVAL);
})();
