"""TCP transfer engine for SafeDrop.

A single :class:`TransferManager` owns:

* the listening server socket on TCP_PORT;
* one worker thread per inbound/outbound connection;
* the transfer state objects that the GUI subscribes to.

The protocol on a fresh TCP connection is:

    1. Sender sends a plaintext  HELLO       (with its pubkey).
    2. Receiver replies plaintext HELLO_ACK  (with its pubkey).
       Both sides now derive a Fernet session + 4-digit pair_code.
    3. From this point on every frame's payload is Fernet-encrypted JSON.
    4. Sender sends REQUEST  (file metadata OR clipboard preview).
    5. Receiver sends ACCEPT or REJECT (after user confirmation).
    6. On ACCEPT:
         * clipboard  -> one CLIPBOARD message with the actual content
         * file       -> N CHUNK messages, last one has "final": true
                         (chunk bytes are base64'd inside the JSON)
    7. Connection closes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import CHUNK_SIZE, DOWNLOAD_DIR, TCP_PORT, VERSION
from .crypto import Identity, Session, derive_session
from .discovery import Peer
from .protocol import ProtocolError, recv_json, send_json
from .tools import ToolRegistry


# --------------------------------------------------------------------------
# Public data classes
# --------------------------------------------------------------------------


@dataclass
class IncomingRequest:
    """Request awaiting the receiver's accept/reject decision."""

    transfer_id: str
    peer_name: str
    peer_ip: str
    pair_code: str
    kind: str                       # "file" | "clipboard"
    name: str
    size: int
    content_type: str | None = None  # "text" | "url" | "code"   (clipboard)
    preview: str | None = None       # clipboard preview text

    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _accepted: bool = field(default=False, repr=False)

    def accept(self) -> None:
        self._accepted = True
        self._event.set()

    def reject(self) -> None:
        self._accepted = False
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout) and self._accepted


@dataclass
class TransferState:
    """Live state of one in-flight transfer (send or receive)."""

    transfer_id: str
    direction: str          # "send" | "recv"
    kind: str               # "file" | "clipboard"
    peer_name: str
    name: str
    size: int
    pair_code: str = ""
    bytes_done: int = 0
    start_time: float = field(default_factory=time.time)
    status: str = "pending"  # pending | transferring | done | failed | rejected
    error: str | None = None
    save_path: Path | None = None      # set for received files
    clipboard_content: str | None = None
    clipboard_content_type: str | None = None

    def speed_bps(self) -> float:
        elapsed = max(0.001, time.time() - self.start_time)
        return self.bytes_done / elapsed


@dataclass
class ClipboardPayload:
    """Result of a received clipboard transfer (delivered to the GUI)."""

    transfer_id: str
    peer_name: str
    content_type: str
    content: str


@dataclass
class ToolCallRequest:
    """Inbound CALL_TOOL — given to the authorizer to decide allow/deny."""
    request_id: str
    peer_name: str
    peer_ip: str
    peer_device_id: str
    pair_code: str
    tool_name: str
    arguments: dict


@dataclass
class ToolCallAuditEntry:
    """One row of the cross-device tool-call audit log."""
    timestamp: float
    direction: str           # "inbound" | "outbound"
    peer_name: str
    peer_ip: str
    tool_name: str
    arguments: dict
    decision: str            # "allowed" | "denied" | "error"
    result_summary: str | None = None
    error: str | None = None


# Callback types (all invoked from worker threads; GUI must marshal).
RequestCallback = Callable[[IncomingRequest], None]
StateCallback = Callable[[TransferState], None]
ClipboardCallback = Callable[[ClipboardPayload], None]
ToolAuthorizer = Callable[[ToolCallRequest], bool]
AuditCallback = Callable[[ToolCallAuditEntry], None]


# --------------------------------------------------------------------------
# Transfer manager
# --------------------------------------------------------------------------


class TransferManager:
    def __init__(
        self,
        identity: Identity,
        device_id: str,
        device_name: str,
        tcp_port: int = TCP_PORT,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.identity = identity
        self.device_id = device_id
        self.device_name = device_name
        self.tcp_port = tcp_port
        self.tool_registry = tool_registry

        self.on_request: RequestCallback | None = None
        self.on_state: StateCallback | None = None
        self.on_clipboard: ClipboardCallback | None = None
        # Authorizer for inbound CALL_TOOL. None = allow-all (Phase 2.0
        # default; Phase 2.1 will plug a GUI/policy prompt in here).
        self.on_tool_call: ToolAuthorizer | None = None
        self.on_audit: AuditCallback | None = None

        self.audit_log: list[ToolCallAuditEntry] = []

        self._server_sock: socket.socket | None = None
        self._stop = threading.Event()
        self._server_thread: threading.Thread | None = None

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # ---- lifecycle ----------------------------------------------------

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.tcp_port))
        sock.listen(8)
        sock.settimeout(1.0)
        # If caller passed tcp_port=0, ask the OS what we actually got so
        # downstream code (e.g. the discovery HELLO) advertises the right port.
        if self.tcp_port == 0:
            self.tcp_port = sock.getsockname()[1]
        self._server_sock = sock

        t = threading.Thread(target=self._accept_loop, daemon=True, name="TCP-accept")
        t.start()
        self._server_thread = t

    def stop(self) -> None:
        self._stop.set()
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass

    # ---- inbound side -------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._stop.is_set():
            try:
                client, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(
                target=self._handle_inbound,
                args=(client, addr[0]),
                daemon=True,
                name=f"TCP-recv-{addr[0]}",
            )
            t.start()

    def _handle_inbound(self, sock: socket.socket, peer_ip: str) -> None:
        state: TransferState | None = None
        try:
            with sock:
                sock.settimeout(60.0)

                # ---- plaintext handshake ----
                hello = recv_json(sock)
                if hello.get("type") != "HELLO":
                    raise ProtocolError("expected HELLO")
                peer_name = str(hello.get("name", "unknown"))
                peer_device_id = str(hello.get("device_id", ""))
                peer_pubkey = str(hello.get("pubkey", ""))
                if not peer_pubkey:
                    raise ProtocolError("missing pubkey")

                session = derive_session(self.identity, peer_pubkey)

                send_json(
                    sock,
                    {
                        "type": "HELLO_ACK",
                        "device_id": self.device_id,
                        "name": self.device_name,
                        "platform": platform.system(),
                        "pubkey": self.identity.public_key_b64(),
                        "version": VERSION,
                        "pair_code": session.pair_code,
                    },
                )

                # ---- dispatch on the first encrypted message ----
                msg = recv_json(sock, decrypt=session.decrypt)
                msg_type = msg.get("type")
                if msg_type == "LIST_TOOLS":
                    self._handle_inbound_list_tools(sock, session, msg, peer_name, peer_ip)
                    return
                if msg_type == "CALL_TOOL":
                    self._handle_inbound_call_tool(sock, session, msg, peer_name, peer_ip, peer_device_id)
                    return
                if msg_type != "REQUEST":
                    raise ProtocolError(f"unexpected first encrypted message type {msg_type!r}")

                # ---- existing REQUEST flow ----
                request = msg
                kind = str(request.get("kind", ""))
                transfer_id = str(request.get("transfer_id", uuid.uuid4().hex))

                if kind == "file":
                    name = str(request.get("name", "file"))
                    size = int(request.get("size", 0))
                    incoming = IncomingRequest(
                        transfer_id=transfer_id,
                        peer_name=peer_name,
                        peer_ip=peer_ip,
                        pair_code=session.pair_code,
                        kind=kind,
                        name=name,
                        size=size,
                    )
                elif kind == "clipboard":
                    content_type = str(request.get("content_type", "text"))
                    preview = str(request.get("preview", ""))
                    length = int(request.get("length", 0))
                    incoming = IncomingRequest(
                        transfer_id=transfer_id,
                        peer_name=peer_name,
                        peer_ip=peer_ip,
                        pair_code=session.pair_code,
                        kind=kind,
                        name=f"Clipboard ({content_type})",
                        size=length,
                        content_type=content_type,
                        preview=preview,
                    )
                else:
                    raise ProtocolError(f"unknown kind {kind!r}")

                state = TransferState(
                    transfer_id=transfer_id,
                    direction="recv",
                    kind=kind,
                    peer_name=peer_name,
                    name=incoming.name,
                    size=incoming.size,
                    pair_code=session.pair_code,
                )
                self._emit_state(state)

                # Hand the request to the GUI for user confirmation.
                if self.on_request is None:
                    raise RuntimeError("no on_request callback registered")
                self.on_request(incoming)
                accepted = incoming.wait(timeout=120.0)

                if not accepted:
                    send_json(sock, {"type": "REJECT", "transfer_id": transfer_id, "reason": "user"}, encrypt=session.encrypt)
                    state.status = "rejected"
                    self._emit_state(state)
                    return

                send_json(sock, {"type": "ACCEPT", "transfer_id": transfer_id}, encrypt=session.encrypt)
                state.status = "transferring"
                state.start_time = time.time()
                self._emit_state(state)

                if kind == "clipboard":
                    self._recv_clipboard(sock, session, state, transfer_id, incoming.content_type or "text")
                else:
                    self._recv_file(sock, session, state, incoming.name)

        except Exception as exc:
            if state is not None:
                state.status = "failed"
                state.error = str(exc)
                self._emit_state(state)

    # ---- inbound: clipboard ------------------------------------------

    def _recv_clipboard(
        self,
        sock: socket.socket,
        session: Session,
        state: TransferState,
        transfer_id: str,
        content_type: str,
    ) -> None:
        msg = recv_json(sock, decrypt=session.decrypt)
        if msg.get("type") != "CLIPBOARD" or msg.get("transfer_id") != transfer_id:
            raise ProtocolError("expected CLIPBOARD")
        content = str(msg.get("content", ""))
        state.bytes_done = len(content.encode("utf-8"))
        state.size = state.bytes_done
        state.clipboard_content = content
        state.clipboard_content_type = content_type
        state.status = "done"
        self._emit_state(state)

        if self.on_clipboard is not None:
            self.on_clipboard(
                ClipboardPayload(
                    transfer_id=transfer_id,
                    peer_name=state.peer_name,
                    content_type=content_type,
                    content=content,
                )
            )

    # ---- inbound: file -----------------------------------------------

    def _recv_file(self, sock: socket.socket, session: Session, state: TransferState, suggested_name: str) -> None:
        dest = self._choose_save_path(suggested_name)
        state.save_path = dest
        self._emit_state(state)

        hasher = hashlib.sha256()
        with dest.open("wb") as fh:
            while True:
                msg = recv_json(sock, decrypt=session.decrypt)
                if msg.get("type") != "CHUNK":
                    raise ProtocolError("expected CHUNK")
                data = base64.b64decode(msg.get("data_b64", "").encode("ascii"))
                fh.write(data)
                hasher.update(data)
                state.bytes_done += len(data)
                self._emit_state(state)
                if msg.get("final"):
                    break

        state.status = "done"
        self._emit_state(state)

    def _choose_save_path(self, name: str) -> Path:
        # Avoid path traversal — strip any directory components from the peer-supplied name.
        clean = Path(name).name or "received.bin"
        candidate = DOWNLOAD_DIR / clean
        if not candidate.exists():
            return candidate
        stem, dot, suffix = clean.rpartition(".")
        if dot:
            base, ext = stem, "." + suffix
        else:
            base, ext = clean, ""
        i = 1
        while True:
            candidate = DOWNLOAD_DIR / f"{base}_{i}{ext}"
            if not candidate.exists():
                return candidate
            i += 1

    # ---- inbound: cross-device tools --------------------------------

    def _handle_inbound_list_tools(
        self,
        sock: socket.socket,
        session: Session,
        msg: dict,
        peer_name: str,
        peer_ip: str,
    ) -> None:
        request_id = str(msg.get("request_id", ""))
        if self.tool_registry is None:
            tools_payload: list[dict] = []
        else:
            tools_payload = self.tool_registry.list_manifests()
        send_json(
            sock,
            {"type": "TOOLS_LIST", "request_id": request_id, "tools": tools_payload},
            encrypt=session.encrypt,
        )

    def _handle_inbound_call_tool(
        self,
        sock: socket.socket,
        session: Session,
        msg: dict,
        peer_name: str,
        peer_ip: str,
        peer_device_id: str,
    ) -> None:
        request_id = str(msg.get("request_id", ""))
        name = str(msg.get("name", ""))
        args = msg.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}

        if self.tool_registry is None or not self.tool_registry.has(name):
            err = f"tool not available: {name!r}"
            send_json(
                sock,
                {"type": "CALL_TOOL_RESULT", "request_id": request_id, "error": err},
                encrypt=session.encrypt,
            )
            self._record_audit("inbound", peer_name, peer_ip, name, args, "denied", error=err)
            return

        # Authorize.
        req = ToolCallRequest(
            request_id=request_id,
            peer_name=peer_name,
            peer_ip=peer_ip,
            peer_device_id=peer_device_id,
            pair_code=session.pair_code,
            tool_name=name,
            arguments=args,
        )
        allowed = True
        if self.on_tool_call is not None:
            try:
                allowed = bool(self.on_tool_call(req))
            except Exception:
                allowed = False
        if not allowed:
            send_json(
                sock,
                {"type": "CALL_TOOL_RESULT", "request_id": request_id,
                 "error": "denied by authorizer"},
                encrypt=session.encrypt,
            )
            self._record_audit("inbound", peer_name, peer_ip, name, args, "denied")
            return

        # Execute.
        try:
            result = self.tool_registry.call(name, args)
            send_json(
                sock,
                {"type": "CALL_TOOL_RESULT", "request_id": request_id, "result": result},
                encrypt=session.encrypt,
            )
            summary = (json.dumps(result)[:120] if not isinstance(result, str) else result[:120])
            self._record_audit("inbound", peer_name, peer_ip, name, args, "allowed", result_summary=summary)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            send_json(
                sock,
                {"type": "CALL_TOOL_RESULT", "request_id": request_id, "error": err},
                encrypt=session.encrypt,
            )
            self._record_audit("inbound", peer_name, peer_ip, name, args, "error", error=err)

    def _record_audit(
        self,
        direction: str,
        peer_name: str,
        peer_ip: str,
        tool_name: str,
        arguments: dict,
        decision: str,
        result_summary: str | None = None,
        error: str | None = None,
    ) -> None:
        entry = ToolCallAuditEntry(
            timestamp=time.time(),
            direction=direction,
            peer_name=peer_name,
            peer_ip=peer_ip,
            tool_name=tool_name,
            arguments=arguments,
            decision=decision,
            result_summary=result_summary,
            error=error,
        )
        self.audit_log.append(entry)
        if self.on_audit is not None:
            try:
                self.on_audit(entry)
            except Exception:
                pass

    # ---- outbound side -----------------------------------------------

    def send_file(self, peer: Peer, path: Path) -> TransferState:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        state = TransferState(
            transfer_id=uuid.uuid4().hex,
            direction="send",
            kind="file",
            peer_name=peer.name,
            name=path.name,
            size=size,
        )
        threading.Thread(
            target=self._do_send_file,
            args=(peer, path, state),
            daemon=True,
            name=f"TCP-send-file-{state.transfer_id[:6]}",
        ).start()
        return state

    def send_clipboard(self, peer: Peer, content: str, content_type: str) -> TransferState:
        if content_type not in ("text", "url", "code"):
            content_type = "text"
        size = len(content.encode("utf-8"))
        state = TransferState(
            transfer_id=uuid.uuid4().hex,
            direction="send",
            kind="clipboard",
            peer_name=peer.name,
            name=f"Clipboard ({content_type})",
            size=size,
            clipboard_content=content,
            clipboard_content_type=content_type,
        )
        threading.Thread(
            target=self._do_send_clipboard,
            args=(peer, content, content_type, state),
            daemon=True,
            name=f"TCP-send-clip-{state.transfer_id[:6]}",
        ).start()
        return state

    def _open_session(self, peer: Peer) -> tuple[socket.socket, Session]:
        """TCP connect + plaintext HELLO/HELLO_ACK + derive a Fernet session."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15.0)
        sock.connect((peer.ip, peer.tcp_port))
        send_json(
            sock,
            {
                "type": "HELLO",
                "device_id": self.device_id,
                "name": self.device_name,
                "platform": platform.system(),
                "pubkey": self.identity.public_key_b64(),
                "version": VERSION,
            },
        )
        ack = recv_json(sock)
        if ack.get("type") != "HELLO_ACK":
            raise ProtocolError("expected HELLO_ACK")
        peer_pubkey = str(ack.get("pubkey", ""))
        if not peer_pubkey:
            raise ProtocolError("missing peer pubkey")
        session = derive_session(self.identity, peer_pubkey)
        return sock, session

    def _connect(self, peer: Peer, state: TransferState) -> tuple[socket.socket, Session]:
        sock, session = self._open_session(peer)
        state.pair_code = session.pair_code
        self._emit_state(state)
        return sock, session

    # ---- outbound: cross-device tools --------------------------------

    def list_remote_tools(self, peer: Peer, timeout: float = 10.0) -> list[dict]:
        """Ask `peer` for its tool manifest. Returns a list of {name, description, inputSchema}."""
        sock, session = self._open_session(peer)
        with sock:
            sock.settimeout(timeout)
            req_id = uuid.uuid4().hex
            send_json(sock, {"type": "LIST_TOOLS", "request_id": req_id}, encrypt=session.encrypt)
            resp = recv_json(sock, decrypt=session.decrypt)
            if resp.get("type") != "TOOLS_LIST" or resp.get("request_id") != req_id:
                raise ProtocolError("expected TOOLS_LIST")
            return list(resp.get("tools") or [])

    def call_remote_tool(
        self,
        peer: Peer,
        name: str,
        arguments: dict | None = None,
        timeout: float = 60.0,
    ) -> dict:
        """Invoke a tool on `peer`. Returns {"result": ...} or {"error": "..."}."""
        args = arguments or {}
        sock, session = self._open_session(peer)
        with sock:
            sock.settimeout(timeout)
            req_id = uuid.uuid4().hex
            send_json(sock, {
                "type": "CALL_TOOL",
                "request_id": req_id,
                "name": name,
                "arguments": args,
            }, encrypt=session.encrypt)
            resp = recv_json(sock, decrypt=session.decrypt)
            if resp.get("type") != "CALL_TOOL_RESULT" or resp.get("request_id") != req_id:
                raise ProtocolError("expected CALL_TOOL_RESULT")

            decision = "error" if "error" in resp else "allowed"
            summary: str | None = None
            if "result" in resp:
                r = resp["result"]
                summary = r[:120] if isinstance(r, str) else json.dumps(r, ensure_ascii=False)[:120]
            self._record_audit(
                direction="outbound",
                peer_name=peer.name,
                peer_ip=peer.ip,
                tool_name=name,
                arguments=args,
                decision=decision,
                result_summary=summary,
                error=resp.get("error"),
            )
            return {k: v for k, v in resp.items() if k in ("result", "error")}

    def _wait_for_decision(self, sock: socket.socket, session: Session, transfer_id: str) -> str:
        sock.settimeout(180.0)
        resp = recv_json(sock, decrypt=session.decrypt)
        sock.settimeout(60.0)
        kind = resp.get("type")
        if kind not in ("ACCEPT", "REJECT") or resp.get("transfer_id") != transfer_id:
            raise ProtocolError(f"expected ACCEPT/REJECT, got {kind!r}")
        return str(kind)

    def _do_send_file(self, peer: Peer, path: Path, state: TransferState) -> None:
        try:
            sock, session = self._connect(peer, state)
            with sock:
                send_json(
                    sock,
                    {
                        "type": "REQUEST",
                        "transfer_id": state.transfer_id,
                        "kind": "file",
                        "name": path.name,
                        "size": state.size,
                    },
                    encrypt=session.encrypt,
                )
                decision = self._wait_for_decision(sock, session, state.transfer_id)
                if decision == "REJECT":
                    state.status = "rejected"
                    self._emit_state(state)
                    return

                state.status = "transferring"
                state.start_time = time.time()
                self._emit_state(state)

                seq = 0
                with path.open("rb") as fh:
                    while True:
                        chunk = fh.read(CHUNK_SIZE)
                        is_final = len(chunk) < CHUNK_SIZE
                        send_json(
                            sock,
                            {
                                "type": "CHUNK",
                                "transfer_id": state.transfer_id,
                                "seq": seq,
                                "data_b64": base64.b64encode(chunk).decode("ascii"),
                                "final": is_final,
                            },
                            encrypt=session.encrypt,
                        )
                        state.bytes_done += len(chunk)
                        self._emit_state(state)
                        seq += 1
                        if is_final:
                            break

                state.status = "done"
                self._emit_state(state)

        except Exception as exc:
            state.status = "failed"
            state.error = str(exc)
            self._emit_state(state)

    def _do_send_clipboard(self, peer: Peer, content: str, content_type: str, state: TransferState) -> None:
        try:
            sock, session = self._connect(peer, state)
            with sock:
                preview = content[:200]
                send_json(
                    sock,
                    {
                        "type": "REQUEST",
                        "transfer_id": state.transfer_id,
                        "kind": "clipboard",
                        "content_type": content_type,
                        "preview": preview,
                        "length": state.size,
                    },
                    encrypt=session.encrypt,
                )
                decision = self._wait_for_decision(sock, session, state.transfer_id)
                if decision == "REJECT":
                    state.status = "rejected"
                    self._emit_state(state)
                    return

                state.status = "transferring"
                state.start_time = time.time()
                self._emit_state(state)

                send_json(
                    sock,
                    {
                        "type": "CLIPBOARD",
                        "transfer_id": state.transfer_id,
                        "content_type": content_type,
                        "content": content,
                    },
                    encrypt=session.encrypt,
                )
                state.bytes_done = state.size
                state.status = "done"
                self._emit_state(state)

        except Exception as exc:
            state.status = "failed"
            state.error = str(exc)
            self._emit_state(state)

    # ---- internal ----------------------------------------------------

    def _emit_state(self, state: TransferState) -> None:
        if self.on_state is not None:
            try:
                self.on_state(state)
            except Exception:
                pass
