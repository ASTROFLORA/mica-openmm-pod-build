"""MICA-side client for the Proactive Board.
Provides tiny helpers to create tasks and listen to SSE events.

Usage example:
    from src.mica.board_client import BoardClient
    bc = BoardClient("http://127.0.0.1:8091")
    tid = bc.create_task({"type": "echo", "payload": {"text": "hola"}})

Note: For streaming, use `listen_events()` which yields event dicts.
"""
from __future__ import annotations
import json
from typing import Any, Dict, Generator, Iterable

import requests


class BoardClient:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def create_task(self, task: Dict[str, Any]) -> str:
        r = requests.post(f"{self.base}/api/board/tasks", json=task, timeout=10)
        r.raise_for_status()
        return r.json()["task"]["id"]

    def list_tasks(self) -> Iterable[Dict[str, Any]]:
        r = requests.get(f"{self.base}/api/board/tasks", timeout=10)
        r.raise_for_status()
        return r.json().get("tasks", [])

    def post_task_event(self, task_id: str, event: Dict[str, Any]) -> None:
        r = requests.post(f"{self.base}/api/board/tasks/{task_id}/event", json=event, timeout=10)
        r.raise_for_status()

    def listen_events(self) -> Generator[Dict[str, Any], None, None]:
        with requests.get(f"{self.base}/api/board/events", stream=True, timeout=30) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if line is None:
                    continue
                if line.startswith("data: "):
                    try:
                        yield json.loads(line[6:])
                    except Exception:
                        continue
