"""test_p2p.py -- headless integration tests for the P2P network.

No Tkinter and no third-party packages (plain unittest).  Every node runs
on 127.0.0.1 with an OS-assigned free port (port 0) and its own temp
downloads folder.

Run with:   python test_p2p.py
or:         python -m unittest test_p2p -v

Covered (assignment section 13):
  * HELLO handshake and connected-peer lists
  * text messaging between peers
  * binary file transfer: small text, ~5 MB blob (spans many 64 KiB chunks
    and is NOT a multiple of 64 KiB), 0-byte file, Unicode filename
  * framing + per-connection send locking: text right after a big file
  * abrupt disconnect of one node; others survive and keep talking
  * error cases: closed port, invalid IP, self-connection, send before
    start, missing file, and garbage/malicious remote data
  * stop() then start() again works
"""

import hashlib
import os
import socket
import tempfile
import threading
import time
import unittest
import uuid

from p2p_node import P2PNode, get_lan_ip
from protocol import make_hello, recv_message, send_message


def hash_file(path):
    """SHA-256 of a file, streamed so memory stays flat."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def free_port():
    """Ask the OS for a free port, close it, and return the number."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Harness(object):
    """Wraps a P2PNode and records its callbacks for assertions."""

    def __init__(self, name):
        self.name = name
        self.records_lock = threading.Lock()
        self.log_line = []
        self.incoming_text = []       # (sender_id, sender_name, text)
        self.incoming_files = []      # (peer_id, peer_name, saved_path)
        self.files_sent = []          # (peer_name, filename)
        self.downloads = tempfile.mkdtemp(prefix="p2p_dl_")
        self.node = P2PNode(
            downloads_dir=self.downloads,
            on_log=self._record_log,
            on_peers_changed=lambda: None,
            on_incoming_text=self._record_text,
            on_incoming_file=self._record_file,
            on_file_sent=self._record_sent,
        )

    # -- callback plumbing (called from node threads) -----------------------
    def _record_log(self, line):
        with self.records_lock:
            self.log_line.append(line)

    def _record_text(self, sender_id, sender_name, text):
        with self.records_lock:
            self.incoming_text.append((sender_id, sender_name, text))

    def _record_file(self, peer_id, peer_name, saved_path):
        with self.records_lock:
            self.incoming_files.append((peer_id, peer_name, saved_path))

    def _record_sent(self, peer_name, filename):
        with self.records_lock:
            self.files_sent.append((peer_name, filename))

    # -- helpers ------------------------------------------------------------
    def logs(self):
        with self.records_lock:
            return list(self.log_line)

    def has_log(self, *substrings):
        return any(all(sub in line for sub in substrings) for line in self.logs())

    def texts_from(self, sender_name):
        with self.records_lock:
            return [t[2] for t in self.incoming_text if t[1] == sender_name]

    def peer_ids(self):
        return {p["peer_id"] for p in self.node.peer_list()}

    def find_peer(self, peer_name):
        for p in self.node.peer_list():
            if p["peer_name"] == peer_name:
                return p
        return None

    def received_hash_set(self):
        found = set()
        for entry in os.listdir(self.downloads):
            path = os.path.join(self.downloads, entry)
            if entry.endswith(".part") or not os.path.isfile(path):
                continue
            found.add(hash_file(path))
        return found

    def stop(self):
        self.node.stop()


def wait_until(condition, timeout=10.0, interval=0.02):
    """Poll condition() until it is True or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def start_all(harnesses):
    ok = True
    for h in harnesses:
        ok = ok and h.node.start(h.name, 0)
    return ok


class TestP2P(unittest.TestCase):

    def setUp(self):
        # Every test starts with three fresh peers on free ports.
        self.alice = Harness("Alice")
        self.bob = Harness("Bob")
        self.charlie = Harness("Charlie")
        self.assertTrue(start_all([self.alice, self.bob, self.charlie]))
        self.A = self.alice.node.peer_id
        self.B = self.bob.node.peer_id
        self.C = self.charlie.node.peer_id

    def tearDown(self):
        for h in (self.alice, self.bob, self.charlie):
            h.stop()

    # ----------------------------------------------------------------------
    # 1. HELLO handshake + connected peer lists
    # ----------------------------------------------------------------------
    def test_handshake_and_peer_lists(self):
        self.assertTrue(self.bob.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(self.charlie.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(self.charlie.node.connect_to("127.0.0.1", self.bob.node.port))

        # Each side eventually sees exactly the right peers.
        self.assertTrue(wait_until(lambda: self.alice.peer_ids() == {self.B, self.C}))
        self.assertTrue(wait_until(lambda: self.bob.peer_ids() == {self.A, self.C}))
        self.assertTrue(wait_until(lambda: self.charlie.peer_ids() == {self.A, self.B}))

        # Identity learned via the handshake matches reality.
        bob_entry = self.alice.find_peer("Bob")
        self.assertIsNotNone(bob_entry)
        self.assertEqual(bob_entry["peer_id"], self.B)
        self.assertEqual(bob_entry["port"], self.bob.node.port)

        charlie_entry = self.alice.find_peer("Charlie")
        self.assertEqual(charlie_entry["peer_id"], self.C)
        self.assertEqual(charlie_entry["port"], self.charlie.node.port)

        # peer_id is the expected form: 8 hex characters.
        for pid in (self.A, self.B, self.C):
            self.assertEqual(len(pid), 8)
            int(pid, 16)  # raises if not hex

    # ----------------------------------------------------------------------
    # 2. Text messaging
    # ----------------------------------------------------------------------
    def test_text_messages(self):
        self.assertTrue(self.bob.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(self.charlie.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(wait_until(lambda: self.A in self.bob.peer_ids()))

        # Bob -> Alice
        self.assertTrue(self.bob.node.send_text(self.A, "Hello Alice!"))
        self.assertTrue(wait_until(
            lambda: self.alice.texts_from("Bob") == ["Hello Alice!"]))

        # Alice -> Bob
        self.assertTrue(self.alice.node.send_text(self.B, "Hi Bob, how are you?"))
        self.assertTrue(wait_until(
            lambda: self.bob.texts_from("Alice") == ["Hi Bob, how are you?"]))

        # Charlie -> Alice
        self.assertTrue(self.charlie.node.send_text(self.A, "Hi Alice from Charlie"))
        self.assertTrue(wait_until(
            lambda: self.alice.texts_from("Charlie") == ["Hi Alice from Charlie"]))

        # The log lines look like the figures: "Bob -> You: Hello Alice!"
        self.assertTrue(wait_until(
            lambda: self.alice.has_log("Bob -> You: Hello Alice!")))

    # ----------------------------------------------------------------------
    # 3. File transfer: text, big binary, empty, Unicode name
    # ----------------------------------------------------------------------
    def make_source_files(self):
        folder = tempfile.mkdtemp(prefix="p2p_src_")

        small = os.path.join(folder, "small.txt")
        with open(small, "wb") as fh:
            fh.write(b"Hello P2P file!\n" * 2000)          # ~27 KB

        big = os.path.join(folder, "big.bin")
        with open(big, "wb") as fh:
            # ~5 MB, and deliberately NOT a multiple of 64 KiB, so the last
            # chunk is short (this is what trips naive recv() loops).
            fh.write(os.urandom(5 * 1024 * 1024 + 31337))

        empty = os.path.join(folder, "empty.bin")
        with open(empty, "wb") as fh:
            fh.write(b"")                                   # 0 bytes

        unicode_name = os.path.join(folder, u"stud\u00e9nt \u5831\u544a.txt")
        with open(unicode_name, "wb") as fh:
            fh.write(os.urandom(4096))

        return {"small": (small, hash_file(small)),
                "big": (big, hash_file(big)),
                "empty": (empty, hash_file(empty)),
                "unicode": (unicode_name, hash_file(unicode_name))}

    def test_file_transfers(self):
        src = self.make_source_files()

        self.assertTrue(self.bob.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(self.charlie.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(self.charlie.node.connect_to("127.0.0.1", self.bob.node.port))
        self.assertTrue(wait_until(lambda: self.bob.peer_ids() == {self.A, self.C}))

        # Different pairs, different sizes.
        self.assertTrue(self.alice.node.send_file(self.B, src["small"][0]))
        self.assertTrue(self.bob.node.send_file(self.C, src["big"][0]))
        self.assertTrue(self.charlie.node.send_file(self.A, src["empty"][0]))
        self.assertTrue(self.alice.node.send_file(self.B, src["unicode"][0]))

        # Each receiver eventually ends up with exactly the right SHA-256s.
        self.assertTrue(wait_until(
            lambda: self.alice.received_hash_set() == {src["empty"][1]},
            timeout=15))
        self.assertTrue(wait_until(
            lambda: self.bob.received_hash_set() == {src["small"][1], src["unicode"][1]},
            timeout=15))
        self.assertTrue(wait_until(
            lambda: self.charlie.received_hash_set() == {src["big"][1]},
            timeout=15))

        # The Unicode filename survived unscathed in downloads/.
        self.assertTrue(wait_until(lambda: self.bob.has_log(
            "File received: " + u"stud\u00e9nt \u5831\u544a.txt")))

    # ----------------------------------------------------------------------
    # 4. Framing + send lock: text immediately after a big file
    # ----------------------------------------------------------------------
    def test_text_right_after_big_file_on_same_connection(self):
        src = self.make_source_files()
        self.assertTrue(self.bob.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(wait_until(lambda: self.A in self.bob.peer_ids()))

        self.assertTrue(self.bob.node.send_text(self.A, "before-the-file"))
        self.assertTrue(self.bob.node.send_file(self.A, src["big"][0]))
        self.assertTrue(self.bob.node.send_text(self.A, "after-the-file"))

        # The receiver saw them IN ORDER: text, file, text -- proving the
        # per-connection send lock kept the file bytes contiguous and the
        # framing kept message boundaries intact.
        self.assertTrue(wait_until(
            lambda: self.alice.texts_from("Bob")
            == ["before-the-file", "after-the-file"], timeout=15))
        self.assertTrue(wait_until(
            lambda: self.alice.received_hash_set() == {src["big"][1]},
            timeout=15))

    # ----------------------------------------------------------------------
    # 5. Abrupt disconnect: the others survive and keep talking
    # ----------------------------------------------------------------------
    def test_abrupt_disconnect_survival(self):
        self.assertTrue(self.bob.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(self.charlie.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(self.charlie.node.connect_to("127.0.0.1", self.bob.node.port))
        self.assertTrue(wait_until(
            lambda: self.alice.peer_ids() == {self.B, self.C}
            and self.bob.peer_ids() == {self.A, self.C}))

        # "Charlie's computer dies": the sockets just close.
        self.charlie.stop()

        # Both survivors notice and drop Charlie from their peer lists.
        self.assertTrue(wait_until(lambda: self.alice.peer_ids() == {self.B}))
        self.assertTrue(wait_until(lambda: self.bob.peer_ids() == {self.A}))

        # Alice and Bob can still talk to each other.
        self.assertTrue(self.alice.node.send_text(self.B, "still here?"))
        self.assertTrue(wait_until(
            lambda: self.bob.texts_from("Alice") == ["still here?"]))

    # ----------------------------------------------------------------------
    # 6. Error handling -- clean errors, never exceptions
    # ----------------------------------------------------------------------
    def test_connection_errors(self):
        # (a) Free port, but nothing listening there.
        dead_port = free_port()
        self.assertFalse(self.alice.node.connect_to("127.0.0.1", dead_port))
        self.assertTrue(self.alice.has_log("Connection failed"))

        # (b) Invalid IP address.
        self.assertFalse(self.alice.node.connect_to("999.1.2.3", 5000))
        self.assertTrue(self.alice.has_log("Invalid IP"))
        self.assertFalse(self.alice.node.connect_to("not-a-host-<x>", 5000))
        self.assertTrue(self.alice.has_log("Invalid IP"))

        # (c) Connecting to yourself (loopback and, if we have one, LAN IP).
        self.assertFalse(self.alice.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(self.alice.has_log("yourself"))
        lan = get_lan_ip()
        if lan != "127.0.0.1":
            self.assertFalse(self.alice.node.connect_to(lan, self.alice.node.port))
            self.assertTrue(self.alice.has_log("yourself"))

        # (d) Duplicate connection to an already-connected peer.  The
        #     duplicate is refused, and the message is logged by the side
        #     that detects it (Alice, who sees Bob's second hello).
        self.assertTrue(self.bob.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(wait_until(lambda: self.B in self.alice.peer_ids()))
        self.assertFalse(self.bob.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(wait_until(lambda: self.alice.has_log("Already connected")))

        # (e) Sending before starting the peer.
        idle = Harness("Idle")
        self.assertFalse(idle.node.send_text("abc", "hi"))
        self.assertTrue(idle.has_log("Start the peer first"))
        self.assertFalse(idle.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(idle.has_log("Start the peer first"))
        idle.stop()

        # (f) File that does not exist.
        self.assertFalse(self.alice.node.send_file(self.B, r"C:\definitely\missing\no.bin"))
        self.assertTrue(self.alice.has_log("File does not exist"))

        # (g) No peer selected / peer gone.
        self.assertFalse(self.alice.node.send_text("00000000", "hi"))
        self.assertTrue(self.alice.has_log("no longer connected"))

    # ----------------------------------------------------------------------
    # 7. Malicious / malformed remote data -- still no crash
    # ----------------------------------------------------------------------
    def test_malicious_filename_is_sanitised(self):
        raw = socket.create_connection(("127.0.0.1", self.alice.node.port))
        send_message(raw, make_hello("deadbeef", "Mallory", 9999))
        recv_message(raw)                       # Alice's hello_ack
        send_message(raw, {"type": "file", "filename": "../../../evil.txt",
                           "filesize": 5})
        raw.sendall(b"HELLO")
        raw.close()                             # then the peer "vanishes"

        # The file must land in downloads/ as plain "evil.txt", never
        # outside the folder (path-traversal blocked by basename()).
        def evil_arrived():
            candidate = os.path.join(self.alice.downloads, "evil.txt")
            return (os.path.isfile(candidate)
                    and hash_file(candidate) == hashlib.sha256(b"HELLO").hexdigest())
        self.assertTrue(wait_until(evil_arrived))
        # Only the flat file appears -- no directories, no .part leftovers.
        py_files = [e for e in os.listdir(self.alice.downloads)
                    if not e.startswith(".")]
        self.assertEqual(py_files, ["evil.txt"])

        # Node is still fully functional afterwards.
        self.assertTrue(self.alice.node.is_running)

    def test_garbage_data_is_rejected(self):
        raw = socket.create_connection(("127.0.0.1", self.alice.node.port))
        send_message(raw, make_hello("b00b1e", "Troll", 9999))
        recv_message(raw)                       # hello_ack
        # Length prefix of 0xFFFFFFFF (> 1 MB cap) -> must be refused.
        raw.sendall(b"\xff\xff\xff\xff")
        raw.close()

        self.assertTrue(wait_until(lambda: self.alice.has_log("oversize message")))
        self.assertTrue(self.alice.node.is_running)

        # And real communication still works afterwards.
        self.assertTrue(self.bob.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(wait_until(lambda: self.B in self.alice.peer_ids()))
        self.assertTrue(self.bob.node.send_text(self.A, "still alive"))
        self.assertTrue(wait_until(
            lambda: self.alice.texts_from("Bob") == ["still alive"]))

    # ----------------------------------------------------------------------
    # 8. stop() then start() again
    # ----------------------------------------------------------------------
    def test_restart_works(self):
        self.assertTrue(self.bob.node.connect_to("127.0.0.1", self.alice.node.port))
        self.assertTrue(wait_until(lambda: self.B in self.alice.peer_ids()))

        self.alice.stop()
        self.assertFalse(self.alice.node.is_running)
        self.assertFalse(self.alice.node.peer_list())

        # Restarting on a fresh port works, and a new peer can connect.
        self.assertTrue(self.alice.node.start("Alice", 0))
        fresh = Harness("Dave")
        try:
            self.assertTrue(fresh.node.start("Dave", 0))
            self.assertTrue(fresh.node.connect_to("127.0.0.1", self.alice.node.port))
            self.assertTrue(wait_until(
                lambda: fresh.node.peer_id in self.alice.peer_ids()))
        finally:
            fresh.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)