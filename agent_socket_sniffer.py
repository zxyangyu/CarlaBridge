"""Bridge × Agent 协议 v1.0 — /agent 命名空间被动旁听客户端。

连接 CarlaBridge（默认 http://127.0.0.1:5000），完成 hello 握手后，把本客户端
收到的事件打印到终端（默认不打印高频的 state_snapshot，可加 --print-snapshots），
并可用 --jsonl 落盘（默认不包含 state_snapshot；需要时加 --jsonl-snapshots）。

说明（重要）:
    Bridge 对 state_snapshot / command_status / scenario_event / event_log 等
    多为 namespace 广播，本脚本作为第二个 /agent 客户端时，通常能看到与 UrbanAgent
    相同的数据面推送。

    本客户端无法看到「其他已连接 Agent」发往 Bridge 的出站帧（例如对方的
    agent.command、对方 emit 的 event_log），除非在 Bridge 侧做代理或查看服务端日志。

用法:
    python agent_socket_sniffer.py
    python agent_socket_sniffer.py --url http://127.0.0.1:5000 --jsonl trace.jsonl
    python agent_socket_sniffer.py --jsonl trace.jsonl --jsonl-snapshots   # 文件里也要每条 snapshot 时
    python agent_socket_sniffer.py --print-snapshots --state-every 10   # 需在终端看 snapshot 时
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from collections.abc import Callable
from typing import Any, TextIO

import socketio

DEFAULT_URL = "http://127.0.0.1:5000"
NAMESPACE = "/agent"
PROTOCOL_VERSION = "1.0"


def _json_default(o: object) -> str:
    return str(o)


def _shorten(data: Any, max_len: int = 2000) -> Any:
    """缩略超大 payload，避免 10Hz state_snapshot 刷屏终端输出。"""
    raw = json.dumps(data, ensure_ascii=False, default=_json_default)
    if len(raw) <= max_len:
        return data
    return raw[: max_len - 3] + "..."


class AgentSniffer:
    def __init__(
        self,
        *,
        url: str,
        agent_id: str,
        connect_timeout: float,
        hello_timeout: float,
        jsonl: TextIO | None,
        state_every: int,
        print_snapshots: bool,
        jsonl_snapshots: bool,
        shorten_terminal: int,
        verbose_unknown: bool,
    ) -> None:
        self.url = url
        self.agent_id = agent_id
        self.connect_timeout = connect_timeout
        self.hello_timeout = hello_timeout
        self.jsonl = jsonl
        self.state_every = state_every
        self.print_snapshots = print_snapshots
        self.jsonl_snapshots = jsonl_snapshots
        self.shorten_terminal = shorten_terminal
        self.verbose_unknown = verbose_unknown
        self._state_count = 0
        self._sio: socketio.AsyncClient | None = None
        self.shutdown_evt = asyncio.Event()

    def _write_jsonl(self, record: dict[str, Any]) -> None:
        if self.jsonl is None:
            return
        self.jsonl.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
        self.jsonl.flush()

    def _log(
        self,
        direction: str,
        event: str,
        data: Any,
        *,
        terminal: bool = True,
        shorten: bool = False,
        write_jsonl: bool = True,
    ) -> None:
        ts = time.time()
        rec: dict[str, Any] = {
            "ts_wall": ts,
            "direction": direction,
            "event": event,
            "data": data,
        }
        if write_jsonl:
            self._write_jsonl(rec)
        if not terminal:
            return
        out_data = _shorten(data, self.shorten_terminal) if shorten else data
        line = f"[{ts:.3f}] {direction:3} {event} {json.dumps(out_data, ensure_ascii=False, default=_json_default)}"
        print(line, flush=True)

    def _make_handler(self, event_name: str, *, shorten: bool = False) -> Callable[..., Any]:
        async def _handler(*args: Any) -> None:
            data = args[0] if args else None
            self._log("in", event_name, data, shorten=shorten)

        return _handler

    def _register_handlers(self, sio: socketio.AsyncClient) -> None:
        # 协议 §4 数据面 + connect/disconnect（state_snapshot 默认不落终端，见下）
        events: list[tuple[str, bool]] = [
            ("command_status", False),
            ("scenario_event", False),
            ("event_log", False),
        ]
        for name, shorten in events:
            sio.on(name, self._make_handler(name, shorten=shorten), namespace=NAMESPACE)

        @sio.on("state_snapshot", namespace=NAMESPACE)
        async def _on_state_snapshot(*args: Any) -> None:
            data = args[0] if args else None
            self._state_count += 1
            if not self.print_snapshots and not self.jsonl_snapshots:
                return
            want_terminal = self.print_snapshots
            if want_terminal and self.state_every > 1 and (self._state_count % self.state_every) != 0:
                want_terminal = False
            want_file = self.jsonl_snapshots
            if not want_terminal and not want_file:
                return
            self._log(
                "in",
                "state_snapshot",
                data,
                terminal=want_terminal,
                shorten=want_terminal,
                write_jsonl=want_file,
            )

        @sio.on("connect", namespace=NAMESPACE)
        async def _on_connect() -> None:
            self._log("sys", "connect", {"namespace": NAMESPACE})

        @sio.on("disconnect", namespace=NAMESPACE)
        async def _on_disconnect() -> None:
            self._log("sys", "disconnect", {"namespace": NAMESPACE})

    async def run(self) -> int:
        self._sio = socketio.AsyncClient(reconnection=True)
        assert self._sio is not None
        self._register_handlers(self._sio)

        try:
            await self._sio.connect(
                self.url,
                namespaces=[NAMESPACE],
                wait_timeout=self.connect_timeout,
            )
        except Exception as exc:
            print(f"连接失败: {exc}", file=sys.stderr, flush=True)
            return 2

        hello_payload = {"agent_id": self.agent_id, "version": PROTOCOL_VERSION}
        self._log("out", "hello(call)", hello_payload)
        try:
            ack = await self._sio.call(
                "hello",
                hello_payload,
                namespace=NAMESPACE,
                timeout=self.hello_timeout,
            )
        except Exception as exc:
            self._log("in", "hello_ack(error)", {"error": repr(exc)})
            print(f"hello 失败: {exc}", file=sys.stderr, flush=True)
            await self._sio.disconnect()
            return 3
        self._log("in", "hello_ack", ack)

        if self.verbose_unknown:
            print(
                "已订阅: state_snapshot（默认不落终端、不写 --jsonl）, command_status, scenario_event, "
                "event_log, connect/disconnect。\n"
                "--print-snapshots 终端输出；--jsonl-snapshots 与 --jsonl 配合写入 snapshot。\n"
                "若协议新增事件请补充 sio.on(...) 注册。按 Ctrl+C 退出。\n",
                flush=True,
            )

        await self.shutdown_evt.wait()
        if self._sio.connected:
            await self._sio.disconnect()
        self._sio = None
        return 0


async def _async_main(sniffer: AgentSniffer) -> int:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, sniffer.shutdown_evt.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: sniffer.shutdown_evt.set())

    return await sniffer.run()


def main() -> None:
    p = argparse.ArgumentParser(description="旁听 CarlaBridge /agent Socket.IO 消息（协议 v1）")
    p.add_argument("--url", default=DEFAULT_URL, help=f"Bridge 基址（默认 {DEFAULT_URL}）")
    p.add_argument("--agent-id", default="socket_sniffer", help="hello 中的 agent_id")
    p.add_argument("--connect-timeout", type=float, default=30.0)
    p.add_argument("--hello-timeout", type=float, default=5.0)
    p.add_argument(
        "--jsonl",
        metavar="FILE",
        default=None,
        help="追加写入 JSONL（每行一条；默认不写入 state_snapshot，见 --jsonl-snapshots）",
    )
    p.add_argument(
        "--jsonl-snapshots",
        action="store_true",
        help="将 state_snapshot 也写入 --jsonl（默认不写；信息量很大）",
    )
    p.add_argument(
        "--print-snapshots",
        action="store_true",
        help="在终端打印 state_snapshot（默认不打印；写文件需同时 --jsonl --jsonl-snapshots）",
    )
    p.add_argument(
        "--state-every",
        type=int,
        default=1,
        metavar="N",
        help="与 --print-snapshots 配合：仅每第 N 条 snapshot 打印到终端；1=每条都打印",
    )
    p.add_argument(
        "--shorten",
        type=int,
        default=2000,
        metavar="CHARS",
        help="终端输出中单条 JSON 最大字符数（超出截断）；JSONL 不受此限",
    )
    p.add_argument(
        "--quiet-hint",
        action="store_true",
        help="不打印「如何扩展订阅」的提示",
    )
    args = p.parse_args()

    if args.state_every < 1:
        print("--state-every 须 >= 1", file=sys.stderr)
        sys.exit(2)
    if args.jsonl_snapshots and not args.jsonl:
        print("--jsonl-snapshots 需同时指定 --jsonl <文件路径>", file=sys.stderr)
        sys.exit(2)

    jsonl_fp: TextIO | None = None
    if args.jsonl:
        jsonl_fp = open(args.jsonl, "a", encoding="utf-8")

    try:
        sniffer = AgentSniffer(
            url=args.url.rstrip("/"),
            agent_id=args.agent_id,
            connect_timeout=args.connect_timeout,
            hello_timeout=args.hello_timeout,
            jsonl=jsonl_fp,
            state_every=args.state_every,
            print_snapshots=args.print_snapshots,
            jsonl_snapshots=args.jsonl_snapshots,
            shorten_terminal=max(80, args.shorten),
            verbose_unknown=not args.quiet_hint,
        )
        code = asyncio.run(_async_main(sniffer))
    finally:
        if jsonl_fp is not None:
            jsonl_fp.close()

    raise SystemExit(code)


if __name__ == "__main__":
    main()
