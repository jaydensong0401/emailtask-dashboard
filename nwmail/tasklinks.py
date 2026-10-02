"""오늘/프로젝트에 중복 추출된 업무의 공통 ID. 문장 유사도만으로 합치지 않는다."""
from __future__ import annotations

import hashlib
import unicodedata

from .projects import person_key


def task_id(thread_key: str, kind: str, text: str) -> str:
    raw = f"{thread_key}\x1f{kind}\x1f{(text or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").split()).casefold()


def task_links(extractions: dict, me_names: list[str]) -> dict[str, dict]:
    """프로젝트 ID → 내 할 일 ID/스냅샷. 같은 대화 안의 명확한 1:1 대응만 연결.

    기존 결과는 동일 문구로, 새 결과는 my_actions.task_text의 정확한 참조로 연결한다.
    타인 담당, 서로 다른 기한, 중복 후보는 자동 연결하지 않는다.
    """
    mine = {person_key(n) for n in me_names if n}
    links = {}
    for key, data in extractions.items():
        if not isinstance(data, dict):
            continue
        tasks = [t for t in data.get("tasks") or [] if isinstance(t, dict) and t.get("text")]
        actions = [a for a in data.get("my_actions") or [] if isinstance(a, dict) and a.get("text")]
        candidates = []
        for a in actions:
            target = _text(a.get("task_text") or a["text"])
            matches = [t for t in tasks if _text(t["text"]) == target]
            if len(matches) != 1:
                continue
            t = matches[0]
            who = person_key(t.get("assignee") or "")
            if who and who not in mine:
                continue
            if a.get("deadline") and t.get("deadline") and a["deadline"] != t["deadline"]:
                continue
            candidates.append((a, t))
        for a, t in candidates:
            if sum(other is t for _, other in candidates) != 1:
                continue
            alias, canonical = task_id(key, "task", t["text"]), task_id(key, "me", a["text"])
            links[alias] = {"task_id": canonical, "text": a["text"], "kind": "me",
                            "thread_key": key, "urgency": a.get("urgency") or "medium",
                            "deadline": a.get("deadline") or t.get("deadline") or ""}
    return links
