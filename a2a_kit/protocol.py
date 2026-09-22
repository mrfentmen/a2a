"""A2A protocol logic (JSON-RPC binding, spec 0.3 shapes).

Implements: message/send, message/stream, tasks/get, tasks/cancel,
tasks/resubscribe, tasks/pushNotificationConfig/{set,get,list,delete}.

Domain behaviour lives in a SkillAgent (see errors.SkillAgent); this module only
speaks the protocol.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from .errors import A2AError, UpstreamError, agent_message, error, new_id, now_iso, validate_webhook_url
from .store import INTERRUPTED_STATES, TERMINAL_STATES, TaskStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .errors import SkillAgent


class A2AHandler:
    def __init__(self, store: TaskStore, agent: "SkillAgent") -> None:
        self.store = store
        self.agent = agent

    # -- dispatch ----------------------------------------------------------

    def handle(self, method: str, params: dict):
        """Return a result object, or a generator of result objects for streams."""
        if not isinstance(params, dict):
            raise error("invalid_params", "params must be an object")
        handlers = {
            "message/send": self.send_message,
            "message/stream": self.stream_message,
            "tasks/get": self.get_task,
            "tasks/cancel": self.cancel_task,
            "tasks/resubscribe": self.resubscribe,
            "tasks/pushNotificationConfig/set": self.push_set,
            "tasks/pushNotificationConfig/get": self.push_get,
            "tasks/pushNotificationConfig/list": self.push_list,
            "tasks/pushNotificationConfig/delete": self.push_delete,
        }
        handler = handlers.get(method)
        if handler is None:
            raise error("method_not_found", f"unknown method: {method}")
        return handler(params)

    # -- message/send ------------------------------------------------------

    def send_message(self, params: dict) -> dict:
        message = params.get("message")
        self._validate_message(message)
        configuration = params.get("configuration") or {}
        task = self._load_or_create_task(message)
        self.store.save_task(task)
        push_config = configuration.get("taskPushNotificationConfig")
        if push_config:
            self._set_push_config(task["id"], push_config)
        if configuration.get("returnImmediately"):
            threading.Thread(target=self._process, args=(task["id"],), daemon=True).start()
        else:
            self._process(task["id"])
        return self.store.get_task(task["id"])

    def stream_message(self, params: dict):
        message = params.get("message")
        self._validate_message(message)
        configuration = params.get("configuration") or {}
        task = self._load_or_create_task(message)
        self.store.save_task(task)
        push_config = configuration.get("taskPushNotificationConfig")
        if push_config:
            self._set_push_config(task["id"], push_config)
        return self._stream_new_task(task["id"])

    def _stream_new_task(self, task_id: str):
        task = self.store.get_task(task_id)
        yield task
        yield self._status_event(task, "working", final=False)
        self._process(task_id)
        final_task = self.store.get_task(task_id)
        for artifact in final_task.get("artifacts", [])[-1:]:
            yield self._artifact_event(final_task, artifact)
        yield self._status_event(
            final_task,
            final_task["status"]["state"],
            final=True,
            message=final_task["status"].get("message"),
        )

    # -- task lifecycle ----------------------------------------------------

    def get_task(self, params: dict) -> dict:
        task_id = params.get("id")
        if not task_id:
            raise error("invalid_params", "id is required")
        task = self.store.get_task(task_id)
        if task is None:
            raise error("task_not_found", f"task {task_id} not found")
        task = dict(task)
        history_length = params.get("historyLength")
        if history_length is not None:
            if not isinstance(history_length, int) or history_length < 0:
                raise error("invalid_params", "historyLength must be a non-negative integer")
            if history_length == 0:
                task.pop("history", None)
            else:
                task["history"] = task.get("history", [])[-history_length:]
        return task

    def cancel_task(self, params: dict) -> dict:
        task_id = params.get("id")
        if not task_id:
            raise error("invalid_params", "id is required")
        task = self.store.get_task(task_id)
        if task is None:
            raise error("task_not_found", f"task {task_id} not found")
        if task["status"]["state"] in TERMINAL_STATES:
            raise error("task_not_cancelable", f"task is already {task['status']['state']}")
        task["status"] = {
            "state": "canceled",
            "timestamp": now_iso(),
            "message": agent_message("Task canceled."),
        }
        self.store.save_task(task)
        return task

    def resubscribe(self, params: dict):
        task_id = params.get("id")
        if not task_id:
            raise error("invalid_params", "id is required")
        task = self.store.get_task(task_id)
        if task is None:
            raise error("task_not_found", f"task {task_id} not found")
        if task["status"]["state"] in TERMINAL_STATES | INTERRUPTED_STATES:
            raise error("unsupported_operation", "task is already in a terminal or interrupted state")
        return self._resubscribe_stream(task_id)

    def _resubscribe_stream(self, task_id: str):
        task = self.store.get_task(task_id)
        yield task
        deadline = time.time() + 120
        last_state = task["status"]["state"]
        while time.time() < deadline:
            time.sleep(0.25)
            task = self.store.get_task(task_id)
            state = task["status"]["state"]
            if state != last_state or state in TERMINAL_STATES | INTERRUPTED_STATES:
                yield self._status_event(task, state, final=state in TERMINAL_STATES | INTERRUPTED_STATES)
                last_state = state
            if state in TERMINAL_STATES | INTERRUPTED_STATES:
                return
        yield self._status_event(task, task["status"]["state"], final=False)

    # -- push config -------------------------------------------------------

    def push_set(self, params: dict) -> dict:
        task_id = params.get("taskId")
        config = params.get("pushNotificationConfig")
        if not task_id or not isinstance(config, dict):
            raise error("invalid_params", "taskId and pushNotificationConfig are required")
        if self.store.get_task(task_id) is None:
            raise error("task_not_found", f"task {task_id} not found")
        return {"taskId": task_id, "pushNotificationConfig": self._set_push_config(task_id, config)}

    def push_get(self, params: dict) -> dict:
        task_id = params.get("taskId")
        config_id = params.get("pushNotificationConfigId")
        if not task_id or not config_id:
            raise error("invalid_params", "taskId and pushNotificationConfigId are required")
        config = self.store.get_push_config(task_id, config_id)
        if config is None:
            raise error("task_not_found", "push notification config not found")
        return {"taskId": task_id, "pushNotificationConfig": config}

    def push_list(self, params: dict) -> dict:
        task_id = params.get("taskId")
        if not task_id:
            raise error("invalid_params", "taskId is required")
        if self.store.get_task(task_id) is None:
            raise error("task_not_found", f"task {task_id} not found")
        configs = [
            {"taskId": task_id, "pushNotificationConfig": cfg}
            for cfg in self.store.list_push_configs(task_id)
        ]
        return {"configs": configs}

    def push_delete(self, params: dict) -> dict:
        task_id = params.get("taskId")
        config_id = params.get("pushNotificationConfigId")
        if not task_id or not config_id:
            raise error("invalid_params", "taskId and pushNotificationConfigId are required")
        self.store.delete_push_config(task_id, config_id)
        return {}

    def _set_push_config(self, task_id: str, config: dict) -> dict:
        url = config.get("url")
        if not isinstance(url, str):
            raise error("invalid_params", "pushNotificationConfig.url is required")
        validate_webhook_url(url, self.agent.allow_private_webhooks,
                             allow_env=f"{self.agent.env_prefix}_ALLOW_PRIVATE_WEBHOOKS")
        token = config.get("token")
        if token is not None and not isinstance(token, str):
            raise error("invalid_params", "pushNotificationConfig.token must be a string")
        stored = {"id": config.get("id") or new_id(), "url": url}
        if token:
            stored["token"] = token
        authentication = config.get("authentication")
        if isinstance(authentication, dict):
            stored["authentication"] = authentication
        return self.store.set_push_config(task_id, stored)

    # -- internals ---------------------------------------------------------

    def _validate_message(self, message) -> None:
        if not isinstance(message, dict):
            raise error("invalid_params", "message is required")
        if not isinstance(message.get("messageId"), str) or not message["messageId"]:
            raise error("invalid_params", "message.messageId is required")
        if message.get("role") != "user":
            raise error("invalid_params", "message.role must be 'user'")
        parts = message.get("parts")
        if not isinstance(parts, list) or not parts:
            raise error("invalid_params", "message.parts must be a non-empty array")
        for part in parts:
            kind = part.get("kind") if isinstance(part, dict) else None
            if kind == "text" and not isinstance(part.get("text"), str):
                raise error("invalid_params", "text part requires a string 'text'")
            elif kind == "data" and not isinstance(part.get("data"), dict):
                raise error("invalid_params", "data part requires an object 'data'")
            elif kind not in ("text", "data", "file"):
                raise error("content_type_not_supported", f"unsupported part kind: {kind!r}")

    def _load_or_create_task(self, message: dict) -> dict:
        task_id = message.get("taskId")
        if task_id:
            task = self.store.get_task(task_id)
            if task is None:
                raise error("task_not_found", f"task {task_id} not found")
            if message.get("contextId") and message["contextId"] != task["contextId"]:
                raise error("invalid_params", "message.contextId does not match the referenced task")
            if task["status"]["state"] in TERMINAL_STATES:
                raise error("unsupported_operation", f"task is already {task['status']['state']}")
            task.setdefault("history", []).append(message)
            task["status"] = {"state": "submitted", "timestamp": now_iso()}
            return task
        context_id = message.get("contextId") or new_id()
        return {
            "kind": "task",
            "id": new_id(),
            "contextId": context_id,
            "status": {"state": "submitted", "timestamp": now_iso()},
            "history": [message],
            "artifacts": [],
            "metadata": {},
        }

    def _process(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if task is None:
            return
        try:
            parsed = self.agent.parse(task["history"][-1])
            pending = task.get("metadata", {}).get("pending") or {}
            request = self._merge(pending, parsed)
            task.setdefault("metadata", {})["skill"] = request["skill"]
            task["status"] = {"state": "working", "timestamp": now_iso()}
            self.store.save_task(task)

            result = self.agent.run(request)

            message = agent_message(result["message"])
            task.setdefault("history", []).append(message)
            task["status"] = {"state": result["final_state"], "timestamp": now_iso(), "message": message}
            if result["artifact"] is not None:
                task.setdefault("artifacts", []).append(
                    {
                        "artifactId": new_id(),
                        "name": request["skill"],
                        "parts": [{"kind": "data", "data": result["artifact"]}],
                    }
                )
            if result.get("watch"):
                task["metadata"]["watch"] = result["watch"]
                task["metadata"]["last_observed"] = result["watch"].get("observed")
            if result["final_state"] == "input-required":
                task["metadata"]["pending"] = request
            else:
                task["metadata"].pop("pending", None)
            self.store.save_task(task)
        except UpstreamError as exc:
            self._fail(task, f"The public dataset is unreachable right now: {exc}")
        except Exception as exc:  # keep the task store consistent no matter what
            self._fail(task, f"Internal error while handling the request: {exc}")

    def _merge(self, pending: dict, parsed: dict) -> dict:
        """Pick the skill for this turn and merge parameters.

        A message that explicitly names a skill wins. Otherwise, if we are mid-way
        through an input-required exchange, the pending skill wins and only the new
        parameters are merged in - a bare answer like "site 55450" continues the
        question instead of starting a different skill.
        """
        if parsed.get("explicit") or not pending.get("skill"):
            skill = parsed["skill"]
        else:
            skill = pending["skill"]
        params = {**(pending.get("params") or {}), **(parsed.get("params") or {})}
        missing = self.agent.missing(skill, params)
        return {"skill": skill, "params": params, "missing": missing}

    def _fail(self, task: dict, text: str) -> None:
        message = agent_message(text)
        task.setdefault("history", []).append(message)
        task["status"] = {"state": "failed", "timestamp": now_iso(), "message": message}
        self.store.save_task(task)

    @staticmethod
    def _status_event(task: dict, state: str, final: bool, message: dict | None = None) -> dict:
        status = {"state": state, "timestamp": now_iso()}
        if message:
            status["message"] = message
        return {
            "kind": "status-update",
            "taskId": task["id"],
            "contextId": task["contextId"],
            "status": status,
            "final": final,
        }

    @staticmethod
    def _artifact_event(task: dict, artifact: dict) -> dict:
        return {
            "kind": "artifact-update",
            "taskId": task["id"],
            "contextId": task["contextId"],
            "artifact": artifact,
            "append": False,
            "lastChunk": True,
        }


__all__ = ["A2AHandler", "A2AError"]
