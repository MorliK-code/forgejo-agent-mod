"""#25 e2e-харнес (оффлайн): пауза/ресюм очереди = метки + пауза отдельной задачи.

Семантика 22.10: on вешает 'pause' на агентские задачи; off снимает её со ВСЕХ
агентских — включая паузы, которые поставили агенты."""
import sys

sys.path.insert(0, "/opt/agent-loop/repo")
from agent import tasks  # noqa: E402

ISSUES = [
    {"number": 1, "labels": [{"name": "agent"}, {"name": "5"}], "body": "обычная агентская"},
    {"number": 2, "labels": [{"name": "agent"}, {"name": "pause"}], "body": "паузу поставил агент"},
    {"number": 3, "labels": [{"name": "todo"}], "body": "без агента"},
    {"number": 4, "labels": [], "body": "- [x] Агент\n\nчекбокс вместо метки"},
]


class FakeFj:
    def __init__(self, labels=None):
        self.added = []
        self.removed = []
        self.set = None
        self._labels = labels or {}

    def issues(self, owner, repo, state="open"):
        return ISSUES

    def add_labels(self, owner, repo, num, labels):
        self.added.append((f"{owner}/{repo}#{num}", labels))
        for i in ISSUES:                       # фейк живёт как настоящий: метка реально появляется
            if i["number"] == num:
                i["labels"] += [{"name": l} for l in labels if l not in [x["name"] for x in i["labels"]]]

    def remove_labels(self, owner, repo, num, labels):
        self.removed.append((f"{owner}/{repo}#{num}", labels))
        for i in ISSUES:
            if i["number"] == num:
                i["labels"] = [x for x in i["labels"] if x["name"] not in labels]

    def issue_labels(self, owner, repo, num):
        return list(self._labels.get(num, []))

    def set_labels(self, owner, repo, num, labels):
        self.set = (owner, repo, num, list(labels))
        self._labels[num] = list(labels)


cfg = {"forgejo": {"owner": "o", "repo": "r", "trigger_labels": ["agent"], "checkbox": "Агент"}}

on = FakeFj()
r = tasks.set_global_pause(cfg, True, on)
assert on.added == [("o/r#1", ["pause"]), ("o/r#4", ["pause"])], on.added
assert on.removed == []
assert r == {"paused": True, "changed": 2, "agent_tasks": 3}, r

off = FakeFj()
r2 = tasks.set_global_pause(cfg, False, off)
assert off.removed == [("o/r#1", ["pause"]), ("o/r#2", ["pause"]), ("o/r#4", ["pause"])], off.removed
assert off.added == []
assert r2 == {"paused": False, "changed": 3, "agent_tasks": 3}, r2

panel = FakeFj(labels={1: ["agent", "5"]})
tasks.client = lambda cfg: panel          # tag_task сам строит клиент — подставляем фейк
out = tasks.tag_task(cfg, "o/r", 1, None, None, "", None, True)
assert "pause" in out["labels"], out["labels"]
out2 = tasks.tag_task(cfg, "o/r", 1, None, None, "", None, False)
assert panel.set[3] == ["agent", "5"], panel.set      # приоритет и agent не тронуты

print("e2e25b OK: on вешает на агентские (метка+чекбокс), off снимает со всех агентских, "
      "пауза отдельной задачи через панель добавляется и снимается")
