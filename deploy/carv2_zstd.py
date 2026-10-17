#!/usr/bin/env python3
"""Wrap bounded CARv1 exports as indexed CARv2, then compress for bulk transfer.

The Zstandard stream is an outer transport encoding. CARv2 and its original
CID-addressed blocks are restored before Kubo sees them. The writer supports
the sha2-256 inventory blocks used by Yataverse and refuses malformed frames,
unsupported multihashes, or an index that does not describe the CAR payload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict
from pathlib import Path


PRAGMA = bytes((0x0A, 0xA1, 0x67, 0x76, 0x65, 0x72, 0x73, 0x69, 0x6F, 0x6E, 0x02))
CARV2_DATA_OFFSET = 51
CARV2_HEADER_BYTES = 40
MULTIHASH_INDEX_SORTED = 0x0401
SHA2_256 = 0x12
MAX_HEADER_BYTES = 16 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024


class ArchiveError(Exception):
    pass


def uvarint(value: int) -> bytes:
    if value < 0:
        raise ArchiveError("negative varint")
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def read_uvarint(source, *, capture=None, limit=(1 << 64) - 1) -> tuple[int, int]:
    value = shift = length = 0
    while length < 10:
        raw = source.read(1)
        if not raw:
            raise ArchiveError("truncated varint")
        if capture is not None:
            capture(raw)
        byte = raw[0]
        value |= (byte & 0x7F) << shift
        length += 1
        if byte < 0x80:
            if value > limit:
                raise ArchiveError("varint exceeds supported limit")
            return value, length
        shift += 7
    raise ArchiveError("invalid varint")


def read_exact(source, size: int) -> bytes:
    if size < 0:
        raise ArchiveError("negative read size")
    chunks = bytearray()
    while len(chunks) < size:
        part = source.read(min(COPY_CHUNK_BYTES, size - len(chunks)))
        if not part:
            raise ArchiveError("truncated CAR data")
        chunks.extend(part)
    return bytes(chunks)


def copy_exact(source, destination, size: int, digest=None) -> None:
    remaining = size
    while remaining:
        part = source.read(min(COPY_CHUNK_BYTES, remaining))
        if not part:
            raise ArchiveError("truncated CAR block")
        destination.write(part)
        if digest is not None:
            digest.update(part)
        remaining -= len(part)


def copy_carv1_payload(source, destination, total_size: int, max_blocks: int,
                       *, start_offset: int = 0, require_eof: bool = True):
    """Copy one CARv1 file into destination and return its checked index rows."""
    source.seek(start_offset)
    written = 0

    def write(data: bytes) -> None:
        nonlocal written
        destination.write(data)
        written += len(data)

    header_size, _ = read_uvarint(source, capture=write, limit=MAX_HEADER_BYTES)
    if header_size < 1 or header_size > MAX_HEADER_BYTES:
        raise ArchiveError("invalid CARv1 header size")
    header = read_exact(source, header_size)
    write(header)

    rows = []
    while written < total_size:
        frame_offset = written
        frame_size, frame_prefix_size = read_uvarint(source, capture=write, limit=total_size)
        if frame_size < 1 or written + frame_size > total_size:
            raise ArchiveError("CARv1 frame exceeds the declared data section")

        cid_start = written
        first = read_exact(source, 1)
        write(first)
        if first[0] == SHA2_256:
            mh_code = SHA2_256
            digest_size, _ = read_uvarint(source, capture=write, limit=1024)
            cid_size = 1 + len(uvarint(digest_size))
        else:
            version = first[0]
            if version != 1:
                raise ArchiveError("unsupported CID version in CARv1 frame")
            version_value, version_len = _read_varint_after_first(first, source, write)
            if version_value != 1:
                raise ArchiveError("unsupported CID version in CARv1 frame")
            _codec, codec_len = read_uvarint(source, capture=write)
            mh_code, mh_len = read_uvarint(source, capture=write)
            digest_size, digest_len = read_uvarint(source, capture=write, limit=1024)
            cid_size = version_len + codec_len + mh_len + digest_len

        if digest_size != 32 or mh_code != SHA2_256:
            raise ArchiveError("only sha2-256 CID blocks are supported")
        block_digest = read_exact(source, digest_size)
        write(block_digest)
        cid_size += digest_size
        if cid_size > frame_size:
            raise ArchiveError("CID is longer than its CARv1 frame")
        payload_size = frame_size - cid_size
        block_hash = hashlib.sha256()
        copy_exact(source, destination, payload_size, block_hash)
        written += payload_size
        if block_hash.digest() != block_digest:
            raise ArchiveError("CARv1 block bytes do not match the CID digest")

        rows.append((mh_code, block_digest, frame_offset))
        if len(rows) > max_blocks:
            raise ArchiveError("CARv1 block count exceeds the configured limit")

    if written != total_size or (require_eof and source.read(1)):
        raise ArchiveError("CARv1 has trailing or missing bytes")
    if not rows:
        raise ArchiveError("CARv1 contains no block frames")
    return rows


def _read_varint_after_first(first: bytes, source, capture) -> tuple[int, int]:
    value = first[0] & 0x7F
    shift = 7
    length = 1
    if first[0] < 0x80:
        return value, length
    while length < 10:
        raw = source.read(1)
        if not raw:
            raise ArchiveError("truncated CID varint")
        capture(raw)
        byte = raw[0]
        value |= (byte & 0x7F) << shift
        length += 1
        if byte < 0x80:
            return value, length
        shift += 7
    raise ArchiveError("invalid CID varint")


def encode_index(rows) -> bytes:
    by_code = defaultdict(lambda: defaultdict(list))
    for code, digest, offset in rows:
        by_code[code][len(digest) + 8].append((digest, offset))
    out = bytearray(uvarint(MULTIHASH_INDEX_SORTED))
    out.extend(struct.pack("<I", len(by_code)))
    for code in sorted(by_code):
        widths = by_code[code]
        out.extend(struct.pack("<QI", code, len(widths)))
        for width in sorted(widths):
            records = sorted(widths[width], key=lambda pair: (pair[0], pair[1]))
            out.extend(struct.pack("<IQ", width, width * len(records)))
            for digest, offset in records:
                out.extend(digest)
                out.extend(struct.pack("<Q", offset))
    return bytes(out)


def carv2_header(data_size: int, index_offset: int) -> bytes:
    return bytes(16) + struct.pack("<QQQ", CARV2_DATA_OFFSET, data_size, index_offset)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish_no_replace(temp_path: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(temp_path, output)
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError as exc:
        raise ArchiveError("output already exists") from exc
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def write_json_no_replace(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    try:
        with temp.open("x", encoding="utf-8") as destination:
            json.dump(value, destination, sort_keys=True, separators=(",", ":"))
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.link(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError as exc:
        raise ArchiveError("receipt already exists") from exc
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def run_zstd(args, timeout: int) -> None:
    executable = shutil.which("zstd")
    if not executable:
        raise ArchiveError("zstd CLI is required")
    try:
        result = subprocess.run([executable, *args], capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArchiveError("zstd command failed") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace")[:500].strip()
        raise ArchiveError("zstd refused the archive" + (": " + detail if detail else ""))


def pack(args) -> dict:
    source = args.carv1
    output = args.output
    receipt_path = args.receipt or output.with_name(output.name + ".json")
    if not source.is_file():
        raise ArchiveError("CARv1 input is absent")
    carv1_size = source.stat().st_size
    if not 1 <= args.max_blocks <= 1000 or not 1 <= carv1_size <= args.max_bytes:
        raise ArchiveError("CARv1 exceeds configured block or byte bounds")
    if output.exists() or receipt_path.exists():
        if not output.is_file() or not receipt_path.is_file():
            raise ArchiveError("saved output is missing its matching receipt")
        saved = json.loads(receipt_path.read_text())
        if (saved.get("format") != "carv2+zstd" or
                saved.get("carv1_sha256") != sha256_file(source) or
                saved.get("compressed_sha256") != sha256_file(output) or
                saved.get("compressed_bytes") != output.stat().st_size):
            raise ArchiveError("saved compressed archive differs from its receipt")
        return saved
    output.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(output.parent).free
    if free < 2 * carv1_size + 64 * 1024 * 1024:
        raise ArchiveError("disk reserve is too low for CARv2 and Zstandard temporaries")

    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".carv2.partial", dir=output.parent)
    temp_carv2 = Path(temp_name)
    compressed_tmp = temp_carv2.with_suffix(temp_carv2.suffix + ".zst")
    try:
        with os.fdopen(fd, "wb") as destination, source.open("rb") as car_source:
            destination.write(PRAGMA)
            destination.write(bytes(CARV2_HEADER_BYTES))
            rows = copy_carv1_payload(car_source, destination, carv1_size, args.max_blocks)
            data_size = destination.tell() - CARV2_DATA_OFFSET
            index_offset = destination.tell()
            index = encode_index(rows)
            destination.write(index)
            destination.flush()
            os.fsync(destination.fileno())
            destination.seek(len(PRAGMA))
            destination.write(carv2_header(data_size, index_offset))
            destination.flush()
            os.fsync(destination.fileno())

        checked = validate_carv2(temp_carv2, args.max_blocks)
        if checked["blocks"] != len(rows):
            raise ArchiveError("CARv2 validation returned a different block count")
        carv2_size = temp_carv2.stat().st_size
        carv2_sha = sha256_file(temp_carv2)
        run_zstd(["-q", "-9", "-T0", "--check", "-o", str(compressed_tmp), str(temp_carv2)], timeout=args.timeout)
        compressed_size = compressed_tmp.stat().st_size
        compressed_sha = sha256_file(compressed_tmp)
        publish_no_replace(compressed_tmp, output)
        receipt = {
            "schema": 1,
            "format": "carv2+zstd",
            "carv1_sha256": sha256_file(source),
            "carv1_bytes": carv1_size,
            "carv2_sha256": carv2_sha,
            "carv2_bytes": carv2_size,
            "compressed_sha256": compressed_sha,
            "compressed_bytes": compressed_size,
            "blocks": len(rows),
            "index_entries": len(rows),
            "compression": "zstd",
            "compression_level": 9,
            "ratio_uncompressed_over_compressed": round(carv2_size / compressed_size, 6),
            "saved_fraction": round(1 - compressed_size / carv2_size, 6),
        }
        try:
            write_json_no_replace(receipt_path, receipt)
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        return receipt
    finally:
        for path in (temp_carv2, compressed_tmp):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def validate_carv2(path: Path, max_blocks: int) -> dict:
    file_size = path.stat().st_size
    with path.open("rb") as source:
        if source.read(len(PRAGMA)) != PRAGMA:
            raise ArchiveError("decompressed stream is not CARv2")
        raw_header = read_exact(source, CARV2_HEADER_BYTES)
        if any(raw_header[:16]):
            raise ArchiveError("CARv2 requests unsupported characteristics")
        data_offset, data_size, index_offset = struct.unpack("<QQQ", raw_header[16:])
        if data_offset != CARV2_DATA_OFFSET or data_size < 1 or index_offset != data_offset + data_size:
            raise ArchiveError("CARv2 offsets are invalid")
        if index_offset >= file_size:
            raise ArchiveError("CARv2 index is missing")
        rows = copy_carv1_payload(source, _DiscardWriter(), data_size, max_blocks,
                                  start_offset=data_offset, require_eof=False)
        expected_index = encode_index(rows)
        source.seek(index_offset)
        actual_index = source.read(len(expected_index) + 1)
        if actual_index != expected_index:
            raise ArchiveError("CARv2 index does not match its CARv1 data")
    return {"carv2_bytes": file_size, "blocks": len(rows), "index_entries": len(rows)}


class _DiscardWriter:
    def write(self, data: bytes) -> int:
        return len(data)


def zstd_frame_content_size(path: Path) -> int:
    """Return the declared size of the single regular Zstandard frame."""
    with path.open("rb") as source:
        prefix = source.read(18)
    if len(prefix) < 5 or prefix[:4] != bytes((0x28, 0xB5, 0x2F, 0xFD)):
        raise ArchiveError("input is not a regular Zstandard frame")
    descriptor = prefix[4]
    if descriptor & 0x18:
        raise ArchiveError("Zstandard frame has reserved descriptor bits")
    fcs_flag = descriptor >> 6
    single_segment = bool(descriptor & 0x20)
    dict_flag = descriptor & 0x03
    offset = 5 + (0 if single_segment else 1)
    offset += (0, 1, 2, 4)[dict_flag]
    fcs_width = (1 if single_segment else 0) if fcs_flag == 0 else (2, 4, 8)[fcs_flag - 1]
    if fcs_width == 0 or len(prefix) < offset + fcs_width:
        raise ArchiveError("Zstandard frame does not declare its content size")
    size = int.from_bytes(prefix[offset:offset + fcs_width], "little")
    if fcs_width == 2:
        size += 256
    return size


def unpack(args) -> dict:
    source = args.compressed
    output = args.output
    receipt_path = args.receipt or output.with_name(output.name + ".json")
    if not source.is_file():
        raise ArchiveError("compressed input is absent")
    compressed_sha = sha256_file(source)
    if args.expected_compressed_sha256 and compressed_sha != args.expected_compressed_sha256:
        raise ArchiveError("compressed SHA-256 differs from the expected receipt")
    if output.exists() or receipt_path.exists():
        if not output.is_file() or not receipt_path.is_file():
            raise ArchiveError("saved CARv2 is missing its matching receipt")
        saved = json.loads(receipt_path.read_text())
        validation = validate_carv2(output, args.max_blocks)
        if (saved.get("format") != "carv2+zstd" or
                saved.get("compressed_sha256") != compressed_sha or
                saved.get("carv2_sha256") != sha256_file(output) or
                saved.get("blocks") != validation["blocks"] or
                saved.get("carv2_bytes") != validation["carv2_bytes"] or
                (args.expected_carv2_sha256 and
                 saved.get("carv2_sha256") != args.expected_carv2_sha256)):
            raise ArchiveError("saved CARv2 differs from its receipt")
        return saved
    declared_size = zstd_frame_content_size(source)
    max_carv2_bytes = args.max_bytes + (128 * args.max_blocks) + 1024
    if declared_size > max_carv2_bytes:
        raise ArchiveError("Zstandard content size exceeds the configured CAR bound")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".partial", dir=output.parent)
    os.close(fd)
    temp_output = Path(temp_name)
    try:
        temp_output.unlink()
        run_zstd(["-q", "-d", "-o", str(temp_output), str(source)], timeout=args.timeout)
        if temp_output.stat().st_size != declared_size:
            raise ArchiveError("Zstandard output size differs from its frame header")
        validation = validate_carv2(temp_output, args.max_blocks)
        carv2_sha = sha256_file(temp_output)
        if args.expected_carv2_sha256 and carv2_sha != args.expected_carv2_sha256:
            raise ArchiveError("decompressed CARv2 SHA-256 differs from the expected receipt")
        publish_no_replace(temp_output, output)
        receipt = {
            "schema": 1,
            "format": "carv2+zstd",
            "compressed_sha256": compressed_sha,
            "compressed_bytes": source.stat().st_size,
            "carv2_sha256": carv2_sha,
            **validation,
            "status": "complete",
        }
        try:
            write_json_no_replace(receipt_path, receipt)
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        return receipt
    finally:
        try:
            temp_output.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    packing = subparsers.add_parser("pack", help="wrap CARv1 as indexed CARv2 and compress it")
    packing.add_argument("--carv1", required=True, type=Path)
    packing.add_argument("--output", required=True, type=Path)
    packing.add_argument("--receipt", type=Path)
    packing.add_argument("--max-blocks", type=int, default=1000)
    packing.add_argument("--max-bytes", type=int, default=536870912)
    packing.add_argument("--timeout", type=int, default=1800)
    unpacking = subparsers.add_parser("unpack", help="verify and restore an indexed CARv2 from Zstandard")
    unpacking.add_argument("--compressed", required=True, type=Path)
    unpacking.add_argument("--output", required=True, type=Path)
    unpacking.add_argument("--receipt", type=Path)
    unpacking.add_argument("--expected-compressed-sha256")
    unpacking.add_argument("--expected-carv2-sha256")
    unpacking.add_argument("--max-blocks", type=int, default=1000)
    unpacking.add_argument("--max-bytes", type=int, default=536870912)
    unpacking.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if args.max_blocks < 1 or args.timeout < 1 or (args.command == "unpack" and args.max_bytes < 1):
        print("REFUSED: invalid block or time limit", file=sys.stderr)
        return 2
    try:
        receipt = pack(args) if args.command == "pack" else unpack(args)
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        return 0
    except (ArchiveError, OSError, ValueError, struct.error) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
