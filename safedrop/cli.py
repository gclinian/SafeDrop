"""Command-line interface for SafeDrop — same 4 actions as the MCP server.

Useful from a shell, from CI, or from an AI agent that can run bash but
doesn't speak MCP yet.

    safedrop ls                                  # list nearby peers
    safedrop send-file <device> <path>           # push a file
    safedrop send-text <device> <text>           # push text/URL/code
    cat snippet.py | safedrop send-text <device> --type code --stdin
    safedrop wait [--timeout 300]                # block until something arrives

Every invocation starts a short-lived headless SafeDrop peer, does its
work, then exits. ``--wait N`` controls how many seconds we let UDP
discovery converge before giving up on a peer name.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .discovery import Peer
from .headless import HeadlessSafeDrop, peer_summary as _peer_summary, state_summary as _state_summary, wait_terminal as _wait_terminal


def _wait_for_peer(service: HeadlessSafeDrop, query: str, timeout: float) -> Peer:
    deadline = time.time() + timeout
    last_err: Exception = LookupError(query)
    while time.time() < deadline:
        try:
            return service.find_peer(query)
        except LookupError as exc:
            last_err = exc
            time.sleep(0.3)
    raise last_err


def _print_peers_human(peers: list[dict]) -> None:
    if not peers:
        print("(no peers visible)")
        return
    w_name = max(len("NAME"), max(len(p["name"]) for p in peers))
    w_addr = max(len("ADDRESS"), max(len(f'{p["ip"]}:{p["tcp_port"]}') for p in peers))
    print(f"{'NAME'.ljust(w_name)}  {'ADDRESS'.ljust(w_addr)}  PLATFORM")
    for p in peers:
        addr = f'{p["ip"]}:{p["tcp_port"]}'
        print(f"{p['name'].ljust(w_name)}  {addr.ljust(w_addr)}  {p['platform']}")


def _emit(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if isinstance(payload, dict):
            for k in ("status", "name", "size", "bytes_done", "pair_code", "save_path", "error",
                     "clipboard_content_type", "clipboard_content"):
                if k in payload and payload[k] not in (None, ""):
                    print(f"{k:>10}: {payload[k]}")
        else:
            print(payload)


# -------------------------------------------------------------------- cmds ----


def cmd_ls(args: argparse.Namespace) -> int:
    service = HeadlessSafeDrop(name_suffix="CLI")
    service.start()
    try:
        time.sleep(args.wait)
        assert service.discovery is not None
        peers = [_peer_summary(p) for p in service.discovery.snapshot().values()]
        if args.json:
            print(json.dumps(peers, ensure_ascii=False, indent=2))
        else:
            _print_peers_human(peers)
        return 0
    finally:
        service.stop()


def cmd_send_file(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser()
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 2
    service = HeadlessSafeDrop(name_suffix="CLI")
    service.start()
    try:
        peer = _wait_for_peer(service, args.device, timeout=args.wait)
        state = service.transfer.send_file(peer, path)
        _wait_terminal(state, timeout=float(args.timeout))
        _emit(_state_summary(state), args.json)
        return 0 if state.status == "done" else 1
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        service.stop()


def cmd_send_text(args: argparse.Namespace) -> int:
    if args.stdin:
        content = sys.stdin.read()
    elif args.text is not None:
        content = args.text
    else:
        print("error: provide TEXT or --stdin", file=sys.stderr)
        return 2
    if not content:
        print("error: empty content", file=sys.stderr)
        return 2
    service = HeadlessSafeDrop(name_suffix="CLI")
    service.start()
    try:
        peer = _wait_for_peer(service, args.device, timeout=args.wait)
        state = service.transfer.send_clipboard(peer, content, args.type)
        _wait_terminal(state, timeout=float(args.timeout))
        _emit(_state_summary(state), args.json)
        return 0 if state.status == "done" else 1
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        service.stop()


def cmd_wait(args: argparse.Namespace) -> int:
    import queue as _queue

    service = HeadlessSafeDrop(name_suffix="CLI")
    service.start()
    try:
        try:
            state = service._drop_queue.get(timeout=float(args.timeout))
        except _queue.Empty:
            print("error: timeout, no drop received", file=sys.stderr)
            return 1
        summary = _state_summary(state)
        if state.kind == "clipboard":
            summary["clipboard_content"] = state.clipboard_content
            summary["clipboard_content_type"] = state.clipboard_content_type
        _emit(summary, args.json)
        return 0
    finally:
        service.stop()


def cmd_tools(args: argparse.Namespace) -> int:
    service = HeadlessSafeDrop(name_suffix="CLI")
    service.start()
    try:
        peer = _wait_for_peer(service, args.device, timeout=args.wait)
        tools = service.transfer.list_remote_tools(peer, timeout=float(args.timeout))
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        service.stop()

    if args.json:
        print(json.dumps({"peer": peer.name, "tools": tools}, ensure_ascii=False, indent=2))
    else:
        if not tools:
            print(f"(no tools advertised by {peer.name})")
        else:
            print(f"Tools on {peer.name}:")
            for t in tools:
                desc = t.get("description", "").strip().splitlines()[0] if t.get("description") else ""
                print(f"  {t['name']:<24}  {desc}")
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    arguments: dict = {}
    if args.args:
        try:
            arguments = json.loads(args.args)
        except json.JSONDecodeError as exc:
            print(f"error: --args must be valid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(arguments, dict):
            print("error: --args must be a JSON object", file=sys.stderr)
            return 2

    service = HeadlessSafeDrop(name_suffix="CLI")
    service.start()
    try:
        peer = _wait_for_peer(service, args.device, timeout=args.wait)
        result = service.transfer.call_remote_tool(
            peer, name=args.tool, arguments=arguments, timeout=float(args.timeout)
        )
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        service.stop()

    payload = {"peer": peer.name, "tool": args.tool, **result}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if "error" in result:
            print(f"error from {peer.name}: {result['error']}")
            return 1
        r = result.get("result")
        if isinstance(r, (dict, list)):
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print(r)
    return 0 if "error" not in result else 1


def cmd_audit(args: argparse.Namespace) -> int:
    # Audit log lives inside a running headless peer. Spinning up an
    # ephemeral one wouldn't see anything because each invocation has
    # its own log. Tell the user how to use this properly.
    print(
        "Note: `safedrop audit` only sees this invocation's log. For the persistent\n"
        "log of a running peer, query its MCP server (`audit_log` tool) instead.",
        file=sys.stderr,
    )
    service = HeadlessSafeDrop(name_suffix="CLI")
    service.start()
    try:
        rows = service.transfer.audit_log[-args.limit:][::-1]
        if args.json:
            print(json.dumps([{
                "timestamp": r.timestamp,
                "direction": r.direction,
                "peer_name": r.peer_name,
                "peer_ip": r.peer_ip,
                "tool_name": r.tool_name,
                "arguments": r.arguments,
                "decision": r.decision,
                "result_summary": r.result_summary,
                "error": r.error,
            } for r in rows], ensure_ascii=False, indent=2))
        else:
            if not rows:
                print("(empty)")
            for r in rows:
                ts = time.strftime("%H:%M:%S", time.localtime(r.timestamp))
                print(f"{ts}  {r.direction:>8}  {r.peer_name:<30}  {r.tool_name:<20}  {r.decision}")
        return 0
    finally:
        service.stop()


# --------------------------------------------------------------------- main ---


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safedrop", description="SafeDrop CLI — LAN file & clipboard sharing")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _json_too(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    p_ls = sub.add_parser("ls", help="list nearby SafeDrop peers")
    p_ls.add_argument("--wait", type=float, default=3.0, help="seconds to wait for discovery (default 3)")
    _json_too(p_ls)
    p_ls.set_defaults(func=cmd_ls)

    p_sf = sub.add_parser("send-file", help="send a file to a peer")
    p_sf.add_argument("device", help="peer name (substring ok) or device id")
    p_sf.add_argument("path", help="path to the file to send")
    p_sf.add_argument("--wait", type=float, default=8.0, help="seconds to wait for peer to appear")
    p_sf.add_argument("--timeout", type=float, default=300, help="max transfer wait (s)")
    _json_too(p_sf)
    p_sf.set_defaults(func=cmd_send_file)

    p_st = sub.add_parser("send-text", help="send a clipboard/text/URL/code snippet")
    p_st.add_argument("device", help="peer name or device id")
    p_st.add_argument("text", nargs="?", help="text to send (or use --stdin)")
    p_st.add_argument("--stdin", action="store_true", help="read text from stdin instead of TEXT arg")
    p_st.add_argument("--type", choices=["text", "url", "code"], default="text", help="content type (default text)")
    p_st.add_argument("--wait", type=float, default=8.0, help="seconds to wait for peer to appear")
    p_st.add_argument("--timeout", type=float, default=60, help="max transfer wait (s)")
    _json_too(p_st)
    p_st.set_defaults(func=cmd_send_text)

    p_w = sub.add_parser("wait", help="block until something is dropped to us")
    p_w.add_argument("--timeout", type=float, default=300, help="max wait (s)")
    _json_too(p_w)
    p_w.set_defaults(func=cmd_wait)

    p_t = sub.add_parser("tools", help="list the tools a peer exposes")
    p_t.add_argument("device", help="peer name or device id")
    p_t.add_argument("--wait", type=float, default=8.0, help="seconds to wait for peer to appear")
    p_t.add_argument("--timeout", type=float, default=10, help="max wait for response (s)")
    _json_too(p_t)
    p_t.set_defaults(func=cmd_tools)

    p_c = sub.add_parser("call", help="invoke a tool on a peer")
    p_c.add_argument("device", help="peer name or device id")
    p_c.add_argument("tool", help="tool name (see `safedrop tools`)")
    p_c.add_argument("--args", default="", help="JSON object of arguments (e.g. '{\"content\":\"hi\"}')")
    p_c.add_argument("--wait", type=float, default=8.0, help="seconds to wait for peer to appear")
    p_c.add_argument("--timeout", type=float, default=60, help="max wait for response (s)")
    _json_too(p_c)
    p_c.set_defaults(func=cmd_call)

    p_a = sub.add_parser("audit", help="show this peer's cross-device tool-call audit log")
    p_a.add_argument("--limit", type=int, default=50)
    _json_too(p_a)
    p_a.set_defaults(func=cmd_audit)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
