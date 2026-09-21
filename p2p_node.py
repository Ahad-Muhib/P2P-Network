"""p2p_node.py -- the networking core of one peer.

Rule from the assignment: every peer = TCP server + TCP client (section 8).
A peer's only job is to open connections, perform the HELLO handshake,
exchange framed JSON messages and streamed file bytes, and survive peers
that disappear.  All user interaction lives in main.py.

Design (assignment sections 10.1 -- 10.10)
------------------------------------------
* P2PNode.start() opens the listening socket and spawns the accept loop.
* P2PNode.connect_to() dials a remote peer, does the HELLO handshake,
  registers the peer and starts ONE receiver thread for that connection.
* The accept loop spawns ONE handler thread per incoming connection.  The
  handler does the handshake and then runs the same receiver loop.  This is
  the "one thread per connection, so a peer can talk to many peers at once"
  requirement (section 10.4).
* Connected peers live in a dict {peer_id: PeerConnection} protected by a
  LOCK because background threads of different connections plus the GUI
  thread all touch it.  Each PeerConnection also has its own send lock so
  text frames can never interleave with the raw bytes of an in-flight file.
* The node never imports Tkinter.  It reports everything through callbacks
  (on_log, on_peers_changed, ...) so it can be tested with no display.
"""

import os
import socket
import threading
import uuid

from protocol import (
    CHUNK_SIZE,
    make_file,
    make_hello,
    make_hello_ack,
    make_text,
    recv_exact,
    recv_message,
    send_message,
)


def _close(sock):
    """Close a socket defensively (shutdown is best-effort)."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


# --- small network helpers -------------------------------------------------

_lan_ip_cache = None
_lan_ip_lock = threading.Lock()


def get_lan_ip():
    """Return this machine's LAN IP without sending any packet.

    A UDP socket's connect() only picks a local address and "route"; it
    never transmits anything on the network.  If that fails (no network),
    fall back to loopback.
    """
    global _lan_ip_cache
    with _lan_ip_lock:
        if _lan_ip_cache is None:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("8.8.8.8", 80))
                _lan_ip_cache = probe.getsockname()[0]
            except OSError:
                _lan_ip_cache = "127.0.0.1"
            finally:
                probe.close()
        return _lan_ip_cache


class PeerConnection(object):
    """Everything this peer knows about one open connection."""

    __slots__ = ("sock", "peer_id", "peer_name", "ip", "port",
                 "inbound", "send_lock")

    def __init__(self, sock, peer_id, peer_name, ip, port, inbound):
        self.sock = sock
        self.peer_id = peer_id            # 8 hex chars, learned in the handshake
        self.peer_name = peer_name
        self.ip = ip
        self.port = port                  # the remote peer's LISTENING port
        self.inbound = inbound            # True if this side accepted the TCP
        self.send_lock = threading.Lock()  # serialises ALL writes to this socket


class P2PNode(object):
    """One peer: listening socket + client connections + thread per peer."""

    CONNECT_TIMEOUT = 5.0    # seconds for connect() and the HELLO handshake

    def __init__(self, downloads_dir="downloads", on_log=None,
                 on_peers_changed=None, on_incoming_text=None,
                 on_incoming_file=None, on_file_sent=None):
        # Generated once at startup: first 8 hex chars of a uuid4.
        self.peer_id = uuid.uuid4().hex[:8]
        self.peer_name = None
        self.port = None
        self.downloads_dir = os.path.abspath(downloads_dir)

        self._server_sock = None
        self._running = False
        self._stopping = False

        self._peers = {}                    # peer_id -> PeerConnection
        self._peers_lock = threading.Lock()
        self._threads = set()

        # UI callbacks.  The GUI wraps these so everything is marshalled
        # through a queue.Queue -- the node itself never touches a widget.
        self._on_log = on_log or (lambda line: print(line))
        self._on_peers_changed = on_peers_changed or (lambda: None)
        self._on_incoming_text = on_incoming_text or (
            lambda sender_id, sender_name, text: None)
        self._on_incoming_file = on_incoming_file or (
            lambda peer_id, peer_name, filename: None)
        self._on_file_sent = on_file_sent or (
            lambda peer_name, filename: None)

    # --- public API --------------------------------------------------------

    @property
    def is_running(self):
        return self._running

    def peer_list(self):
        """Snapshot of connected peers (safe to call from any thread)."""
        with self._peers_lock:
            items = [{"peer_id": c.peer_id, "peer_name": c.peer_name,
                      "ip": c.ip, "port": c.port}
                     for c in self._peers.values()]
        items.sort(key=lambda d: d["peer_name"].lower())
        return items

    def start(self, name, port):
        """Start the server half: bind, listen, run the accept loop."""
        if self._running:
            self._on_log("[ERROR] Peer is already running")
            return False
        if not isinstance(name, str) or not name.strip():
            self._on_log("[ERROR] Peer name cannot be empty")
            return False
        try:
            port = int(port)
        except (TypeError, ValueError):
            self._on_log("[ERROR] Invalid port: %r" % (port,))
            return False
        if not (0 <= port <= 65535):     # 0 = let the OS pick (used by tests)
            self._on_log("[ERROR] Port must be between 0 and 65535")
            return False

        try:
            server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # SO_REUSEADDR lets us immediately rebind after a restart instead
            # of waiting for the OS to recycle TIME_WAIT ports.
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Bind 0.0.0.0 so peers on loopback AND the LAN can reach us.
            server_sock.bind(("0.0.0.0", port))
            server_sock.listen(5)
        except OSError as exc:
            self._on_log("[ERROR] Cannot listen on port %s: %s" % (port, exc))
            return False

        self.peer_name = name.strip()
        self.port = server_sock.getsockname()[1]  # real port (handles port 0)
        self._server_sock = server_sock
        self._running = True
        self._stopping = False

        self._spawn(self._accept_loop)
        self._on_log("[SYSTEM] Peer started: %s [%s] on port %s"
                     % (self.peer_name, self.peer_id, self.port))
        return True

    def stop(self):
        """Tear everything down so the peer can be started again later."""
        if not self._running and self._server_sock is None:
            return
        self._running = False
        self._stopping = True              # suppress "disconnected" noise
        if self._server_sock is not None:
            _close(self._server_sock)
            self._server_sock = None
        with self._peers_lock:
            conns = list(self._peers.values())
            self._peers.clear()
        for conn in conns:
            _close(conn.sock)              # this unblocks its receiver thread
        # Daemon threads exit as soon as their blocking call returns; give
        # them a brief chance to notice, then carry on regardless.
        for t in list(self._threads):
            t.join(timeout=0.5)
        self._threads.clear()
        self._on_peers_changed()
        self._on_log("[SYSTEM] Peer stopped")
        self.peer_name = None
        self.port = None
        self._stopping = False

    def connect_to(self, ip, port):
        """Client half: TCP connect, HELLO handshake, register, receive thread."""
        if not self._running:
            self._on_log("[ERROR] Start the peer first")
            return False
        ip = ip.strip()
        if not ip:
            self._on_log("[ERROR] Invalid IP address")
            return False
        try:
            socket.inet_aton(ip)
        except OSError:
            # Maybe it is a hostname like "localhost" -- try to resolve it.
            try:
                ip = socket.gethostbyname(ip)
            except OSError:
                self._on_log("[ERROR] Invalid IP address: %s" % ip)
                return False
        try:
            port = int(port)
        except (TypeError, ValueError):
            self._on_log("[ERROR] Invalid port: %r" % (port,))
            return False
        if not (1 <= port <= 65535):
            self._on_log("[ERROR] Port must be between 1 and 65535")
            return False
        if self._is_own(ip) and port == self.port:
            self._on_log("[ERROR] Cannot connect to yourself")
            return False

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.CONNECT_TIMEOUT)     # connect() must not hang
        try:
            sock.connect((ip, port))
        except OSError as exc:
            self._on_log("[ERROR] Connection failed: %s" % exc)
            sock.close()
            return False

        try:
            # Stage 1: introduce ourselves with the HELLO message.
            send_message(sock, make_hello(self.peer_id, self.peer_name,
                                          self.port))
            # Stage 2: only trust the peer once it answers with HELLO_ACK.
            ack = recv_message(sock)
            if not isinstance(ack, dict) or ack.get("type") != "hello_ack":
                self._on_log("[ERROR] Peer did not complete the HELLO handshake")
                sock.close()
                return False
            peer_id = ack.get("peer_id")
            peer_name = ack.get("peer_name")
            peer_port = ack.get("port")
            if not (isinstance(peer_id, str) and isinstance(peer_name, str)):
                self._on_log("[ERROR] Peer sent a malformed HELLO_ACK")
                sock.close()
                return False
            if peer_id == self.peer_id:
                self._on_log("[ERROR] Cannot connect to yourself")
                sock.close()
                return False
            try:
                peer_port = int(peer_port)
            except (TypeError, ValueError):
                peer_port = 0
        except (ConnectionError, ValueError, OSError) as exc:
            self._on_log("[ERROR] Connection failed: %s" % exc)
            sock.close()
            return False

        sock.settimeout(None)                    # long-lived connection now

        # Refuse a second connection to a peer we already talk to.
        with self._peers_lock:
            if peer_id in self._peers:
                self._on_log("[ERROR] Already connected to %s (%s); "
                             "closing duplicate connection"
                             % (peer_name, peer_id))
                sock.close()
                return False
            conn = PeerConnection(sock, peer_id, peer_name, ip, peer_port,
                                  inbound=False)
            self._peers[peer_id] = conn

        self._on_peers_changed()
        self._on_log("[SYSTEM] Connected to %s" % peer_name)
        self._spawn(self._receive_loop, conn)
        return True

    def send_text(self, peer_id, text):
        """Send one text message.  Returns True on success."""
        if not self._running:
            self._on_log("[ERROR] Start the peer first")
            return False
        peer = self._peer(peer_id)
        if peer is None:
            self._on_log("[ERROR] That peer is no longer connected")
            return False
        if not isinstance(text, str) or not text:
            self._on_log("[ERROR] Message cannot be empty")
            return False
        try:
            with peer.send_lock:
                send_message(peer.sock, make_text(self.peer_id,
                                                  self.peer_name, text))
            return True
        except OSError as exc:
            self._on_log("[ERROR] Failed to send to %s: %s"
                         % (peer.peer_name, exc))
            self._drop_peer(peer)
            return False

    def send_file(self, peer_id, path):
        """Validate a file then stream it in a background thread.

        The GUI must not freeze while a large video is being sent, so the
        actual transfer runs in _send_file_worker.  This method only does
        cheap validation and returns immediately.
        """
        if not self._running:
            self._on_log("[ERROR] Start the peer first")
            return False
        peer = self._peer(peer_id)
        if peer is None:
            self._on_log("[ERROR] That peer is no longer connected")
            return False
        if not os.path.isfile(path):
            self._on_log("[ERROR] File does not exist: %s" % path)
            return False
        try:
            filesize = os.path.getsize(path)
        except OSError as exc:
            self._on_log("[ERROR] Cannot stat %s: %s" % (path, exc))
            return False
        if filesize < 0:
            self._on_log("[ERROR] Invalid file size: %d" % filesize)
            return False
        filename = os.path.basename(path)
        self._spawn(self._send_file_worker, peer_id, path, filename, filesize)
        return True

    # --- internal threads --------------------------------------------------

    def _spawn(self, target, *args):
        """Run target(*args) in a daemon thread.

        Daemon threads can never keep the process alive after stop(), and
        stop() only has to unblock their blocking calls (close the socket).
        """
        self._threads = {t for t in self._threads if t.is_alive()}
        t = threading.Thread(target=target, args=args, daemon=True)
        t.start()
        self._threads.add(t)
        return t

    def _accept_loop(self):
        """Endlessly accept connections; one handler thread per connection."""
        while self._running:
            try:
                conn, addr = self._server_sock.accept()
            except OSError:
                break        # stop() closed the listener -> normal exit
            self._spawn(self._handle_incoming, conn, addr)

    def _handle_incoming(self, sock, addr):
        """Handshake with a new connection, then read everything it sends."""
        ip = addr[0]
        if not self._running:
            _close(sock)
            return
        try:
            hello = recv_message(sock)
            if not isinstance(hello, dict) or hello.get("type") != "hello":
                self._on_log("[ERROR] Bad HELLO from %s; closing connection" % ip)
                _close(sock)
                return
            peer_id = hello.get("peer_id")
            peer_name = hello.get("peer_name")
            if not (isinstance(peer_id, str) and isinstance(peer_name, str)):
                self._on_log("[ERROR] Malformed HELLO from %s; closing" % ip)
                _close(sock)
                return
            try:
                peer_port = int(hello.get("port") or 0)
            except (TypeError, ValueError):
                peer_port = 0
            if peer_id == self.peer_id:
                self._on_log("[ERROR] Rejected a connection from myself")
                _close(sock)
                return
            with self._peers_lock:
                if peer_id in self._peers:
                    self._on_log("[ERROR] Already connected to %s (%s); "
                                 "closing duplicate connection"
                                 % (peer_name, peer_id))
                    _close(sock)
                    return
                conn = PeerConnection(sock, peer_id, peer_name, ip,
                                      peer_port, inbound=True)
                self._peers[peer_id] = conn
        except (ConnectionError, ValueError, OSError) as exc:
            self._on_log("[ERROR] Incoming connection failure from %s: %s"
                         % (ip, exc))
            _close(sock)
            return

        # Answer the handshake so the initiator can finish connecting.
        try:
            send_message(sock, make_hello_ack(self.peer_id, self.peer_name,
                                              self.port))
        except OSError as exc:
            self._on_log("[ERROR] Handshake with %s failed: %s"
                         % (peer_name, exc))
            self._drop_peer(conn)
            _close(sock)
            return

        self._on_peers_changed()
        self._on_log("[SYSTEM] Peer %s connected" % peer_name)
        # This thread IS the receiver for this connection now.
        self._receive_loop(conn)

    def _receive_loop(self, conn):
        """Read framed messages for one connection until it dies."""
        while self._running and conn.peer_id in self._peers:
            try:
                msg = recv_message(conn.sock)
                # Dispatch inside the try: an error raised while handling a
                # message (e.g. the peer dies mid-file) must also be turned
                # into a clean shutdown, never an uncaught thread exception.
                self._dispatch(conn, msg)
            except ConnectionError:
                self._drop_peer(conn)          # peer closed / dropped
                return
            except (ValueError, OSError) as exc:
                # Garbage, an absurd length, or a write error: we cannot
                # trust the stream, so shut this connection down cleanly.
                self._on_log("[ERROR] Connection error with %s: %s; "
                             "closing connection" % (conn.peer_name, exc))
                self._drop_peer(conn)
                _close(conn.sock)
                return

    def _dispatch(self, conn, msg):
        mtype = msg.get("type")
        if mtype == "text":
            text = msg.get("message")
            sender_name = msg.get("sender_name", conn.peer_name)
            if not isinstance(text, str):
                self._on_log("[ERROR] Malformed text message from %s"
                             % conn.peer_name)
                return
            self._on_log("%s -> You: %s" % (sender_name, text))
            self._on_incoming_text(conn.peer_id, sender_name, text)
        elif mtype == "file":
            self._receive_file(conn, msg)
        elif mtype in ("hello", "hello_ack"):
            # The handshake already completed; seeing it again is a protocol
            # error, so treat the connection as unusable.
            self._on_log("[ERROR] Unexpected %r after handshake from %s; "
                         "closing connection" % (mtype, conn.peer_name))
            self._drop_peer(conn)
            _close(conn.sock)
        else:
            self._on_log("[ERROR] Unknown message type %r from %s"
                         % (mtype, conn.peer_name))

    def _receive_file(self, conn, msg):
        """Read exactly filesize raw bytes and write them to downloads/.

        The size comes from the metadata, NEVER from the number of recv()
        calls: the file stream ends when we have consumed filesize bytes
        (assignment section 11).
        """
        filesize = msg.get("filesize")
        if isinstance(filesize, bool) or not isinstance(filesize, int) \
                or filesize < 0:
            # We cannot know how many bytes follow, so the stream is
            # unrecoverable on this connection.  Close it cleanly.
            self._on_log("[ERROR] Invalid file size %r from %s; "
                         "closing connection" % (filesize, conn.peer_name))
            self._drop_peer(conn)
            _close(conn.sock)
            return

        raw_name = msg.get("filename") or ""
        # os.path.basename strips any directory parts, so a hostile
        # "../../../x" arrives as plain "x" (blocks path traversal).
        safe_name = os.path.basename(raw_name.replace("\\", "/"))
        if not safe_name:
            safe_name = "download_%s.bin" % uuid.uuid4().hex[:8]

        os.makedirs(self.downloads_dir, exist_ok=True)
        target = self._unique_path(self.downloads_dir, safe_name)
        part_path = target + ".part"      # write to .part, rename when done

        try:
            with open(part_path, "wb") as out:
                remaining = filesize
                while remaining > 0:
                    chunk = recv_exact(conn.sock, min(CHUNK_SIZE, remaining))
                    out.write(chunk)
                    remaining -= len(chunk)
            # os.replace is atomic: downloads/ never contains a half-written
            # file that looks complete.
            os.replace(part_path, target)
        except ConnectionError:
            # Peer vanished mid-file -> delete the .part file.
            self._quiet_remove(part_path)
            raise
        except OSError as exc:
            self._on_log("[ERROR] Cannot save file %s: %s" % (safe_name, exc))
            self._quiet_remove(part_path)
            raise

        self._on_log("%s -> You: File received: %s" % (conn.peer_name, safe_name))
        self._on_incoming_file(conn.peer_id, conn.peer_name, target)

    def _send_file_worker(self, peer_id, path, filename, filesize):
        """Background thread that actually streams a file (§10.9)."""
        peer = self._peer(peer_id)
        if peer is None:
            self._on_log("[ERROR] Peer disconnected before the file could "
                         "be sent")
            return
        try:
            header = make_file(self.peer_id, self.peer_name, filename, filesize)
            with peer.send_lock:
                # Stage 1: framed JSON metadata.
                send_message(peer.sock, header)
                # Stage 2: raw bytes.  The send lock guarantees no text
                # frame can slip between the header and the file bytes.
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        peer.sock.sendall(chunk)
            self._on_file_sent(peer.peer_name, filename)
        except OSError as exc:
            if not self._stopping:
                self._on_log("[ERROR] File send to %s failed: %s"
                             % (peer.peer_name, exc))
            self._drop_peer(peer)

    # --- helpers -----------------------------------------------------------

    def _peer(self, peer_id):
        with self._peers_lock:
            return self._peers.get(peer_id)

    def _drop_peer(self, conn):
        """Forget a peer and close its socket.  Idempotent."""
        with self._peers_lock:
            if self._peers.get(conn.peer_id) is conn:
                del self._peers[conn.peer_id]
                changed = True
            else:
                changed = False
        _close(conn.sock)
        if changed and not self._stopping:
            self._on_log("[SYSTEM] Peer %s disconnected" % conn.peer_name)
            self._on_peers_changed()

    def _unique_path(self, directory, filename):
        """Return a free path, using 'name (1).ext' style suffixes."""
        stem, ext = os.path.splitext(filename)
        candidate = os.path.join(directory, filename)
        counter = 1
        while os.path.exists(candidate):
            candidate = os.path.join(directory, "%s (%d)%s" % (stem, counter, ext))
            counter += 1
        return candidate

    @staticmethod
    def _quiet_remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _is_own(self, ip):
        """True if ip is an address of THIS machine (loopback / LAN)."""
        try:
            resolved = socket.gethostbyname(ip)   # also handles "localhost"
        except OSError:
            resolved = ip
        if resolved == "127.0.0.1":
            return True
        lan = get_lan_ip()
        return lan != "127.0.0.1" and resolved == lan