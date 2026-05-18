"""tkinter GUI for SafeDrop.

Worker threads (discovery / transfer) push events at us via callbacks.
Tk is single-threaded, so every callback marshals onto the GUI thread with
``self.root.after(0, ...)``. The GUI itself only mutates widgets from the
main thread.
"""

from __future__ import annotations

import platform
import queue
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pyperclip

from .config import DOWNLOAD_DIR, TCP_PORT, default_device_name, new_device_id
from .crypto import Identity
from .discovery import DiscoveryService, Peer
from .tools import build_default_registry
from .transfer import (
    ClipboardPayload,
    IncomingRequest,
    ToolCallAuditEntry,
    ToolCallRequest,
    TransferManager,
    TransferState,
)
from .trust import (
    AuditWriter,
    DECISION_ALLOW,
    DECISION_DENY,
    TrustPolicy,
)
import threading
import time


def _human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024.0:
            return f"{f:,.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024.0
    return f"{f:,.1f} PB"


def _human_speed(bps: float) -> str:
    return _human_size(int(bps)) + "/s"


class _ToolCallGate:
    """Tiny sync primitive for the GUI Allow/Deny dialog."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.allow = False
        self.persist = False

    def respond(self, allow: bool, persist: bool, win: tk.Toplevel | None = None) -> None:
        self.allow = allow
        self.persist = persist
        self.event.set()
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass


class SafeDropApp:
    def __init__(self) -> None:
        self.identity = Identity.generate()
        self.device_id = new_device_id()
        self.device_name = default_device_name()
        self.platform_name = platform.system()

        self.root = tk.Tk()
        self.root.title(f"SafeDrop — {self.device_name}")
        self.root.geometry("960x600")
        self.root.minsize(820, 500)

        self._build_ui()

        # ---- trust + audit (Phase 2.1) ----
        self.trust_policy = TrustPolicy()
        self.audit_writer = AuditWriter()
        self.tool_registry = build_default_registry()
        self._audit_entries: list[ToolCallAuditEntry] = []

        # ---- backend services ----
        self.transfer = TransferManager(
            identity=self.identity,
            device_id=self.device_id,
            device_name=self.device_name,
            tcp_port=TCP_PORT,
            tool_registry=self.tool_registry,
            trust_policy=self.trust_policy,
        )
        self.transfer.on_request = self._on_incoming_request
        self.transfer.on_state = self._on_transfer_state
        self.transfer.on_clipboard = self._on_clipboard_received
        self.transfer.on_tool_call = self._on_tool_call
        self.transfer.on_audit = self._on_audit_entry
        self.transfer.start()

        self.discovery = DiscoveryService(
            device_id=self.device_id,
            device_name=self.device_name,
            platform_name=self.platform_name,
            tcp_port=TCP_PORT,
            pubkey_b64=self.identity.public_key_b64(),
            capabilities=("safedrop.transfer", "safedrop.tools"),
            on_change=self._on_peers_changed,
        )
        self.discovery.start()

        self._peers: dict[str, Peer] = {}
        self._transfers: dict[str, TransferState] = {}

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_status_bar()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("aqua" if self.platform_name == "Darwin" else "clam")
        except tk.TclError:
            pass

        header = ttk.Frame(self.root, padding=(12, 10))
        header.pack(side="top", fill="x")
        ttk.Label(header, text="SafeDrop", font=("Helvetica", 18, "bold")).pack(side="left")
        ttk.Label(
            header,
            text="Zero-config LAN file & clipboard sharing",
            foreground="#666",
        ).pack(side="left", padx=(10, 0))

        self.status_var = tk.StringVar(value="Starting…")
        ttk.Label(header, textvariable=self.status_var, foreground="#444").pack(side="right", padx=(0, 8))
        ttk.Button(header, text="🔒 Manage trust",
                   command=self._show_trust_dialog).pack(side="right", padx=(0, 6))

        main = ttk.Panedwindow(self.root, orient="horizontal")
        main.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 6))

        # ---- Left: nearby devices ------------------------------------
        left = ttk.Labelframe(main, text="Nearby devices", padding=8)
        main.add(left, weight=1)

        cols = ("name", "ip", "platform")
        self.peer_tree = ttk.Treeview(left, columns=cols, show="headings", height=12, selectmode="browse")
        self.peer_tree.heading("name", text="Name")
        self.peer_tree.heading("ip", text="IP")
        self.peer_tree.heading("platform", text="OS")
        self.peer_tree.column("name", width=190, anchor="w")
        self.peer_tree.column("ip", width=110, anchor="w")
        self.peer_tree.column("platform", width=70, anchor="w")
        self.peer_tree.pack(side="top", fill="both", expand=True)
        self.peer_tree.bind("<<TreeviewSelect>>", lambda _e: self._refresh_selected_peer())

        ttk.Label(left, text="Other devices on the same Wi-Fi appear here automatically.",
                  foreground="#777", wraplength=260).pack(side="top", anchor="w", pady=(8, 0))

        # ---- Right: send panel + transfers ---------------------------
        right = ttk.Frame(main)
        main.add(right, weight=2)

        send = ttk.Labelframe(right, text="Send to selected device", padding=10)
        send.pack(side="top", fill="x")

        self.selected_var = tk.StringVar(value="No device selected")
        ttk.Label(send, textvariable=self.selected_var, font=("Helvetica", 12, "bold")).pack(anchor="w")

        # -- file row --
        file_row = ttk.Frame(send)
        file_row.pack(fill="x", pady=(10, 6))
        ttk.Label(file_row, text="File:").pack(side="left")
        self.file_path_var = tk.StringVar(value="")
        ttk.Entry(file_row, textvariable=self.file_path_var).pack(side="left", fill="x", expand=True, padx=(8, 6))
        ttk.Button(file_row, text="Choose…", command=self._choose_file).pack(side="left")
        ttk.Button(file_row, text="Send file", command=self._send_file).pack(side="left", padx=(6, 0))

        # -- clipboard row --
        clip = ttk.Frame(send)
        clip.pack(fill="x", pady=(8, 0))
        head = ttk.Frame(clip)
        head.pack(fill="x")
        ttk.Label(head, text="Clipboard / text:").pack(side="left")
        self.content_type_var = tk.StringVar(value="text")
        ttk.Radiobutton(head, text="Text", variable=self.content_type_var, value="text").pack(side="left", padx=(10, 0))
        ttk.Radiobutton(head, text="URL", variable=self.content_type_var, value="url").pack(side="left")
        ttk.Radiobutton(head, text="Code", variable=self.content_type_var, value="code").pack(side="left")
        ttk.Button(head, text="Paste from clipboard", command=self._paste_from_clipboard).pack(side="right")

        self.clip_text = tk.Text(clip, height=6, wrap="word")
        self.clip_text.pack(fill="x", pady=(6, 0))

        button_row = ttk.Frame(clip)
        button_row.pack(fill="x", pady=(6, 0))
        ttk.Button(button_row, text="Clear", command=lambda: self.clip_text.delete("1.0", "end")).pack(side="left")
        ttk.Button(button_row, text="Send clipboard", command=self._send_clipboard).pack(side="right")

        # ---- Transfers panel ------------------------------------------
        transfers = ttk.Labelframe(right, text="Transfers", padding=8)
        transfers.pack(side="top", fill="both", expand=True, pady=(10, 0))

        t_cols = ("dir", "peer", "name", "status", "progress", "speed")
        self.transfer_tree = ttk.Treeview(
            transfers, columns=t_cols, show="headings", height=8, selectmode="browse"
        )
        for col, label, width, anchor in [
            ("dir", "↑↓", 30, "center"),
            ("peer", "Peer", 130, "w"),
            ("name", "Item", 180, "w"),
            ("status", "Status", 100, "w"),
            ("progress", "Progress", 120, "w"),
            ("speed", "Speed", 90, "w"),
        ]:
            self.transfer_tree.heading(col, text=label)
            self.transfer_tree.column(col, width=width, anchor=anchor)
        self.transfer_tree.pack(side="top", fill="both", expand=True)
        self.transfer_tree.bind("<Double-1>", self._on_transfer_double_click)

        ttk.Label(
            transfers,
            text=f"Received files → {DOWNLOAD_DIR}",
            foreground="#777",
        ).pack(side="top", anchor="w", pady=(6, 0))

        # ---- Audit log panel (Phase 2.1) ------------------------------
        audit = ttk.Labelframe(right, text="Cross-device tool audit", padding=8)
        audit.pack(side="top", fill="both", expand=False, pady=(10, 0))
        a_cols = ("time", "dir", "peer", "tool", "decision", "summary")
        self.audit_tree = ttk.Treeview(
            audit, columns=a_cols, show="headings", height=5, selectmode="browse"
        )
        for col, label, width, anchor in [
            ("time", "Time", 70, "w"),
            ("dir", "↕", 70, "w"),
            ("peer", "Peer", 130, "w"),
            ("tool", "Tool", 130, "w"),
            ("decision", "Decision", 80, "w"),
            ("summary", "Result / error", 200, "w"),
        ]:
            self.audit_tree.heading(col, text=label)
            self.audit_tree.column(col, width=width, anchor=anchor)
        self.audit_tree.pack(side="top", fill="both", expand=True)

        # ---- footer status bar ---------------------------------------
        self.footer_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.footer_var, anchor="w", foreground="#666", padding=(12, 4)).pack(
            side="bottom", fill="x"
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def _refresh_status_bar(self) -> None:
        local_ip = getattr(self.discovery, "local_ip", "?") if hasattr(self, "discovery") else "?"
        self.status_var.set(f"{self.device_name}  ·  {local_ip}:{TCP_PORT}")
        self.footer_var.set(
            f"Device-ID {self.device_id[:8]}…   |   {len(self._peers)} peer(s) nearby"
        )

    # ------------------------------------------------------------------
    # Peer list
    # ------------------------------------------------------------------
    def _on_peers_changed(self, peers: dict[str, Peer]) -> None:
        self.root.after(0, lambda: self._apply_peers(peers))

    def _apply_peers(self, peers: dict[str, Peer]) -> None:
        self._peers = peers
        selected = self.peer_tree.selection()
        selected_id = selected[0] if selected else None

        for iid in self.peer_tree.get_children():
            self.peer_tree.delete(iid)
        for pid, peer in peers.items():
            self.peer_tree.insert("", "end", iid=pid, values=(peer.name, peer.ip, peer.platform))

        if selected_id and selected_id in peers:
            self.peer_tree.selection_set(selected_id)
        self._refresh_selected_peer()
        self._refresh_status_bar()

    def _refresh_selected_peer(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            self.selected_var.set("No device selected")
        else:
            self.selected_var.set(f"→ {peer.name}  ({peer.ip}:{peer.tcp_port})")

    def _selected_peer(self) -> Peer | None:
        sel = self.peer_tree.selection()
        if not sel:
            return None
        return self._peers.get(sel[0])

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def _choose_file(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="Select a file to send")
        if path:
            self.file_path_var.set(path)

    def _send_file(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            messagebox.showinfo("SafeDrop", "Select a nearby device first.")
            return
        raw = self.file_path_var.get().strip()
        if not raw:
            messagebox.showinfo("SafeDrop", "Pick a file to send.")
            return
        path = Path(raw).expanduser()
        if not path.is_file():
            messagebox.showerror("SafeDrop", f"Not a file: {path}")
            return
        try:
            self.transfer.send_file(peer, path)
        except Exception as exc:
            messagebox.showerror("SafeDrop", f"Send failed: {exc}")

    def _paste_from_clipboard(self) -> None:
        try:
            text = pyperclip.paste()
        except Exception as exc:
            messagebox.showerror("SafeDrop", f"Could not read clipboard: {exc}")
            return
        if not text:
            return
        self.clip_text.delete("1.0", "end")
        self.clip_text.insert("1.0", text)
        guess = self.content_type_var.get()
        stripped = text.strip()
        if stripped.startswith(("http://", "https://")) and "\n" not in stripped:
            guess = "url"
        elif "\n" in stripped or any(c in stripped for c in ("{", "}", ";", "def ", "class ")):
            guess = "code" if guess != "url" else guess
        self.content_type_var.set(guess)

    def _send_clipboard(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            messagebox.showinfo("SafeDrop", "Select a nearby device first.")
            return
        content = self.clip_text.get("1.0", "end-1c")
        if not content:
            messagebox.showinfo("SafeDrop", "Type or paste something to send.")
            return
        try:
            self.transfer.send_clipboard(peer, content, self.content_type_var.get())
        except Exception as exc:
            messagebox.showerror("SafeDrop", f"Send failed: {exc}")

    # ------------------------------------------------------------------
    # Transfer table
    # ------------------------------------------------------------------
    def _on_transfer_state(self, state: TransferState) -> None:
        self.root.after(0, lambda: self._apply_transfer_state(state))

    def _apply_transfer_state(self, state: TransferState) -> None:
        self._transfers[state.transfer_id] = state
        values = (
            "↑" if state.direction == "send" else "↓",
            state.peer_name,
            state.name,
            self._status_text(state),
            self._progress_text(state),
            _human_speed(state.speed_bps()) if state.status == "transferring" else "",
        )
        if self.transfer_tree.exists(state.transfer_id):
            self.transfer_tree.item(state.transfer_id, values=values)
        else:
            self.transfer_tree.insert("", 0, iid=state.transfer_id, values=values)

    def _status_text(self, state: TransferState) -> str:
        if state.status == "failed" and state.error:
            return f"failed: {state.error[:40]}"
        return state.status

    def _progress_text(self, state: TransferState) -> str:
        if state.size <= 0:
            return "—"
        pct = min(100.0, 100.0 * state.bytes_done / max(1, state.size))
        return f"{pct:5.1f}%  ({_human_size(state.bytes_done)} / {_human_size(state.size)})"

    def _on_transfer_double_click(self, _event: tk.Event) -> None:
        sel = self.transfer_tree.selection()
        if not sel:
            return
        state = self._transfers.get(sel[0])
        if state is None:
            return
        if state.kind == "file" and state.direction == "recv" and state.save_path is not None:
            self._reveal_in_finder(state.save_path)
        elif state.kind == "clipboard" and state.clipboard_content is not None:
            self._show_clipboard_payload(
                ClipboardPayload(
                    transfer_id=state.transfer_id,
                    peer_name=state.peer_name,
                    content_type=state.clipboard_content_type or "text",
                    content=state.clipboard_content,
                )
            )

    def _reveal_in_finder(self, path: Path) -> None:
        try:
            if self.platform_name == "Darwin":
                import subprocess
                subprocess.Popen(["open", "-R", str(path)])
            elif self.platform_name == "Windows":
                import subprocess
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path.parent)])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Incoming request dialog
    # ------------------------------------------------------------------
    def _on_incoming_request(self, request: IncomingRequest) -> None:
        self.root.after(0, lambda: self._show_request_dialog(request))

    def _show_request_dialog(self, request: IncomingRequest) -> None:
        top = tk.Toplevel(self.root)
        top.title("Incoming SafeDrop request")
        top.transient(self.root)
        top.geometry("420x300")
        top.resizable(False, False)
        top.protocol("WM_DELETE_WINDOW", lambda: self._respond(top, request, accept=False))

        wrap = ttk.Frame(top, padding=18)
        wrap.pack(fill="both", expand=True)

        ttk.Label(
            wrap,
            text=f"{request.peer_name} wants to send you:",
            font=("Helvetica", 12, "bold"),
        ).pack(anchor="w")
        ttk.Label(wrap, text=f"from {request.peer_ip}", foreground="#666").pack(anchor="w")

        ttk.Separator(wrap).pack(fill="x", pady=10)

        if request.kind == "file":
            ttk.Label(wrap, text=f"📄  {request.name}").pack(anchor="w")
            ttk.Label(wrap, text=f"Size: {_human_size(request.size)}").pack(anchor="w")
        else:
            ctype = request.content_type or "text"
            label = {"text": "Text", "url": "URL", "code": "Code snippet"}.get(ctype, ctype)
            ttk.Label(wrap, text=f"📋  Clipboard — {label}").pack(anchor="w")
            preview = request.preview or ""
            preview_box = tk.Text(wrap, height=4, wrap="word")
            preview_box.insert("1.0", preview)
            preview_box.configure(state="disabled")
            preview_box.pack(fill="x", pady=(6, 0))

        ttk.Separator(wrap).pack(fill="x", pady=10)
        ttk.Label(wrap, text="Pair code (verify visually):", foreground="#666").pack(anchor="w")
        ttk.Label(wrap, text=request.pair_code, font=("Helvetica", 22, "bold")).pack(anchor="w")

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(16, 0))
        ttk.Button(btns, text="Reject", command=lambda: self._respond(top, request, accept=False)).pack(side="left")
        accept_btn = ttk.Button(btns, text="Accept", command=lambda: self._respond(top, request, accept=True))
        accept_btn.pack(side="right")
        accept_btn.focus_set()

        top.lift()
        top.attributes("-topmost", True)
        top.after(200, lambda: top.attributes("-topmost", False))

    def _respond(self, window: tk.Toplevel, request: IncomingRequest, accept: bool) -> None:
        if accept:
            request.accept()
        else:
            request.reject()
        try:
            window.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Inbound clipboard popup
    # ------------------------------------------------------------------
    def _on_clipboard_received(self, payload: ClipboardPayload) -> None:
        self.root.after(0, lambda: self._show_clipboard_payload(payload))

    def _show_clipboard_payload(self, payload: ClipboardPayload) -> None:
        top = tk.Toplevel(self.root)
        top.title(f"Clipboard from {payload.peer_name}")
        top.geometry("520x320")

        wrap = ttk.Frame(top, padding=14)
        wrap.pack(fill="both", expand=True)

        label = {"text": "Text", "url": "URL", "code": "Code snippet"}.get(payload.content_type, payload.content_type)
        ttk.Label(wrap, text=f"{label} from {payload.peer_name}", font=("Helvetica", 12, "bold")).pack(anchor="w")

        body = tk.Text(wrap, wrap="word")
        body.insert("1.0", payload.content)
        body.configure(state="normal")
        body.pack(fill="both", expand=True, pady=(8, 8))

        btns = ttk.Frame(wrap)
        btns.pack(fill="x")
        ttk.Button(btns, text="Close", command=top.destroy).pack(side="right")
        ttk.Button(btns, text="Copy to clipboard",
                   command=lambda: self._copy_to_clipboard(payload.content)).pack(side="left")
        if payload.content_type == "url":
            ttk.Button(btns, text="Open URL",
                       command=lambda: self._open_url(payload.content)).pack(side="left", padx=(6, 0))

    def _copy_to_clipboard(self, text: str) -> None:
        try:
            pyperclip.copy(text)
        except Exception:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

    def _open_url(self, url: str) -> None:
        url = url.strip()
        if not url:
            return
        try:
            webbrowser.open(url)
        except Exception as exc:
            messagebox.showerror("SafeDrop", f"Could not open URL: {exc}")

    # ------------------------------------------------------------------
    # Cross-device tool calls (Phase 2.1)
    # ------------------------------------------------------------------
    def _on_tool_call(self, req: ToolCallRequest) -> bool:
        """Worker-thread entrypoint for inbound CALL_TOOL.

        Marshals onto the GUI thread to show a modal-ish dialog, then
        blocks until the user clicks. Returns the allow/deny decision.
        """
        gate = _ToolCallGate()
        self.root.after(0, lambda: self._show_tool_call_dialog(req, gate))
        if not gate.event.wait(timeout=120.0):
            return False  # timeout → deny
        if gate.persist:
            self.trust_policy.set(
                req.peer_device_id,
                req.tool_name,
                DECISION_ALLOW if gate.allow else DECISION_DENY,
            )
        return gate.allow

    def _show_tool_call_dialog(self, req: ToolCallRequest, gate: "_ToolCallGate") -> None:
        top = tk.Toplevel(self.root)
        top.title("Cross-device tool call")
        top.transient(self.root)
        top.geometry("440x340")
        top.protocol("WM_DELETE_WINDOW", lambda: gate.respond(allow=False, persist=False, win=top))

        wrap = ttk.Frame(top, padding=16)
        wrap.pack(fill="both", expand=True)

        ttk.Label(
            wrap,
            text=f"{req.peer_name} wants to call a tool on this device",
            font=("Helvetica", 12, "bold"),
        ).pack(anchor="w")
        ttk.Label(wrap, text=f"from {req.peer_ip}", foreground="#666").pack(anchor="w")

        ttk.Separator(wrap).pack(fill="x", pady=8)

        ttk.Label(wrap, text=f"🔧  {req.tool_name}", font=("Helvetica", 13)).pack(anchor="w")
        args_preview = (
            "(no arguments)"
            if not req.arguments
            else "\n".join(f"  {k} = {v!r}"[:80] for k, v in req.arguments.items())
        )
        args_box = tk.Text(wrap, height=4, wrap="word")
        args_box.insert("1.0", args_preview)
        args_box.configure(state="disabled")
        args_box.pack(fill="x", pady=(4, 0))

        ttk.Separator(wrap).pack(fill="x", pady=8)
        ttk.Label(wrap, text="Pair code:", foreground="#666").pack(anchor="w")
        ttk.Label(wrap, text=req.pair_code, font=("Helvetica", 18, "bold")).pack(anchor="w")

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="Deny once",
                   command=lambda: gate.respond(allow=False, persist=False, win=top)).pack(side="left")
        ttk.Button(btns, text="Always deny",
                   command=lambda: gate.respond(allow=False, persist=True, win=top)).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Always allow",
                   command=lambda: gate.respond(allow=True, persist=True, win=top)).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Allow once",
                   command=lambda: gate.respond(allow=True, persist=False, win=top)).pack(side="right")

        top.lift()
        top.attributes("-topmost", True)
        top.after(200, lambda: top.attributes("-topmost", False))

    def _on_audit_entry(self, entry: ToolCallAuditEntry) -> None:
        # Persist to disk + remember in memory + refresh GUI panel.
        try:
            self.audit_writer.append(entry)
        except Exception:
            pass
        self.root.after(0, lambda: self._append_audit_row(entry))

    # ------------------------------------------------------------------
    # Trust management dialog (Phase 2.1 polish)
    # ------------------------------------------------------------------
    def _show_trust_dialog(self) -> None:
        top = tk.Toplevel(self.root)
        top.title("Trusted devices")
        top.geometry("640x420")
        top.transient(self.root)

        wrap = ttk.Frame(top, padding=10)
        wrap.pack(fill="both", expand=True)

        ttk.Label(
            wrap,
            text=(
                "Per-(peer, tool) decisions saved by 'Always allow' / 'Always deny'.\n"
                "Select a row and press 'Revoke' to clear that entry; future calls will "
                "ask again."
            ),
            foreground="#555",
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        cols = ("peer", "tool", "decision")
        tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended", height=12)
        tree.heading("peer", text="Peer device-id")
        tree.heading("tool", text="Tool")
        tree.heading("decision", text="Decision")
        tree.column("peer", width=280, anchor="w")
        tree.column("tool", width=180, anchor="w")
        tree.column("decision", width=100, anchor="w")
        tree.pack(fill="both", expand=True)

        def refresh() -> None:
            for iid in tree.get_children():
                tree.delete(iid)
            snap = self.trust_policy.snapshot()
            if not snap:
                tree.insert("", "end", values=("(empty)", "—", "—"))
                return
            for peer_id, tools in sorted(snap.items()):
                for tool, decision in sorted(tools.items()):
                    tree.insert(
                        "", "end",
                        iid=f"{peer_id}::{tool}",
                        values=(peer_id[:36], tool, decision),
                    )

        def revoke_selected() -> None:
            sel = tree.selection()
            for iid in sel:
                if "::" not in iid:
                    continue
                peer_id, tool = iid.split("::", 1)
                self.trust_policy.clear(peer_id, tool)
            refresh()

        def revoke_peer() -> None:
            sel = tree.selection()
            peers = {iid.split("::", 1)[0] for iid in sel if "::" in iid}
            for p in peers:
                self.trust_policy.clear(p)
            refresh()

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Revoke selected entry", command=revoke_selected).pack(side="left")
        ttk.Button(btns, text="Revoke entire peer", command=revoke_peer).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Refresh", command=refresh).pack(side="left", padx=(12, 0))
        ttk.Button(btns, text="Close", command=top.destroy).pack(side="right")

        refresh()

    def _append_audit_row(self, entry: ToolCallAuditEntry) -> None:
        self._audit_entries.append(entry)
        if not hasattr(self, "audit_tree"):
            return
        ts = time.strftime("%H:%M:%S", time.localtime(entry.timestamp))
        arrow = "↓" if entry.direction == "inbound" else "↑"
        summary = entry.error or entry.result_summary or ""
        self.audit_tree.insert(
            "", 0,
            values=(ts, f"{arrow} {entry.direction}", entry.peer_name, entry.tool_name,
                    entry.decision, summary[:60]),
        )
        # Keep only recent 200 rows
        children = self.audit_tree.get_children()
        if len(children) > 200:
            for iid in children[200:]:
                self.audit_tree.delete(iid)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        try:
            self.discovery.stop()
        except Exception:
            pass
        try:
            self.transfer.stop()
        except Exception:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    SafeDropApp().run()
