# P2P Network Chat & File Sharing

A lightweight **peer-to-peer (P2P) chat and file-sharing application** written
in Python. Every running instance is both a **TCP server** (accepts incoming
connections) and a **TCP client** (connects to other peers), so peers talk to
each other **directly** -- there is **no central server**.

Each peer connects to another peer by **IP address + port**, exchanges a
**HELLO handshake**, and can then send **text messages** and **arbitrary
binary files** (text files, images, audio, video, PDF, ZIP, ...). Files are
treated as plain bytes; there is no per-file-type code.

Built for the CSE 433 *Blockchain & Distributed Security Lab* assignment, using
**only the Python standard library** -- no third-party packages.

---

## Requirements

- **Python 3.9 or later**
- OS: Windows, Linux, or macOS
- No third-party packages (see `requirements.txt`)

## Setup / Installation

1. Install Python 3.9+ from https://www.python.org/downloads/
   (on Windows tick **"Add Python to PATH"** when installing).
2. Open a terminal in this folder (`P2P_Network/`) and confirm:
   ```
   python --version
   ```
3. That's it. Nothing else to install.

## How to Run

```
python main.py
```

The GUI opens. You can now start your own peer, connect to others, and chat.

> Files that **you receive** from other peers are saved into the
> `downloads/` folder next to the project (auto-created if missing).

---

## How to Connect Two Peers on the Same PC

Run the app **twice** on the same computer:

| Window | Name   | My Port | Connect to    |
| ------ | ------ | ------- | ------------- |
| 1      | Alice  | `5000`  | -             |
| 2      | Bob    | `5001`  | `127.0.0.1` : `5000` |

Steps in window 2:

1. Set **My Peer** name = `Bob`, port = `5001`, click **Start Peer**.
2. Leave **IP** = `127.0.0.1` (loopback = this same machine),
   set **Port** = `5000`, click **Connect**.
3. `[SYSTEM] Connected to Alice` appears and both windows show each other in
   **Connected Peers**.

Why different ports? Both peers run a TCP server on the same computer, and
two services cannot bind the same port number at the same time.

## How to Connect Two Peers over LAN (two computers)

1. Find each computer's **LAN IP** (the app shows it at the bottom:
   `This PC's LAN IP: 192.168.x.x`). You can also run `ipconfig` (Windows)
   or `ip addr` (Linux/macOS) and look at the Wi-Fi/Ethernet adapter.
2. Start Peer A on computer A (e.g. Alice, port `5000`).
3. On computer B (Bob), start the peer, then connect to
   `IP of A` : `5000`, e.g. `192.168.1.10` : `5000`.
4. Both PCs must be on the **same Wi-Fi / LAN**.

**Windows firewall (important):** Windows Defender Firewall usually blocks
Python's incoming connections. When you **Start Peer** the first time, allow
the "Python" / "Allow access" prompt. If you closed it, or connections are
refused, check:

- Control Panel → Windows Defender Firewall → *Allow an app through the
  firewall* → **Python** must be allowed on **Private** networks.
- If already listed, re-add it or toggle it once to reset the rule.

## How to Send Text Messages

1. Select the peer in the **Connected Peers** list (highlight it).
2. Type in the **Send Text** box and press **Enter**, or click **Send**.
3. The log shows `You -> Ray: Hi There` on your side and
   `Ray -> You: Hi There` on theirs.

If nothing is selected you get `[ERROR] No peer selected` and nothing is sent.

## How to Send Files

1. Select the peer in the **Connected Peers** list.
2. Click **Choose File & Send**, pick any file (image, audio, video, PDF, ZIP,
   ...). Large files are streamed in 64 KiB chunks in a background thread, so
   the GUI stays responsive.
3. The receiver stores the file in their **`downloads/`** folder under the
   original name. If a file with that name already exists there, it is saved as
   `name (1).ext`, `name (2).ext`, ... (never overwritten silently).

---

## Protocol (brief)

TCP is a **byte stream** and does not preserve message boundaries, so every
JSON message is **framed**:

```
+------------------------+----------------------------+
| 4-byte big-endian size |  JSON payload (UTF-8)      |
+------------------------+----------------------------+
```

The sender prepends `len(json_bytes)` as a 4-byte big-endian integer; the
receiver reads 4 bytes, then **exactly** that many bytes.

Message types:

| type       | purpose                                    | main fields                     |
| ---------- | ------------------------------------------ | ------------------------------- |
| `hello`    | introduced when a TCP connection opens     | `peer_id`, `peer_name`, `port`  |
| `hello_ack`| reply to `hello` (handshake)               | `peer_id`, `peer_name`, `port`  |
| `text`     | a chat message                             | `sender_id`, `sender_name`, `message` |
| `file`     | file metadata; **raw bytes follow**        | `sender_id`, `sender_name`, `filename`, `filesize` |

File transfer is two stages on the same TCP connection:

```
Sender                          Receiver
|  [frame] {"type":"file",     |
|   "filename":"photo.jpg",    |
|   "filesize":2456789}        |
|------------------------------>|
|  [2456789 raw bytes ... ]    |
|------------------------------>|   read exactly filesize bytes,
                                |   stream them to downloads/photo.jpg
```

Files are streamed in **64 KiB chunks** on both sides so huge files never
load fully into memory. The **per-connection send lock** ensures a text
message can never interleave with the middle of a file being sent.

---

## Project Structure

```
P2P_Network/
├── main.py          # Tkinter GUI + user interaction only
├── p2p_node.py      # networking: server, client, threads, text, file transfer
├── protocol.py      # message encode/decode, framing, recv_exact helpers
├── requirements.txt # no third-party packages needed
├── test_p2p.py      # headless automated tests (python test_p2p.py)
├── README.md
├── VIVA_NOTES.md    # answers to the viva questions
└── downloads/       # received files go here (auto-created)
```

| File           | Responsibility (from the assignment)                     |
| -------------- | -------------------------------------------------------- |
| `main.py`      | User interface and interaction with the user.            |
| `p2p_node.py`  | Peer networking, connections, text & file transfer.      |
| `protocol.py`  | Encoding/decoding application-level messages + framing.  |
| `downloads/`   | Stores files received from other peers.                  |
| `README.md`    | Setup, usage, and explanation.                           |

## Running the Automated Tests (headless)

No display and no GUI needed:

```
python test_p2p.py
```

It starts 3 peers on free local ports and verifies the handshake, text,
file integrity (SHA-256) for small / big / empty / Unicode-named files,
message framing after large transfers, abrupt disconnects, and the error
cases.

---

## Troubleshooting

| Problem                          | Cause & fix                                                        |
| -------------------------------- | ------------------------------------------------------------------ |
| `[ERROR] Connection failed: Connection refused` | The remote peer is not running, the IP/port is wrong, or a firewall is blocking you. Check the peer started on the other PC and that the port matches. |
| `[ERROR] Cannot listen on port ...` / starts immediately failing | The port is already in use by another peer or program. Use a different port (e.g. 5002). |
| Connect times out on LAN         | Windows firewall blocks Python: allow Python on **Private** networks (see above). |
| I receive files as `name (1).ext` | A file with that name already exists in `downloads/`; the app never overwrites silently. |
| `[ERROR] No peer selected`       | Click a peer in the Connected Peers list first.                   |
| Sending before starting          | Click **Start Peer** first (the app also warns you).              |

## Screenshots

> Add your screenshots here (they will be your 3 demo figures).

| # | What it shows                        | Image |
|---|--------------------------------------|-------|
| 1 | Peer 1 (Alice) started on port 5000  | `screenshots/peer1.png` |
| 2 | Peer 2 (Bob) connected, chat + file  | `screenshots/peer2.png` |
| 3 | Peer 3 (Charlie) in the network      | `screenshots/peer3.png` |

(Drop the three PNGs into a `screenshots/` folder inside the project and link
them above.)
