"""protocol.py -- the application-level protocol shared by every peer.

TCP is a byte stream: one socket.send() call does NOT arrive as one
socket.recv() chunk, and a message boundary is not preserved by TCP.
So we cannot do "one recv() == one message".  Instead every JSON message
is *framed* with its size:

    [4-byte big-endian unsigned length][ UTF-8 JSON payload ]

The sender prepends len(json_bytes) as a 4-byte big-endian integer
(struct.pack("!I", ...)); the receiver first reads exactly 4 bytes to
learn the length, then reads exactly that many bytes.  This turns the
raw byte stream into clean message boundaries (assignment section 10.6).

The message types used by the application are:

    {"type": "hello",     "peer_id": "...", "peer_name": "...", "port": 5000}
    {"type": "hello_ack", "peer_id": "...", "peer_name": "...", "port": 5001}
    {"type": "text",      "sender_id": "...", "sender_name": "...", "message": "..."}
    {"type": "file",      "sender_id": "...", "sender_name": "...",
                          "filename": "photo.jpg", "filesize": 2456789}

This module has no idea about the GUI or the node logic; it only knows
how to put bytes on / take bytes off a socket.
"""

import json
import struct

# A 4-byte big-endian unsigned integer: the length prefix of every frame.
_HEADER = struct.Struct("!I")

# A JSON control message never legitimately exceeds ~1 MB.  Rejecting
# absurd lengths protects us against garbage / malicious data and against
# accidental memory abuse (a malicious length would make us wait forever).
MAX_MESSAGE_SIZE = 1 * 1024 * 1024

# Files are streamed in 64 KiB chunks so that a huge file is never loaded
# into memory all at once (assignment section 10.9).
CHUNK_SIZE = 64 * 1024


def recv_exact(sock, n):
    """Read exactly n bytes from sock, looping until we have them all.

    Why loop?  One call to sock.recv() may return anywhere from 1 byte up
    to the requested count, and it does NOT respect message boundaries.
    Only when n bytes have accumulated do we have a complete unit (the
    4-byte length, or the n-byte JSON body / file chunk).  If the peer
    closes the connection before those n bytes arrive, recv() returns
    b"" -- we treat that as ConnectionError so the caller can clean up
    instead of waiting forever.
    """
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:                      # peer closed the socket
            raise ConnectionError("peer closed the connection")
        data.extend(chunk)
    return bytes(data)


def send_message(sock, message):
    """Frame ``message`` (a JSON-serialisable dict) and send it.

    One sendall() call transmits the whole frame, and every write to a
    given socket is serialised by the node's per-connection send lock, so
    two messages can never interleave halfway.
    """
    payload = json.dumps(message).encode("utf-8")
    frame = _HEADER.pack(len(payload)) + payload
    sock.sendall(frame)


def recv_message(sock):
    """Read one framed JSON message and return it as a dict.

    Raises ConnectionError if the peer dies mid-frame, and ValueError if
    the data is malformed or absurdly large.
    """
    header = recv_exact(sock, _HEADER.size)
    (length,) = _HEADER.unpack(header)

    if length > MAX_MESSAGE_SIZE:
        raise ValueError("refusing oversize message (%d bytes)" % length)

    body = recv_exact(sock, length)
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("garbage payload: %s" % exc)
    if not isinstance(message, dict):
        raise ValueError("expected a JSON object, got %s" % type(message).__name__)
    return message


# ---------------------------------------------------------------------------
# Helper builders for the four message types.  Keeping them in one place
# means the wire format is defined exactly once (section 10.5).
# ---------------------------------------------------------------------------

def make_hello(peer_id, peer_name, port):
    """The handshake intro: sent by whichever peer initiates a TCP connection."""
    return {"type": "hello", "peer_id": peer_id, "peer_name": peer_name, "port": port}


def make_hello_ack(peer_id, peer_name, port):
    """The handshake reply: sent in answer to a received hello."""
    return {"type": "hello_ack", "peer_id": peer_id, "peer_name": peer_name, "port": port}


def make_text(sender_id, sender_name, message):
    """A chat message.  Rich text / formatting is out of scope."""
    return {"type": "text", "sender_id": sender_id,
            "sender_name": sender_name, "message": message}


def make_file(sender_id, sender_name, filename, filesize):
    """File metadata.  The raw file bytes follow immediately on the socket."""
    return {"type": "file", "sender_id": sender_id, "sender_name": sender_name,
            "filename": filename, "filesize": filesize}