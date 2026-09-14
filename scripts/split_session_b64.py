"""Split a base64 session blob into SESSION_B64_* chunks for Railway.

Railway rejects very large single variable values, which is why the session is
stored as SESSION_B64_1..N plus SESSION_B64_CHUNKS. This regenerates that whole
block from a .b64 file so it can be pasted into the Raw Editor in one go.

Usage:
    python scripts/split_session_b64.py data/clean_forwarder.b64 [chunks]

Writes <input>.vars.txt next to the input and prints the block to stdout.
"""

import sys
from pathlib import Path


def build_chunks(blob: str, chunk_count: int):
    blob = "".join(blob.split())

    if not blob:
        raise SystemExit("Input file is empty.")

    if chunk_count < 1:
        raise SystemExit("Chunk count must be at least 1.")

    size = -(-len(blob) // chunk_count)  # ceiling division

    chunks = [blob[i:i + size] for i in range(0, len(blob), size)]

    # Guard the round trip: the chunks must rebuild the original exactly,
    # and none may be empty, or the worker raises on a missing chunk.
    if "".join(chunks) != blob:
        raise SystemExit("Internal error: chunks do not rejoin to the original blob.")

    if any(not c for c in chunks):
        raise SystemExit("Internal error: produced an empty chunk.")

    return chunks


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python scripts/split_session_b64.py <file.b64> [chunks]")

    path = Path(sys.argv[1])
    chunk_count = int(sys.argv[2]) if len(sys.argv) >= 3 else 11

    blob = path.read_text(encoding="ascii")
    chunks = build_chunks(blob, chunk_count)

    lines = [f"SESSION_B64_CHUNKS={len(chunks)}"]
    lines += [f"SESSION_B64_{i}={c}" for i, c in enumerate(chunks, start=1)]

    block = "\n".join(lines)

    out = path.with_suffix(".vars.txt")
    out.write_text(block, encoding="ascii")

    print(block)
    print("", file=sys.stderr)
    print(f"blob_length={len(''.join(blob.split()))}", file=sys.stderr)
    print(f"chunks={len(chunks)} max_chunk_length={max(len(c) for c in chunks)}", file=sys.stderr)
    print(f"written={out}", file=sys.stderr)


if __name__ == "__main__":
    main()
