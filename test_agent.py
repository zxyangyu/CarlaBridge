"""Hardcoded test agent for CarlaBridge — end-to-end smoke without UrbanAgent.

Triggers (design §8.1):
  * First state_snapshot → PATROL each UAV around its origin (loop=true).
  * New incident → UGV_GOTO toward (position + UGV_GOTO_OFFSET).
  * UGV within EXTINGUISH_RADIUS_M of a responding incident → UGV_EXTINGUISH.
  * EXTINGUISH command_status:completed → UGV_RTL.
  * scenario_event:reset → drop local state, re-PATROL on next snapshot.

Run: ``python test_agent.py [--url URL] [-v] [--no-extinguish]``.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import signal
from typing import Any

import socketio  # python-socketio[asyncio_client]


DEFAULT_URL = "http://127.0.0.1:5000"
NAMESPACE = "/agent"
AGENT_ID = "test-agent"
PROTOCOL_VERSION = "1.0"

UAV_IDS = ("UAV-01",)
UGV_ID = "UGV-01"

PATROL_DELTAS = ((5.0, 0.0, 0.0), (5.0, 5.0, 0.0), (0.0, 5.0, 0.0))
PATROL_CRUISE_SPEED = 8.0

UGV_TARGET_SPEED_KMH = 25.0
UGV_GOTO_OFFSET = (0.0, 3.0, 0.0)
EXTINGUISH_RADIUS_M = 5.0


def _pose_xyz(v: Any) -> tuple[float, float, float] | None:
    """Snapshot pose is either [x,y,z] (vehicles/uavs) or {x,y,z} (incidents)."""
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        return float(v[0]), float(v[1]), float(v[2])
    if isinstance(v, dict) and all(k in v for k in ("x", "y", "z")):
        return float(v["x"]), float(v["y"]), float(v["z"])
    return None


def _dist(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2)


def _unwrap(data: Any) -> dict:
    """Protocol v1.0 §3.2 — Agent receives envelope-wrapped events from Bridge.

    Return the inner ``payload`` dict if present, else the raw dict (so the
    reference client stays robust against pre-v1.0 bridges during rollout).
    """
    if isinstance(data, dict):
        inner = data.get("payload")
        if isinstance(inner, dict):
            return inner
        return data
    return {}


def _envelope(event_type: str, payload: dict) -> dict:
    """Protocol §3.1 envelope for Agent → Bridge events."""
    import time
    import uuid

    return {
        "version": PROTOCOL_VERSION,
        "msg_id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": time.time(),
        "frame": None,
        "sim_time": None,
        "sender": "agent",
        "payload": payload,
    }


class TestAgent:
    def __init__(self, sio: socketio.AsyncClient, *, no_extinguish: bool, verbose: bool):
        self.sio = sio
        self.no_extinguish = no_extinguish
        self.verbose = verbose
        self.bridge_session_id: str | None = None
        self.run_id: int | None = None
        self.seen_first_snapshot = False
        self.responding: dict[str, str] = {}        # incident_id → "going" | "extinguishing"
        self.extinguish_cmds: dict[str, str] = {}   # cmd_id → incident_id
        self.shutdown_evt = asyncio.Event()
        self.cmd_seq = 0

    def _log(self, msg: str) -> None:
        print(msg, flush=True)

    def _dbg(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _next_id(self, kind: str) -> str:
        self.cmd_seq += 1
        return f"ta-{kind.lower()}-{self.cmd_seq:04d}"

    async def _call_cmd(self, kind: str, target: str, params: dict | None = None) -> dict | None:
        cmd_id = self._next_id(kind)
        cmd: dict = {"id": cmd_id, "kind": kind, "target": target}
        if params:
            cmd["params"] = params
        envelope = _envelope("agent.command", cmd)
        # Retry BadNamespace briefly (call may land during the connect handshake).
        for _ in range(3):
            try:
                ack = await self.sio.call("agent.command", envelope,
                                          namespace=NAMESPACE, timeout=2.0)
                break
            except socketio.exceptions.BadNamespaceError:
                await asyncio.sleep(0.05)
            except asyncio.TimeoutError:
                self._log(f"[send] {cmd_id} {kind} {target} → TIMEOUT")
                return None
        else:
            self._log(f"[send] {cmd_id} {kind} {target} → namespace error")
            return None
        ack = ack or {}
        if ack.get("status") == "accepted":
            self._log(f"[send] {cmd_id} {kind} {target} → accepted")
            ack["_cmd_id"] = cmd_id
            return ack
        self._log(f"[send] {cmd_id} {kind} {target} → rejected "
                  f"reason={ack.get('reason')} detail={ack.get('detail')}")
        return None

    async def _patrol_all_uavs(self, origins: dict[str, tuple[float, float, float]]) -> None:
        for uav in UAV_IDS:
            o = origins.get(uav)
            if o is None:
                continue
            path = [{"x": o[0]+dx, "y": o[1]+dy, "z": o[2]+dz}
                    for dx, dy, dz in PATROL_DELTAS]
            await self._call_cmd("UAV_PATROL", uav,
                                 {"path": path, "cruise_speed": PATROL_CRUISE_SPEED, "loop": True})

    async def _spawn_extinguish(self, incident_id: str) -> None:
        ack = await self._call_cmd("UGV_EXTINGUISH", UGV_ID, {"incident_id": incident_id})
        if ack is not None:
            self.extinguish_cmds[ack["_cmd_id"]] = incident_id

    # ---- event handlers --------------------------------------------------

    async def on_snapshot(self, snap: dict) -> None:
        snap = _unwrap(snap)
        self._dbg(f"[recv] state_snapshot sim_time={snap.get('sim_time')} "
                  f"incidents={len(snap.get('incidents', []))} "
                  f"in_flight={len(snap.get('in_flight_commands', []))}")
        ses = snap.get("bridge_session_id")
        if ses and ses != self.bridge_session_id:
            self._log(f"[bridge] session_id → {ses}")
            self.bridge_session_id = ses
        run = snap.get("run_id")
        if isinstance(run, int) and run != self.run_id:
            if self.run_id is not None:
                self._log(f"[bridge] run_id {self.run_id} → {run} (reset)")
            self.run_id = run

        if not self.seen_first_snapshot:
            self.seen_first_snapshot = True
            origins: dict[str, tuple[float, float, float]] = {}
            for u in snap.get("uavs", []):
                p = _pose_xyz(u.get("pose"))
                if u.get("id") in UAV_IDS and p is not None:
                    origins[u["id"]] = p
            # Hand off to a separate task so the namespace dispatcher
            # completes its tick before we issue outgoing calls (python-
            # socketio marks the namespace "connected" right after the
            # callback returns; nested call() during the callback races).
            asyncio.get_event_loop().create_task(self._patrol_all_uavs(origins))

        ugv_pose: tuple[float, float, float] | None = None
        for v in snap.get("vehicles", []):
            if v.get("id") == UGV_ID:
                ugv_pose = _pose_xyz(v.get("pose"))
                break

        for inc in snap.get("incidents", []):
            iid = inc.get("id")
            inc_pose = _pose_xyz(inc.get("position"))
            if not isinstance(iid, str) or inc_pose is None:
                continue
            state = self.responding.get(iid)
            if state is None:
                dest = (inc_pose[0]+UGV_GOTO_OFFSET[0],
                        inc_pose[1]+UGV_GOTO_OFFSET[1],
                        inc_pose[2]+UGV_GOTO_OFFSET[2])
                self.responding[iid] = "going"
                asyncio.get_event_loop().create_task(self._call_cmd("UGV_GOTO", UGV_ID, {
                    "dest": {"x": dest[0], "y": dest[1], "z": dest[2]},
                    "target_speed": UGV_TARGET_SPEED_KMH,
                }))
            elif state == "going" and not self.no_extinguish and ugv_pose is not None:
                if _dist(ugv_pose, inc_pose) <= EXTINGUISH_RADIUS_M:
                    self.responding[iid] = "extinguishing"
                    asyncio.get_event_loop().create_task(
                        self._spawn_extinguish(iid)
                    )

    async def on_command_status(self, payload: dict) -> None:
        payload = _unwrap(payload)
        cmd_id = payload.get("cmd_id")
        status = payload.get("status")
        reason = payload.get("reason")
        self._log(f"[recv] command_status {cmd_id} {status}"
                  + (f" reason={reason}" if reason else ""))
        if cmd_id in self.extinguish_cmds and status == "completed":
            self.responding.pop(self.extinguish_cmds.pop(cmd_id), None)
            await self._call_cmd("UGV_RTL", UGV_ID)

    async def on_scenario_event(self, payload: dict) -> None:
        payload = _unwrap(payload)
        event = payload.get("event")
        self._log(f"[recv] scenario_event {event} run_id={payload.get('run_id')}")
        if event == "reset":
            self.responding.clear()
            self.extinguish_cmds.clear()
            self.seen_first_snapshot = False  # re-PATROL on next snapshot

    async def on_event_log(self, payload: dict) -> None:
        payload = _unwrap(payload)
        self._dbg(f"[recv] event_log {payload.get('severity')} "
                  f"{payload.get('source')} {payload.get('message')}"
                  + (f" cmd_id={payload['cmd_id']}" if payload.get('cmd_id') else ""))


async def main(args: argparse.Namespace) -> int:
    sio = socketio.AsyncClient(reconnection=True)
    agent = TestAgent(sio, no_extinguish=args.no_extinguish, verbose=args.verbose)

    @sio.event(namespace=NAMESPACE)
    async def connect():  # noqa: ANN201
        print(f"[conn] connected to {args.url}{NAMESPACE}", flush=True)
        try:
            ack = await sio.call(
                "hello",
                {"agent_id": AGENT_ID, "version": PROTOCOL_VERSION},
                namespace=NAMESPACE, timeout=2.0,
            )
            print(f"[conn] hello ack: {ack}", flush=True)
            if isinstance(ack, dict):
                agent.bridge_session_id = ack.get("bridge_session_id")
                ver = ack.get("version")
                if ver and ver.split(".", 1)[0] != PROTOCOL_VERSION.split(".", 1)[0]:
                    print(
                        f"[conn] WARNING: protocol major mismatch agent={PROTOCOL_VERSION} "
                        f"bridge={ver} (continuing per protocol §11.1)",
                        flush=True,
                    )
        except Exception as e:
            print(f"[conn] hello failed: {e}", flush=True)

    @sio.event(namespace=NAMESPACE)
    async def disconnect():  # noqa: ANN201
        print("[conn] disconnected", flush=True)

    sio.on("state_snapshot", agent.on_snapshot, namespace=NAMESPACE)
    sio.on("command_status", agent.on_command_status, namespace=NAMESPACE)
    sio.on("scenario_event", agent.on_scenario_event, namespace=NAMESPACE)
    sio.on("event_log", agent.on_event_log, namespace=NAMESPACE)

    try:
        await sio.connect(args.url, namespaces=[NAMESPACE])
    except Exception as e:
        print(f"[conn] connect failed: {e}", flush=True)
        return 2

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, agent.shutdown_evt.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: agent.shutdown_evt.set())

    await agent.shutdown_evt.wait()
    await sio.disconnect()
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Hardcoded CarlaBridge test agent")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--namespace", default=NAMESPACE)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--no-extinguish", action="store_true",
                   help="send UGV_GOTO only; never UGV_EXTINGUISH (cancel-chain testing)")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(_parse_args())))
