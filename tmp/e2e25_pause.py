"""#25 e2e-харнес: гейт паузы в поллере + обвешивание меток в цикле паузы.

Оффлайн: кандидаты, wake и Forgejo-клиент подставлены, живой Forgejo/Hermes не трогаем."""
import sys

sys.path.insert(0, "/opt/agent-loop/repo")
from agent import poller  # noqa: E402

CAND = [{"key": "o/r#1", "owner": "o", "repo": "r", "num": 1,
         "issue": {"created_at": "2026-10-22T00:00:00Z"}, "labels": ["agent"],
         "st": {"last_cid": 0, "last_upd": "", "ran": False},
         "fresh": ["x"], "max_id": 1, "max_upd": "", "wake": True, "hold": True, "prio": 5}]

woke = []
tagged = []
poller._bridge_candidates = lambda *a, **k: [dict(CAND[0])]
poller._bridge_wake = lambda cfg, fj, state, bs, c: woke.append(c["key"])


class FakeFj:
    def issues(self, owner, repo, state="open"):
        return [{"number": 1, "labels": [{"name": "agent"}], "body": "агентская"}]

    def add_labels(self, owner, repo, num, labels):
        tagged.append((f"{owner}/{repo}#{num}", labels))

    def remove_labels(self, owner, repo, num, labels):
        tagged.append((f"{owner}/{repo}#{num}", labels))


cfg = {"forgejo": {"owner": "o", "repo": "r", "trigger_labels": ["agent"],
                   "checkbox": "Агент", "user": "hermes"},
       "__path__": "/tmp/e2e25_state.yaml"}
fj = FakeFj()

poller.poll_once_bridge(cfg, fj, {})
assert woke == ["o/r#1"], woke                       # без паузы — будит, метки не трогает
assert tagged == [], tagged

cfg["forgejo"]["paused"] = True
poller.poll_once_bridge(cfg, fj, {})
assert woke == ["o/r#1"], woke                       # на паузе — wake нет
assert tagged == [("o/r#1", ["pause"])], tagged      # зато навесили метку паузы

cfg["forgejo"]["paused"] = False
poller.poll_once_bridge(cfg, fj, {})
assert woke == ["o/r#1", "o/r#1"], woke              # сняли паузу — снова будит

print("e2e25 OK: пауза блокирует wake и обвешивает 'pause'; снятие возвращает wake")
