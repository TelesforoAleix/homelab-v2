"""Durable proposals and delivery receipts, never an action executor.

All transactions share the mirror lock: a pull and its overtaken check cannot
race a tap or a collector. Content stays on the encrypted volume and in Telegram.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime

ACTIONS = {"start", "slot", "pause", "resume", "answer", "approve", "orchestrator"}
OPEN = {"proposed", "queued"}
OUTCOMES = {"done", "dropped", "failed"}
PUBLIC = ("id", "created", "action", "slice", "argument", "expect")


def snapshot(item):
    attention = item.get("attention")
    return {
        "state": item["state"],
        "paused": item["paused"],
        "attention": {k: attention[k] for k in ("kind", "since")} if attention else None,
    }


def validate(proposal, slices, owner_text=None):
    if not isinstance(proposal, dict) or set(proposal) != {"action", "id", "argument"}:
        raise ValueError("invalid proposal")
    action, target, argument = (proposal[k] for k in ("action", "id", "argument"))
    if not isinstance(action, str) or action not in ACTIONS or not isinstance(target, str):
        raise ValueError("invalid proposal")
    if action in {"answer", "slot", "orchestrator"}:
        if not isinstance(argument, str) or not argument.strip() or len(argument) > 16000:
            raise ValueError("argument required")
    elif argument is not None:
        raise ValueError("unexpected argument")
    if action == "orchestrator":
        if target != "development" and target not in {s["repo"] for s in slices}:
            raise ValueError("repository not found")
        return None
    item = next((s for s in slices if s["id"] == target), None)
    if item is None:
        raise ValueError("slice not found")
    attention = item.get("attention") or {}
    permitted = {
        "start": item["state"] == "ready",
        "slot": item["state"] == "ready",
        "pause": item["state"] != "accepted",
        "resume": item["state"] != "accepted",
        "answer": attention.get("kind") in {"question", "escalation"}
        and attention.get("answer") is None,
        "approve": attention.get("kind") == "merge"
        and attention.get("approved") is False
        and bool(attention.get("review")),
    }
    if not permitted[action]:
        raise ValueError("precondition no longer holds")
    if action == "answer" and owner_text is not None and argument not in owner_text:
        raise ValueError("answer must be verbatim from this message")
    return item


def confirmation(order, item, github_owner=None):
    text = f"Queue {order['action']} {order['slice']}?"
    if order["argument"] is not None:
        text += "\n\n" + order["argument"]
    if order["action"] == "approve":
        pr = item.get("pr")
        text += f"\n\nPR #{pr}"
        if github_owner and pr is not None:
            text += f"\nhttps://github.com/{github_owner}/{item['repo']}/pull/{pr}"
        text += "\n\nReview: " + item["attention"]["review"]["verdict"]
    return text


def changed(order, item):
    if item is None:
        return "slice removed"
    previous, current = order["expect"], snapshot(item)
    if previous["state"] != current["state"]:
        return f"state changed from {previous['state']} to {current['state']}"
    if previous["paused"] != current["paused"]:
        return "pause state changed"
    attention = item.get("attention") or {}
    if order["action"] == "answer" and attention.get("answer") is not None:
        return "answered from the orchestrator"
    if order["action"] == "approve" and attention.get("approved"):
        return "approved from the orchestrator"
    if previous["attention"] != current["attention"]:
        return "attention changed"
    try:
        validate(
            {"action": order["action"], "id": order["slice"], "argument": order["argument"]},
            [item],
        )
    except ValueError:
        return "action precondition changed"
    return None


class Orders:
    def __init__(self, planning):
        self.planning = planning
        self.path = planning.directory / "orders.json"

    def available(self):
        return self.planning.ready() and self.planning.config is not None

    def load(self):
        if not self.available():
            raise ValueError("orders unavailable")
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError("invalid order store")
        return data

    def save(self, data):
        # Persist before any acknowledgement; a failed replace leaves the old store.
        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as file:
            temporary = file.name
            try:
                os.fchmod(file.fileno(), 0o600)
                json.dump(data, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
                os.replace(temporary, self.path)
                descriptor = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def audit(self, order):
        # Slice names come from the clone, not arbitrary model or HTTP text.
        self.planning.log(
            f"order id={order['id']} action={order['action']} "
            f"slice={json.dumps(order['slice'])} status={order['status']}"
        )

    def slices(self):
        return json.loads(self.planning.read_file(self.planning.config.state_path))["slices"]

    def propose(self, proposal, owner_text, owner):
        with self.planning.lock:
            if self.planning.refresh():
                raise ValueError("planning unavailable")
            item = validate(proposal, self.slices(), owner_text)
            order = {
                "id": uuid.uuid4().hex,
                "created": datetime.now(UTC).isoformat(),
                "action": proposal["action"],
                "slice": proposal["id"],
                "argument": proposal["argument"],
                "expect": copy.deepcopy(snapshot(item)) if item else None,
                "status": "proposed",
                "owner": list(owner),
                "messages": [],
                "dirty": False,
            }
            order["confirmation"] = confirmation(order, item, self.planning.config.github_owner)
            data = self.load()
            data[order["id"]] = order
            self.save(data)
            self.audit(order)
            return order

    def attach(self, order_id, message_id, text):
        with self.planning.lock:
            data = self.load()
            order = data[order_id]
            order["messages"].append({"id": message_id, "text": text})
            order["dirty"] = order["status"] != "proposed"
            self.save(data)

    def reconcile(self):
        # Called inside refresh, under the planning lock, after every successful pull.
        data = self.load()
        slices = {s["id"]: s for s in self.slices()}
        updates = []
        for order in data.values():
            if order["status"] not in OPEN or order["expect"] is None:
                continue
            reason = changed(order, slices.get(order["slice"]))
            if reason:
                order.update(status="overtaken", detail=reason, dirty=True)
                updates.append(order)
        if updates:
            self.save(data)
            for order in updates:
                self.audit(order)

    def tap(self, order_id, yes, chat_id):
        with self.planning.lock:
            if not self.available():
                return "Orders need the data volume unlocked.", None
            # First report closed status, even when a later pull cannot succeed.
            data = self.load()
            order = data.get(order_id)
            if order is None or order["owner"][0] != chat_id:
                return "Unknown order.", None
            if order["status"] != "proposed":
                return "already " + order["status"], order["owner"]
            if yes and self.planning.refresh():
                return "Planning sync failed; try again shortly.", None
            data = self.load()
            order = data[order_id]
            if order["status"] != "proposed":
                return "already " + order["status"], order["owner"]
            order.update(status="queued" if yes else "cancelled", dirty=True)
            self.save(data)
            self.audit(order)
            return order["status"], order["owner"]

    def queued(self):
        with self.planning.lock:
            return [
                {key: order[key] for key in PUBLIC}
                for order in sorted(self.load().values(), key=lambda o: (o["created"], o["id"]))
                if order["status"] == "queued"
            ]

    def ack(self, order_id, body):
        if (
            not isinstance(body, dict)
            or set(body) != {"outcome", "detail"}
            or not isinstance(body["outcome"], str)
            or body["outcome"] not in OUTCOMES
            or not isinstance(body["detail"], str)
        ):
            return 400
        with self.planning.lock:
            data = self.load()
            order = data.get(order_id)
            if order is None:
                return 404
            # Receipts for closed (including overtaken) proposals are harmless.
            if order["status"] == "queued":
                order.update(status=body["outcome"], detail=body["detail"], dirty=True)
                self.save(data)
                self.audit(order)
            return 204

    def pending_edits(self):
        with self.planning.lock:
            return copy.deepcopy([o for o in self.load().values() if o["dirty"] and o["messages"]])

    def edited(self, order):
        with self.planning.lock:
            data = self.load()
            current = data[order["id"]]
            if (current["status"], current.get("detail")) == (order["status"], order.get("detail")):
                current["dirty"] = False
                self.save(data)


def status_text(order):
    status = order["status"]
    if status == "done":
        return "Done"
    if status in {"dropped", "failed", "overtaken"}:
        return status.capitalize() + ": " + order.get("detail", "")
    return status.capitalize()


def valid_id(value):
    return re.fullmatch(r"[0-9a-f]{32}", value) is not None
