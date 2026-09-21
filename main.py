"""main.py -- Tkinter GUI for the P2P chat and file sharing app.

Everything in this file is user interface only.  All sockets, threads and
protocol work live in p2p_node.P2PNode, which deliberately knows nothing
about Tkinter (so the whole network can be tested without a display).

THREAD SAFETY (IMPORTANT -- read before touching callbacks)
-----------------------------------------------------------
P2PNode runs background threads: an accept loop, one receiver thread per
connection, and one background thread per file being sent.  Those threads
invoke our callbacks (on_log / on_peers_changed / on_file_sent).  A
Tkinter rule is that widgets may ONLY be touched from the thread that runs
the Tk main loop -- touching them from a background thread can crash or
freeze the GUI.  So each callback does the only thread-safe thing it can:
push an event onto a queue.Queue.  The GUI thread drains that queue every
100 ms with root.after(100, ...) and updates the widgets there.
"""

import queue
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from p2p_node import P2PNode, get_lan_ip

# GUI size
WINDOW_W = 940
WINDOW_H = 620


class ChatApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("P2P Chat & File Sharing - CSE 433")
        self.geometry("%dx%d" % (WINDOW_W, WINDOW_H))
        self.minsize(760, 520)

        self.node = None           # a P2PNode while a peer is running
        self.peer_ids = {}         # listbox index -> peer_id
        self.status_var = tk.StringVar(value="Not started")
        self.event_queue = queue.Queue()   # node threads -> GUI hand-off
        self._drain_after_id = None

        self._build_widgets()

        # Drain queued node events every 100 ms on the GUI thread.
        self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Widgets (layout follows assignment section 12 / Figures 3-5)
    # ------------------------------------------------------------------
    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        # --- My Peer ---------------------------------------------------
        top = ttk.LabelFrame(self, text="My Peer")
        top.grid(row=0, column=0, sticky="we", padx=8, pady=(8, 2))
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Name:").grid(row=0, column=0, sticky="w", **pad)
        self.name_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.name_var, width=18).grid(
            row=0, column=1, sticky="we", **pad)

        ttk.Label(top, text="Port:").grid(row=0, column=2, sticky="w", **pad)
        self.port_var = tk.StringVar(value="5000")
        ttk.Entry(top, textvariable=self.port_var, width=8).grid(
            row=0, column=3, **pad)

        self.btn_start = ttk.Button(top, text="Start Peer", command=self._start_peer)
        self.btn_start.grid(row=0, column=4, **pad)
        self.btn_stop = ttk.Button(top, text="Stop", command=self._stop_peer,
                                   state="disabled")
        self.btn_stop.grid(row=0, column=5, **pad)

        # "Name | ID: xxxx | Port: xxxx" header line (Figure 3)
        self.header_var = tk.StringVar(value="Name | ID: - | Port: -")
        ttk.Label(top, textvariable=self.header_var, font=("", 9, "bold")).grid(
            row=1, column=0, columnspan=6, sticky="w", padx=6, pady=(0, 4))

        # --- Connect to another peer ------------------------------------
        conn = ttk.LabelFrame(self, text="Connect to Another Peer")
        conn.grid(row=1, column=0, sticky="we", padx=8, pady=2)
        conn.columnconfigure(3, weight=1)

        ttk.Label(conn, text="IP:").grid(row=0, column=0, sticky="w", **pad)
        self.ip_var = tk.StringVar(value="127.0.0.1")
        ttk.Entry(conn, textvariable=self.ip_var, width=16).grid(
            row=0, column=1, **pad)
        ttk.Label(conn, text="Port:").grid(row=0, column=2, sticky="w", **pad)
        self.remote_port_var = tk.StringVar(value="5001")
        ttk.Entry(conn, textvariable=self.remote_port_var, width=8).grid(
            row=0, column=3, sticky="we", **pad)

        self.btn_connect = ttk.Button(conn, text="Connect", command=self._connect,
                                      state="disabled")
        self.btn_connect.grid(row=0, column=4, **pad)

        # --- Peer list + log side by side --------------------------------
        middle = ttk.Frame(self)
        middle.grid(row=2, column=0, sticky="nsew", padx=8, pady=2)
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)
        middle.rowconfigure(0, weight=1)
        middle.columnconfigure(1, weight=1)

        peers_frame = ttk.LabelFrame(middle, text="Connected Peers")
        peers_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.peer_listbox = tk.Listbox(peers_frame, width=32, activestyle="dotbox")
        self.peer_listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(peers_frame, orient="vertical",
                               command=self.peer_listbox.yview)
        scroll.pack(side="right", fill="y")
        self.peer_listbox.configure(yscrollcommand=scroll.set)

        log_frame = ttk.LabelFrame(middle, text="Messages / Events")
        log_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self.log_text = tk.Text(log_frame, state="disabled", wrap="word",
                                width=64)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll2 = ttk.Scrollbar(log_frame, orient="vertical",
                                command=self.log_text.yview)
        scroll2.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll2.set)

        # --- Send text ----------------------------------------------------
        send = ttk.LabelFrame(self, text="Send Text")
        send.grid(row=3, column=0, sticky="we", padx=8, pady=2)
        send.columnconfigure(0, weight=1)

        self.msg_var = tk.StringVar(value="")
        msg_entry = ttk.Entry(send, textvariable=self.msg_var)
        msg_entry.grid(row=0, column=0, sticky="we", padx=6, pady=4)
        msg_entry.bind("<Return>", lambda e: self._send_text())

        self.btn_send = ttk.Button(send, text="Send", command=self._send_text,
                                   state="disabled")
        self.btn_send.grid(row=0, column=1, padx=6, pady=4)
        self.btn_file = ttk.Button(send, text="Choose File & Send",
                                   command=self._send_file, state="disabled")
        self.btn_file.grid(row=0, column=2, padx=(0, 6), pady=4)

        # --- Status bar + LAN IP -------------------------------------------
        status = ttk.Frame(self)
        status.grid(row=4, column=0, sticky="we", padx=8, pady=(2, 6))
        status.columnconfigure(1, weight=1)
        ttk.Label(status, textvariable=self.status_var).grid(
            row=0, column=0, sticky="w")
        lan = get_lan_ip()
        ttk.Label(status, text="This PC's LAN IP: %s (use this on another "
                               "PC)" % lan, foreground="#444444").grid(
            row=0, column=1, sticky="e")

    # ------------------------------------------------------------------
    # Actions (all run on the GUI thread)
    # ------------------------------------------------------------------
    def _parse_port(self, raw, field):
        try:
            port = int(raw.strip())
        except (TypeError, ValueError):
            self._append_log("[ERROR] %s: '%s' is not a number" % (field, raw))
            return None
        if not (1 <= port <= 65535):
            self._append_log("[ERROR] %s: port must be 1-65535" % field)
            return None
        return port

    def _start_peer(self):
        if self.node is not None:
            self._append_log("[ERROR] A peer is already running; stop it first")
            return
        name = self.name_var.get().strip()
        if not name:
            self._append_log("[ERROR] Peer name cannot be empty")
            return
        port = self._parse_port(self.port_var.get(), "Local port")
        if port is None:
            return

        # A fresh node per start -> peer_id is generated once at start
        # (see 'peer_id = first 8 hex chars of uuid4' in P2PNode).
        node = P2PNode(downloads_dir="downloads",
                       on_log=lambda line: self.event_queue.put(("log", line)),
                       on_peers_changed=lambda: self.event_queue.put(("peers", None)),
                       on_file_sent=lambda p, f: self.event_queue.put(("file_sent", (p, f))))
        if not node.start(name, port):
            return
        self.node = node
        self.header_var.set("%s | ID: %s | Port: %s"
                            % (node.peer_name, node.peer_id, node.port))
        self.status_var.set("Running on port %d" % node.port)
        self._update_buttons()

    def _stop_peer(self):
        if self.node is not None:
            self.node.stop()
            self.node = None
        self.header_var.set("Name | ID: - | Port: -")
        self.status_var.set("Not started")
        self.peer_listbox.delete(0, "end")
        self.peer_ids = {}
        self._update_buttons()

    def _connect(self):
        if self.node is None:
            self._append_log("[ERROR] Start the peer first")
            return
        ip = self.ip_var.get().strip()
        port = self._parse_port(self.remote_port_var.get(), "Remote port")
        if port is None:
            return
        self.node.connect_to(ip, port)   # errors are logged by the node

    def _selected_peer_id(self):
        if self.node is None:
            return None
        selection = self.peer_listbox.curselection()
        if not selection:
            return None
        return self.peer_ids.get(selection[0])

    def _selected_peer_name(self):
        selection = self.peer_listbox.curselection()
        if not selection:
            return None
        item = self.peer_listbox.get(selection[0])
        # Listbox lines look like:  Ray [a83f21c4] 127.0.0.1:5001
        return item.split(" [")[0]

    def _send_text(self):
        if self.node is None:
            self._append_log("[ERROR] Start the peer first")
            return
        peer_id = self._selected_peer_id()
        if peer_id is None:
            self._append_log("[ERROR] No peer selected")
            messagebox.showwarning("P2P Chat", "Select a peer in the list first.")
            return
        text = self.msg_var.get().strip()
        if not text:
            self._append_log("[ERROR] Message cannot be empty")
            return
        if self.node.send_text(peer_id, text):
            self._append_log("You -> %s: %s" % (self._selected_peer_name(), text))
            self.msg_var.set("")

    def _send_file(self):
        if self.node is None:
            self._append_log("[ERROR] Start the peer first")
            return
        peer_id = self._selected_peer_id()
        if peer_id is None:
            self._append_log("[ERROR] No peer selected")
            messagebox.showwarning("P2P Chat", "Select a peer in the list first.")
            return
        path = filedialog.askopenfilename(title="Choose a file to send")
        if not path:
            return                          # user cancelled the dialog
        if self.node.send_file(peer_id, path):
            # "File sent" is confirmed by the node's on_file_sent event,
            # which arrives through the queue as a "file_sent" event.
            self.status_var.set("Sending %s ..." % os.path.basename(path))

    # ------------------------------------------------------------------
    # Event draining (GIU thread, NEVER touched by node threads)
    # ------------------------------------------------------------------
    def _drain_events(self):
        self._drain_after_id = None
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "peers":
                    self._refresh_peers()
                elif kind == "file_sent":
                    peer_name, filename = payload
                    self._append_log("You -> %s: File sent: %s"
                                     % (peer_name, filename))
                    self.status_var.set("File sent to %s" % peer_name)
        except queue.Empty:
            pass
        self._drain_after_id = self.after(100, self._drain_events)  # keep polling

    def _append_log(self, line):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.status_var.set(line)

    def _refresh_peers(self):
        if self.node is None:              # a stale "peers" event after Stop
            return
        self.peer_listbox.delete(0, "end")
        self.peer_ids = {}
        last_selected = None
        selection = self.peer_listbox.curselection()
        if selection:
            last_selected = self.peer_ids.get(selection[0])
        for index, peer in enumerate(self.node.peer_list()):
            label = "%s [%s] %s:%d" % (peer["peer_name"], peer["peer_id"],
                                       peer["ip"], peer["port"])
            self.peer_listbox.insert("end", label)
            self.peer_ids[index] = peer["peer_id"]
            if peer["peer_id"] == last_selected:
                self.peer_listbox.selection_set(index)

    def _update_buttons(self):
        running = self.node is not None
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.btn_connect.configure(state="normal" if running else "disabled")
        self.btn_send.configure(state="normal" if running else "disabled")
        self.btn_file.configure(state="normal" if running else "disabled")

    # ------------------------------------------------------------------
    # Clean shutdown
    # ------------------------------------------------------------------
    def _on_close(self):
        if self._drain_after_id is not None:
            try:
                self.after_cancel(self._drain_after_id)
            except Exception:
                pass                      # window may already be destroyed
        try:
            if self.node is not None:
                self.node.stop()
        finally:
            self.destroy()


if __name__ == "__main__":
    app = ChatApp()
    app.mainloop()