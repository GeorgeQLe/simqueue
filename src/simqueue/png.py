"""Bounded PNG integrity checks for native framebuffer exports; no imaging dependency."""

import struct
import zlib


def validate_png(path):
    if not 0 < path.stat().st_size <= 64 * 1024 * 1024:
        raise ValueError("PNG size outside bounds")
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG signature missing")
    offset, dimensions, compressed, palette = 8, None, [], False
    ended = False
    while offset < len(data):
        if len(data) - offset < 12:
            raise ValueError("Truncated PNG chunk")
        length, kind = struct.unpack_from(">I4s", data, offset)
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("Truncated PNG payload")
        payload = data[offset + 8 : end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF != crc:
            raise ValueError("PNG checksum mismatch")
        if dimensions is None and kind != b"IHDR":
            raise ValueError("PNG header must be first")
        if kind == b"IHDR":
            if dimensions is not None or length != 13:
                raise ValueError("Invalid PNG header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if (
                not width
                or not height
                or width * height > 16 * 1024 * 1024
                or depth not in depths.get(color, ())
                or compression
                or filtering
                or interlace not in (0, 1)
            ):
                raise ValueError("Unsupported PNG dimensions or format")
            dimensions = (width, height)
        elif kind == b"PLTE":
            if not length or length > 768 or length % 3:
                raise ValueError("Invalid PNG palette")
            palette = True
        elif kind == b"IDAT":
            compressed.append(payload)
        elif kind == b"IEND":
            if length or end != len(data):
                raise ValueError("Invalid PNG ending")
            ended = True
            break
        elif not kind[0] & 32:
            raise ValueError("Unknown critical PNG chunk")
        offset = end
    if not ended or not compressed or (color == 3 and not palette):
        raise ValueError("Incomplete PNG")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    passes = (
        [(0, 0, 1, 1)]
        if not interlace
        else [
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        ]
    )

    def row_sizes():
        for x, y, dx, dy in passes:
            w = max(0, (width - x + dx - 1) // dx)
            h = max(0, (height - y + dy - 1) // dy)
            if w:
                for _ in range(h):
                    yield (w * channels * depth + 7) // 8

    rows, remaining = iter(row_sizes()), 0

    def consume(block):
        nonlocal remaining
        at = 0
        while at < len(block):
            if remaining == 0:
                try:
                    remaining = next(rows)
                except StopIteration as exc:
                    raise ValueError("Excess PNG pixel data") from exc
                if block[at] > 4:
                    raise ValueError("Invalid PNG row filter")
                at += 1
            count = min(remaining, len(block) - at)
            remaining -= count
            at += count

    decoder = zlib.decompressobj()
    try:
        for payload in compressed:
            while payload:
                consume(decoder.decompress(payload, 65536))
                if decoder.unused_data:
                    raise ValueError("Trailing compressed PNG data")
                payload = decoder.unconsumed_tail
    except zlib.error as exc:
        raise ValueError("Invalid PNG compression") from exc
    if not decoder.eof or remaining or next(rows, None) is not None:
        raise ValueError("Truncated PNG pixels")
    return dimensions
