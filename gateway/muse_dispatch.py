"""Dispatch retained XPhone events into role-specific Muse side chats."""

import json
import logging
import os
import subprocess
import threading
import time
import traceback
from pathlib import Path

from muse_context import MuseContext, tool_data
from muse_delivery import batch_id, delivery_status
from muse_memory import MuseMemory

log = logging.getLogger("xphone-gateway")
EVENT_LEASE_SECONDS = 90
MUSE_WAIT_SECONDS = 60
DELIVERY_WAIT_SECONDS = 120
DELEGATION_CHECK_INTERVAL = 20


class MuseCliError(Exception):
    def __init__(self, operation: str, returncode: int | None = None, reason: str = "exit"):
        super().__init__(operation)
        self.operation = operation
        self.returncode = returncode
        self.reason = reason


class AwaitingDelegations(Exception):
    pass


class FailedDelegations(Exception):
    pass


class LeaseRenewal:
    def __init__(self, mcp_call, event_id: str, lease_id: str, cancel: threading.Event | None):
        self.mcp_call, self.event_id, self.lease_id = mcp_call, event_id, lease_id
        self.cancel = cancel
        self.stop_signal = threading.Event()
        self.lost = False
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self) -> None:
        next_at = time.monotonic() + 30
        failures = 0
        while not self.stop_signal.wait(1):
            if self.cancel and self.cancel.is_set():
                return
            if time.monotonic() < next_at:
                continue
            try:
                self.mcp_call("xphone.renew_event", {"eventId": self.event_id, "leaseId": self.lease_id})
                next_at = time.monotonic() + 30
                failures = 0
            except Exception:
                failures += 1
                if failures >= 3:
                    self.lost = True
                    log.warning("muse lease renewal lost: event=%s", self.event_id)
                    return
                next_at = time.monotonic() + 5
                log.warning("muse lease renewal retry: event=%s attempt=%s", self.event_id, failures)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop_signal.set()
        self.thread.join()


class MuseDispatch:
    def __init__(self, inbox: str, state_path: str):
        self.inbox, self.state_path = Path(inbox), Path(state_path)
        self.state_lock = threading.RLock()
        self.session_locks = {}
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {
            "sessions": {}, "events": {},
        }
        self.memory = MuseMemory(str(self.state_path.with_name("muse-memory.json")))
        self.context = MuseContext()
        self.state.setdefault("promptVersions", {})

    def save(self) -> None:
        with self.state_lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)

    def cli(self, *args: str) -> dict:
        env = os.environ.copy()
        proxy = env.get("wss_proxy") or env.get("WSS_PROXY") or env.get("https_proxy") or env.get("HTTPS_PROXY")
        if proxy:
            env["wss_proxy"] = proxy
        cli_bin = env.get("XPHONE_MUSE_CLI")
        if not cli_bin or not os.path.exists(cli_bin):
            cli_bin = next((path for path in [os.path.expanduser("~/.local/bin/muse-cli"),
                                              "/home/hatch/.local/bin/muse-cli"] if os.path.exists(path)), "muse-cli")
        try:
            result = subprocess.run([cli_bin, *args], capture_output=True, text=True, timeout=90, env=env)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MuseCliError(args[0], reason=type(error).__name__) from error
        if result.returncode:
            raise MuseCliError(args[0], result.returncode)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise MuseCliError(args[0], reason="invalid-json") from error

    def session_key(self, record: dict) -> tuple[str, bool]:
        route = record["route"]
        scope = f"{record['packId']}:{record['playthroughId']}"
        if route["role"] == "os-agent" and record["decrypted"].get("type") != "os.user_message":
            return f"{scope}:ops:{record['id']}", True
        return f"{scope}:{route['roleId']}", False

    def session_lock(self, key: str):
        with self.state_lock:
            return self.session_locks.setdefault(key, threading.RLock())

    def session_id(self, key: str) -> str:
        with self.session_lock(key):
            sessions = self.state["sessions"]
            if key not in sessions:
                result = self.cli("session-start", "--title", f"XPhone {key}")
                sessions[key] = result.get("session_id") or result["id"]
                self.save()
                log.info("muse session created: key=%s", key)
            return sessions[key]

    def rejection_reason(self, batch: str | None) -> str | None:
        if not batch:
            return None
        receipt = self.inbox / f"receipt.{batch}.json"
        try:
            return json.loads(receipt.read_text(encoding="utf-8"))["decrypted"].get("reason")
        except FileNotFoundError:
            return None

    def on_receipt(self, batch: str) -> None:
        for attempt in self.state["events"].copy().values():
            if batch == attempt.get("phase1Batch") or batch == attempt.get("batchId") or any(
                record.get("batchId") == batch for path in attempt.get("delegations", [])
                if (record := self.state.get("delegations", {}).get(path))
            ):
                attempt["nextAt"] = 0
        self.save()

    def finish(self, item: dict, record: dict, mcp_call, lease_id: str, batches: list[str],
               reply_text: str, source: str, key: str, temporary: bool, outcome: str | None = None) -> None:
        event_id = record["id"]
        if record["route"]["role"] == "os-agent":
            if outcome is None:
                outcome = self.delegation_outcome(event_id, self.state["events"][event_id], mcp_call)
            if outcome == "pending":
                raise AwaitingDelegations("waiting for delegated actions to be applied")
            if outcome in ("failed", "rejected"):
                raise FailedDelegations(outcome)
        self.memory.commit(record["packId"], record["playthroughId"], event_id,
                           record["route"], batches, reply_text[:500], source)
        mcp_call("xphone.finish_event", {"eventId": event_id, "leaseId": lease_id, "status": "completed"})
        mcp_call("xphone.ack_inbox", {"cursor": item["cursor"]})
        (self.inbox / f"{event_id}.json").unlink(missing_ok=True)
        self.state["events"].pop(event_id, None)
        self.save()
        if temporary and key in self.state["sessions"]:
            try:
                self.cli("session-delete", self.state["sessions"][key])
                del self.state["sessions"][key]
                self.state["promptVersions"].pop(key, None)
                self.save()
            except Exception:
                log.warning("muse temporary session cleanup failed: event=%s", event_id)
        log.info("muse event completed: event=%s cursor=%s batches=%s", event_id, item["cursor"], batches)

    def wait_delivery(self, mcp_call, batch: str, cancel: threading.Event | None,
                      renewal: LeaseRenewal) -> str:
        deadline = time.monotonic() + DELIVERY_WAIT_SECONDS
        while True:
            if renewal.lost or (cancel and cancel.is_set()):
                raise RuntimeError("lease or scope lost")
            status = delivery_status(mcp_call, batch)
            if status in ("applied", "rejected") or time.monotonic() >= deadline:
                return status
            if cancel:
                cancel.wait(2)
            else:
                time.sleep(2)

    def delegation_outcome(self, event_id: str, attempt: dict, mcp_call) -> str:
        runtime = tool_data(mcp_call, "xphone.get_event_status", {"eventId": event_id}).get("delegationStatus")
        local = self.legacy_delegation_outcome(attempt, mcp_call, runtime not in (None, "none"))
        for outcome in ("pending", "failed", "rejected"):
            if outcome in (runtime, local):
                return outcome
        return "ready" if runtime == "applied" or local == "ready" else "missing"

    def phase2_outcome(self, event_id: str, attempt: dict, mcp_call) -> str:
        outcome = self.delegation_outcome(event_id, attempt, mcp_call)
        if outcome == "missing" and attempt.get("phase2NoLinkage"):
            return "ready"
        if outcome == "failed":
            raise FailedDelegations(outcome)
        return outcome

    def legacy_delegation_outcome(self, attempt: dict, mcp_call, runtime_tracks_jobs: bool) -> str:
        paths = attempt.get("delegations") or []
        if not paths:
            return "missing"
        records = [self.state.get("delegations", {}).get(path) for path in paths]
        if any(record is None for record in records):
            return "missing"
        records = [record for record in records if not record.get("superseded")
                   and (not runtime_tracks_jobs or not record.get("jobId"))]
        if not records:
            return "missing"
        statuses = [delivery_status(mcp_call, record["batchId"]) for record in records]
        if any(record.get("deliveryStatus") != status for record, status in zip(records, statuses)):
            for record, status in zip(records, statuses):
                record["deliveryStatus"] = status
            self.save()
        if all(status == "applied" for status in statuses):
            return "ready"
        return "rejected" if all(status in ("applied", "rejected") for status in statuses) else "pending"

    def dispatch(self, items: list, mcp_call, playthrough_id: str, pack_id: str | None = None,
                 cancel: threading.Event | None = None) -> None:
        for item in sorted(items, key=lambda value: value["cursor"]):
            if cancel and cancel.is_set():
                return
            event_id = item["envelope"]["id"]
            path = self.inbox / f"{event_id}.json"
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            if record["playthroughId"] != playthrough_id or record["packId"] != pack_id:
                return
            if item["envelope"]["kind"] != "event":
                mcp_call("xphone.ack_inbox", {"cursor": item["cursor"]})
                path.unlink(missing_ok=True)
                continue
            attempt = self.state["events"].setdefault(event_id, {"failures": 0, "nextAt": 0})
            if time.time() < attempt["nextAt"]:
                return
            is_journal = (record["decrypted"].get("type") == "journal.main_record.requested" or
                          record.get("route", {}).get("surfaceId") == "journal.main_record")
            if not attempt.get("phase2Started") and is_journal:
                first_batch = batch_id(record["packId"], playthrough_id, event_id, 0)
                if delivery_status(mcp_call, first_batch) == "applied":
                    post_step = max(1, attempt.get("step", 1))
                    post_batch = attempt.get("batchId") or batch_id(record["packId"], playthrough_id, event_id, post_step)
                    attempt.update({"phase2Started": True, "phase1Batch": first_batch, "step": post_step, "batchId": post_batch})
                    self.save()
            status = tool_data(mcp_call, "xphone.get_event_status", {"eventId": event_id})
            if status["status"] == "completed":
                mcp_call("xphone.ack_inbox", {"cursor": item["cursor"]})
                path.unlink(missing_ok=True)
                self.state["events"].pop(event_id, None)
                self.save()
                continue
            if status["status"] == "failed" and attempt.get("delegationFailed"):
                return
            lease_current = (status["status"] == "running" and
                             status["updatedAt"] + EVENT_LEASE_SECONDS * 1000 > time.time() * 1000)
            resume = lease_current and attempt.get("waitingDelegations") and attempt.get("leaseId")
            if lease_current and not resume:
                attempt["nextAt"] = status["updatedAt"] / 1000 + EVENT_LEASE_SECONDS + 1
                self.save()
                return
            if resume:
                lease_id = attempt["leaseId"]
            else:
                started = tool_data(mcp_call, "xphone.start_event", {"eventId": event_id})
                if not started.get("claimed"):
                    return
                lease_id = started["leaseId"]
                attempt["leaseId"] = lease_id
                self.save()
            try:
                if resume:
                    mcp_call("xphone.renew_event", {"eventId": event_id, "leaseId": lease_id})
                with LeaseRenewal(mcp_call, event_id, lease_id, cancel) as renewal:
                    event_type = record["decrypted"].get("type")
                    key, temporary = self.session_key(record)
                    if attempt.get("phase2Started"):
                        outcome = self.phase2_outcome(event_id, attempt, mcp_call)
                        if outcome == "ready":
                            self.finish(item, record, mcp_call, lease_id, [attempt["phase1Batch"]],
                                        attempt.get("replyText", ""), "reply_text", key, temporary, outcome)
                            continue
                        if outcome == "pending":
                            raise AwaitingDelegations("waiting for delegated actions to be applied")
                        sid = self.session_id(key)
                        post_batch = attempt.get("batchId") or batch_id(record["packId"], playthrough_id, event_id, attempt.get("step", 1))
                        post_prompt = self.context.post_journal_prompt(
                            record, record["packId"], playthrough_id, post_batch,
                            self.memory.view(record["packId"], playthrough_id, record["route"]),
                            journal_summary=attempt.get("replyText", "")[:500])
                        if outcome == "rejected":
                            post_prompt += "\n\n已有派工被客户端拒收。请查看反馈，为同一目标新建任务文档并重新交办。"
                        post_result = self.cli("send", post_prompt, "--thread", sid, "--wait", str(MUSE_WAIT_SECONDS))
                        if post_result.get("sent") is not True:
                            raise MuseCliError("send", reason="not-sent")
                        post_reply = str((post_result.get("reply") or {}).get("text") or "")
                        if post_reply:
                            attempt["replyText"] = f"{attempt.get('replyText', '')}\n\n[Post-Journal]: {post_reply}"[:4000]
                        attempt["phase2NoLinkage"] = "无需联动" in post_reply
                        self.save()
                        outcome = self.phase2_outcome(event_id, attempt, mcp_call)
                        if outcome == "ready":
                            self.finish(item, record, mcp_call, lease_id, [attempt["phase1Batch"]],
                                        attempt.get("replyText", ""), "reply_text", key, temporary, outcome)
                            continue
                        if outcome == "pending":
                            raise AwaitingDelegations("waiting for delegated actions to be applied")
                        raise RuntimeError("post-journal 必须调用 delegate_task 或明确回复无需联动")
                    if event_type in ("app.foreground", "app.background"):
                        self.finish(item, record, mcp_call, lease_id, [], "", "lifecycle", key, False)
                        continue
                    step = attempt.get("step", 0)
                    batch = batch_id(record["packId"], playthrough_id, event_id, step)
                    attempt.update({"playthroughId": playthrough_id, "packId": record["packId"],
                                    "batchId": batch, "sessionKey": key, "step": step})
                    self.save()
                    current = delivery_status(mcp_call, batch)
                    if current == "rejected":
                        attempt.update({"step": step + 1, "rejectedBatchId": batch,
                                        "museStarted": False, "museSent": False})
                        self.save()
                        raise RuntimeError("delivery rejected")
                    session_id = None
                    if current == "unknown" and not attempt.get("museStarted"):
                        session_id = self.session_id(key)
                        prompt = self.context.prompt(record, record["packId"], playthrough_id, batch,
                                                     self.memory.view(record["packId"], playthrough_id,
                                                                      record["route"]),
                                                     self.state["promptVersions"].get(key) != self.context.version(),
                                                     rejected_batch=attempt.get("rejectedBatchId"),
                                                     rejected_reason=self.rejection_reason(attempt.get("rejectedBatchId")))
                        attempt["museStarted"] = True
                        self.save()
                        result = self.cli("send", prompt, "--thread", session_id, "--wait", str(MUSE_WAIT_SECONDS))
                        if result.get("sent") is not True:
                            raise MuseCliError("send", reason="not-sent")
                        self.state["promptVersions"][key] = self.context.version()
                        reply_text = str((result.get("reply") or {}).get("text") or "")
                        attempt["replyText"] = reply_text[:4000]
                        attempt["museSent"] = True
                        self.save()
                    else:
                        reply_text = attempt.get("replyText", "")
                    if record["route"]["role"] == "os-agent":
                        current = delivery_status(mcp_call, batch)
                        if current == "unknown":
                            if event_type == "os.user_message":
                                raise AwaitingDelegations("waiting for the OS reply to be submitted")
                            outcome = self.delegation_outcome(event_id, attempt, mcp_call)
                            if outcome != "missing" or "无需联动" in reply_text:
                                self.finish(item, record, mcp_call, lease_id, [], reply_text,
                                            "reply_text" if reply_text else "delegation_only", key, temporary, outcome)
                                continue
                    current = self.wait_delivery(mcp_call, batch, cancel, renewal)
                    if current == "rejected":
                        attempt.update({"step": step + 1, "rejectedBatchId": batch,
                                        "museStarted": False, "museSent": False})
                        self.save()
                        raise RuntimeError("delivery rejected")
                    if current != "applied":
                        raise RuntimeError(f"delivery {current}")
                    if renewal.lost or (cancel and cancel.is_set()):
                        raise RuntimeError("lease or scope lost")

                    batches_completed = [batch]
                    is_journal = (event_type == "journal.main_record.requested" or
                                  record.get("route", {}).get("surfaceId") == "journal.main_record")
                    if is_journal and step == 0:
                        post_step = 1
                        post_batch = batch_id(record["packId"], playthrough_id, event_id, post_step)
                        attempt.update({"step": post_step, "batchId": post_batch})
                        attempt.update({"phase2Started": True, "phase1Batch": batch})
                        self.save()
                        post_status = delivery_status(mcp_call, post_batch)
                        if post_status == "unknown":
                            sid = session_id or self.session_id(key)
                            post_prompt = self.context.post_journal_prompt(
                                record, record["packId"], playthrough_id, post_batch,
                                self.memory.view(record["packId"], playthrough_id, record["route"]),
                                journal_summary=reply_text[:500]
                            )
                            result = self.cli("send", post_prompt, "--thread", sid, "--wait", str(MUSE_WAIT_SECONDS))
                            if result.get("sent") is not True:
                                raise MuseCliError("send", reason="not-sent")
                            post_reply = str((result.get("reply") or {}).get("text") or "")
                            if post_reply:
                                reply_text = f"{reply_text}\n\n[Post-Journal]: {post_reply}"
                            attempt["replyText"] = reply_text[:4000]
                            attempt["phase2NoLinkage"] = "无需联动" in post_reply
                            self.save()
                        outcome = self.phase2_outcome(event_id, attempt, mcp_call)
                        if outcome == "pending":
                            raise AwaitingDelegations("waiting for delegated actions to be applied")
                        if outcome != "ready":
                            raise RuntimeError("post-journal 必须调用 delegate_task 或明确回复无需联动")

                    if not reply_text:
                        try:
                            sid = session_id or self.state["sessions"].get(key)
                            if sid:
                                history = self.cli("history", "--thread", sid, "--limit", "5")
                                items = history if isinstance(history, list) else history.get("messages", []) if isinstance(history, dict) else []
                                for ev in reversed(items):
                                    if ev.get("text") and ("assistant" in str(ev.get("role", "")).lower()):
                                        reply_text = str(ev["text"])
                                        break
                        except Exception as err:
                            log.warning("muse history fallback failed: %s", err)
                    source = "reply_text" if reply_text else "delivery_only"
                    self.finish(item, record, mcp_call, lease_id, batches_completed, reply_text,
                                source, key, temporary, outcome if is_journal and step == 0 else None)
                    continue
            except AwaitingDelegations as error:
                attempt.update(waitingDelegations=True, nextAt=time.time() + DELEGATION_CHECK_INTERVAL)
                self.save()
                log.info("muse dispatch waiting: event=%s reason=%s", event_id, error)
                return
            except FailedDelegations as error:
                mcp_call("xphone.finish_event", {"eventId": event_id, "leaseId": lease_id, "status": "failed"})
                attempt.update(delegationFailed=True, waitingDelegations=False)
                self.save()
                log.warning("muse delegation failed: event=%s status=%s", event_id, error)
                return
            except Exception as error:
                failures = attempt["failures"] + 1
                delay = min(30 * 2 ** min(failures - 1, 5), 600)
                attempt.update({"failures": failures, "nextAt": time.time() + delay})
                self.save()
                log.warning("muse dispatch deferred: event=%s reason=%s error=%s retry_in=%ss\n%s",
                            event_id, type(error).__name__, str(error), delay, traceback.format_exc())
                return
