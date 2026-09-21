# VIVA NOTES -- CSE 433 P2P Assignment

Answers to the 20 viva questions, tied to the actual code in this project
(`protocol.py`, `p2p_node.py`, `main.py`), plus the "walk me through sending a
file" explanation demanded by assignment section 21.

File map for quick reference:

| Question | Where it lives in the code |
| -------- | -------------------------- |
| TCP server (socket/bind/listen/accept) | `P2PNode.start()` and `_accept_loop()` in `p2p_node.py` |
| TCP client (socket/connect)            | `P2PNode.connect_to()` in `p2p_node.py` |
| HELLO handshake                         | `protocol.make_hello`, `make_hello_ack`; used in `connect_to()` and `_handle_incoming()` |
| Framing / length prefix                 | `send_message`, `recv_message`, `recv_exact` in `protocol.py` |
| Chunked file transfer                   | `_send_file_worker()` and `_receive_file()` in `p2p_node.py` |
| One thread per connection               | `_accept_loop()` spawns `_handle_incoming`; `connect_to()` spawns `_receive_loop` |
| Thread-safe GUI                         | `main.py` `ChatApp.event_queue` + `_drain_events()` |
| Where files are stored                  | `downloads/` (`P2PNode.downloads_dir`) |

---

## 1. What is the difference between a client and a server?

A **server** waits for connections: it calls `socket()`, `bind()`, `listen()`
then `accept()` in a loop (see `P2PNode.start()` and `_accept_loop()` in
`p2p_node.py`). A **client** actively initiates a connection by calling
`socket()` then `connect()` (see `P2PNode.connect_to()`). The server *waits*
and the client *initiates* -- the two roles are defined only by who connects
to whom.

## 2. In this project, why can one peer act as both a client and a server?

Because there is no central server (section 1 of the assignment). Every peer
must be reachable by others (so it runs a listening socket, like a server) and
must also be able to reach others (so it can create outgoing connections, like
a client). In `p2p_node.py`, `P2PNode.start()` opens and runs the server side,
and `connect_to()` provides the client side -- so **every peer = TCP server +
TCP client**, exactly as the assignment says in section 8.

## 3. What is the purpose of an IP address?

An IP address identifies a specific computer (network interface) on the
network, so that a connection can be directed to the right machine. In
`connect_to()` the user types the remote IP (e.g. `192.168.1.10`) and the IP
is part of the address tuple passed to `sock.connect((ip, port))`. On the
server side, `accept()` tells us which IP the remote peer came from.

## 4. What is the purpose of a port number?

A port number identifies a specific *service/app* on a computer, so many
programs can use the network at once. When we bind `("0.0.0.0", 5000)` in
`P2PNode.start()`, the OS knows that TCP packets for port 5000 belong to our
peer. The port of a peer is exchanged in the HELLO message so we can show
`Name [id] ip:port` in the peer list and detect duplicates.

## 5. Why do different peers need different ports when testing multiple peers on the same computer?

Because a `bind()` can only bind one port at a time, and on the same machine
there is only one OS -- two listening sockets cannot both bind port 5000. The
assignment example (section 13.3) uses Alice = 5000, Bob = 5001, Charlie =
5002. That is why the GUI keeps separate **My Port** (e.g. 5000) and **remote
Port** (e.g. 5001) fields.

## 6. What happens when connect() is called?

The OS performs a three-way TCP handshake with the remote IP:port and, on
success, creates an end-to-end connection between the two sockets. After
`connect()` returns we have a reliable byte pipe. In `connect_to()` we call it
inside a 5-second timeout, then validate the remote and perform our
application-level HELLO handshake.

## 7. What happens when accept() is called?

`accept()` blocks until a connection arrives; then it returns a **new**
socket for that connection plus the remote address. In `_accept_loop()` we
accept in a `while` loop and immediately hand each new socket to its own
thread (`_handle_incoming`), which is what lets one peer talk to many peers at
once.

## 8. Why is TCP used instead of UDP?

Because TCP is **reliable and ordered**: bytes arrive exactly once, in order,
or the connection is reported broken. A file is a sequence of bytes that must
not be lost or reordered (assignment section 5.4 says exactly this), so a raw
byte stream over TCP is the right fit. UDP is a datagram service with no
guaranteed delivery or order, which would need extra protocol work we do not
need.

## 9. Why are threads used?

Because one peer must stay responsive while handling several connections:
while blocked reading from Alice, it must still be able to read from Bob. The
assignment's "connection manager" picture (section 5.5) shows one thread per
connection. In code, `_accept_loop()` spawns `_handle_incoming` per inbound
connection and `connect_to()` spawns `_receive_loop` per outbound connection,
so every peer runs as many reader threads as it has connections.

## 10. What is the purpose of the HELLO message?

The HELLO/HELLO_ACK exchange is the **handshake**: before any real data, each
side announces who it is (`peer_name`, and the unique `peer_id` = first 8 hex
chars of a uuid4), and the receiver of a HELLO replies with HELLO_ACK. This is
how the peer list learns `Alice [a83f21c4]`. In `connect_to()` we send
`make_hello(...)` and only register the peer after a valid `hello_ack`; in
`_handle_incoming()` we read the hello, then reply with
`make_hello_ack(...)`.

## 11. What is message framing?

TCP only gives us bytes; it does not remember where one message ends and the
next begins. Framing means we stamp each message with its size:
`[4-byte big-endian length][JSON payload]`. `send_message()` does
`struct.pack("!I", len(payload)) + payload`; `recv_message()` reads 4 bytes,
then exactly the rest. This is the length-prefix framing from assignment
section 10.6.

## 12. Why can't we assume that one recv() call gives us one complete message?

Because TCP is a byte stream: `recv()` may return 1 byte, 100 bytes, or part
of a message, depending on network buffering -- a single `send()` is not
delivered as a single chunk. That is exactly why `recv_exact()` loops until it
has accumulated exactly `n` bytes (comment at the top of `protocol.py`), and
why file receive uses the **file size from the metadata**, never the number of
`recv()` calls, to know when the transfer is complete (assignment section 11).

## 13. Why is file metadata sent before the actual file?

The receiver must know how many bytes to expect, what name to save, and when
to stop reading. So we first send the framed JSON header (`filename`,
`filesize`) and only then the raw bytes. `_receive_file()` reads the header,
then loops `recv_exact(min(65536, remaining))` until `remaining == 0`; the
moment all expected bytes are written to a `.part` file it is renamed into
`downloads/`, so a half-written transfer never looks like a valid file.

## 14. Why are files transferred in chunks?

So that memory usage is flat no matter how big the file is: we read 64 KiB
from disk, `sendall` it, repeat. The sender loop is in `_send_file_worker()`
(`fh.read(CHUNK_SIZE)`), and `CHUNK_SIZE = 64 * 1024` is defined in
`protocol.py`. A 1 GB video therefore only ever occupies a 64 KiB buffer in
RAM on both ends.

## 15. How does the receiver know when the file transfer has finished?

From the **metadata**, not from `recv()`: the receiver counts how many bytes
it has written and stops when it has consumed exactly `filesize` bytes. Only
then is the `.part` file renamed to the final name. This is why sending the
size up front (question 13) is essential; assignment section 11 states it
explicitly.

## 16. What happens if the other peer disconnects?

The receiver thread's `recv()` eventually returns `b""` / raises, and
`_receive_loop()` calls `_drop_peer()`, which removes the peer from the dict,
closes the socket, logs `[SYSTEM] Peer Bob disconnected`, and refreshes the
peer list -- while every other thread keeps running. File sending to that peer
fails with a clean `[ERROR] File send ... failed`. This is tested in
`test_p2p.py::test_abrupt_disconnect_survival`. The app never crashes because
a peer leaves.

## 17. Where is the received file stored?

In the `downloads/` folder next to the project (`P2PNode.downloads_dir`,
created automatically). The name is sanitised with `os.path.basename` (a
hostile `../../x` becomes `x`) and collisions are renamed to `name (1).ext`,
`name (2).ext`, ... -- it is never silently overwritten.

## 18. Is there a central server in this system?

No. Every peer is both server and client (question 2); two peers talk directly
through one TCP connection. If you look at the accept/connect code there is no
central coordinator anywhere. That is the whole point of the assignment's
"Peer A <-> Peer B" diagram in section 1.

## 19. What would happen if the central server in a traditional client-server system went offline?

The clients could no longer talk to each other: all communication went through
that one server, so it is a single point of failure. In our P2P design there
is no such node -- if one peer disappears, only that peer's links break
(question 16); all other peers keep talking, which the tests demonstrate.

## 20. What additional challenges would need to be solved to build a large-scale Internet P2P system?

Peer **discovery** (how peers find each other -- DHTs like Kademlia),
**NAT traversal** (most home computers are behind routers, so incoming
connections must be relayed or punched through), **reliability/resume** of
broken transfers, **encryption & authentication**, and **decentralised
routing/consensus** for very large networks. The assignment explicitly marks
all of these as *not required* (section 15) -- here peers meet by typing each
other's IP:port directly.

---

## Walkthrough: "Sending photo.jpg from Alice to Bob" (assignment section 21)

Follow the chain: **IP + Port → TCP socket → HELLO handshake → application
protocol → text/file data**.

1. **IP + Port.** Bob's server is listening on `0.0.0.0:5000`
   (`P2PNode.start()`: `socket, bind(("0.0.0.0", port)), listen(5)`). The GUI
   on Alice's side has Bob's IP (`127.0.0.1` on one PC) and port (`5000`).
2. **TCP socket.** Alice presses **Connect**; `P2PNode.connect_to()` creates a
   client socket and calls `sock.connect((ip, port))` (5-second timeout). On
   Bob, `_accept_loop()` is blocked in `accept()`; the TCP three-way handshake
   completes and `accept()` returns the new connection socket, which is given
   to its own handler thread.
3. **HELLO handshake.** Alice's `connect_to()` sends the framed JSON
   `hello` (`peer_id`, `peer_name`="Alice", `port`). Bob's handler reads it,
   registers Alice in the protected `self._peers` dict, replies with
   `hello_ack` and starts its receiver loop; Alice validates the ack, registers
   Bob, and starts her receiver loop. Now both sides know who they talk to.
4. **Application protocol.** Every later message is a framed JSON message:
   `[4-byte length][JSON]`. Alice selects Bob in the peer list and picks
   `photo.jpg`. `P2PNode.send_file()` checks the file exists, then a background
   thread (`_send_file_worker`) acquires the per-connection send lock and
   sends stage 1: `{"type":"file","filename":"photo.jpg","filesize":2456789}`.
5. **File data.** Under the same lock it then streams stage 2: read 64 KiB,
   `sendall`, repeat. Bob's receiver loop gets the `file` header, opens
   `downloads/photo.jpg.part`, reads exactly `filesize` bytes in ≤64 KiB chunks
   using `recv_exact()`, then atomically renames the `.part` file to
   `downloads/photo.jpg`. Bob's log shows `Alice -> You: File received:
   photo.jpg`. Text messages use the same framing but no second stage; the send
   lock guarantees a text message never slides between the header and the file
   bytes.

The whole project is built incrementally exactly as the assignment demands:
TCP server → TCP client → HELLO → threads → framing → text → file chunks →
multiple peers → GUI (verified headlessly in `test_p2p.py`).