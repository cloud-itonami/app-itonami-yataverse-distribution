#!/usr/bin/env python3
"""Ship one bounded inventory range to an owned Kubo node, then checkpoint."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CID = re.compile(r"(?:Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{20,200})\Z")
MAX_CAR_BLOCK = 8_000_000


class ShipError(Exception):
    pass


def atomic_json(path, value):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    with temp.open("x") as output:
        json.dump(value, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temp.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def sha_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(argv, *, cwd=None, timeout=900):
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ShipError("command failed: " + argv[0]) from exc
    if result.returncode:
        raise ShipError("command refused: " + argv[0] + ": " +
                        result.stderr[-600:].strip())
    return result.stdout


def last_receipt(output, source):
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise ShipError(source + " returned no receipt")
    try:
        return json.loads(lines[-1])
    except ValueError as exc:
        raise ShipError(source + " returned invalid receipt") from exc


def s3_object_uri(prefix, suffix):
    parsed = urlsplit(prefix)
    if (parsed.scheme != "s3" or not parsed.netloc or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ShipError("S3 destination must be an s3://bucket/prefix URI")
    base = "s3://" + parsed.netloc + parsed.path.rstrip("/")
    return base + "/" + suffix.lstrip("/")


def s3_head(uri, endpoint_url, region):
    parsed = urlsplit(uri)
    key = parsed.path.lstrip("/")
    command = ["aws", "s3api", "head-object", "--bucket", parsed.netloc,
               "--key", key, "--output", "json"]
    if endpoint_url:
        command += ["--endpoint-url", endpoint_url]
    if region:
        command += ["--region", region]
    return json.loads(run(command, timeout=120))


def upload_s3_file(path, uri, *, endpoint_url, region, content_type, metadata):
    command = ["aws", "s3", "cp", str(path), uri, "--only-show-errors",
               "--content-type", content_type, "--metadata",
               ",".join(key + "=" + value for key, value in metadata.items())]
    if endpoint_url:
        command += ["--endpoint-url", endpoint_url]
    if region:
        command += ["--region", region]
    run(command, timeout=3600)
    head = s3_head(uri, endpoint_url, region)
    if (head.get("ContentLength") != path.stat().st_size or
            head.get("Metadata") != metadata):
        raise ShipError("S3-compatible HEAD differs from uploaded object")
    return {"uri": uri, "bytes": path.stat().st_size,
            "metadata": head.get("Metadata", {})}


def pack_transport_archive(carv1, output, receipt_path, exported, row_count,
                           max_bytes, timeout=1800):
    helper = Path(__file__).resolve().with_name("carv2_zstd.py")
    result = last_receipt(run([sys.executable, str(helper), "pack",
                               "--carv1", str(carv1), "--output", str(output),
                               "--receipt", str(receipt_path),
                               "--max-blocks", str(row_count),
                               "--max-bytes", str(max_bytes),
                               "--timeout", str(timeout)], timeout=timeout + 60),
                         "CARv2/Zstandard packer")
    if (result.get("format") != "carv2+zstd" or
            result.get("carv1_sha256") != exported.get("sha256") or
            result.get("blocks") != row_count or
            result.get("compressed_sha256") != sha_file(output) or
            result.get("compressed_bytes") != output.stat().st_size or
            not SHA256.fullmatch(result.get("carv2_sha256", ""))):
        raise ShipError("CARv2/Zstandard receipt differs from the exported CAR")
    return result


def unpack_remote_archive(args, remote_compressed, remote_carv2,
                          remote_unpack_receipt, archive_receipt, row_count):
    remote_tool = str(Path(args.remote_importer).parent / "carv2_zstd.py")
    command = ["python3", remote_tool, "unpack",
               "--compressed", remote_compressed,
               "--output", remote_carv2,
               "--receipt", remote_unpack_receipt,
               "--expected-compressed-sha256", archive_receipt["compressed_sha256"],
               "--expected-carv2-sha256", archive_receipt["carv2_sha256"],
               "--max-blocks", str(row_count),
               "--max-bytes", str(archive_receipt["carv2_bytes"]),
               "--timeout", "1800"]
    output = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                  args.ssh_host, shlex.join(command)], timeout=1900)
    result = last_receipt(output, "remote CARv2/Zstandard unpacker")
    if (result.get("status") != "complete" or
            result.get("compressed_sha256") != archive_receipt["compressed_sha256"] or
            result.get("carv2_sha256") != archive_receipt["carv2_sha256"] or
            result.get("blocks") != row_count):
        raise ShipError("remote CARv2/Zstandard receipt differs")
    return result


def large_archive_receipt_matches(receipt, skipped, inventory_sha):
    return (receipt.get("schema") == 1 and
            receipt.get("row") == skipped["row"] and
            receipt.get("inventory_sha256") == inventory_sha and
            receipt.get("original_cid") == skipped["cid"] and
            receipt.get("original_bytes") == skipped["bytes"] and
            receipt.get("validation") ==
            "fresh-offline-kubo-import-cat-and-original-cid-rehydration" and
            isinstance(receipt.get("car_sha256"), str) and
            SHA256.fullmatch(receipt["car_sha256"]) is not None and
            type(receipt.get("car_bytes")) is int and receipt["car_bytes"] > 0)


def verify_remote_large_archive(args, skipped):
    if not args.remote_large_archive:
        raise ShipError("oversized row has no Jacob recovery archive")
    name = "row-{}-recovery.car".format(skipped["row"])
    car = args.remote_large_archive.rstrip("/") + "/" + name
    receipt = car + ".json"
    code = ("import json,pathlib,sys; car=pathlib.Path(sys.argv[1]); "
            "receipt=pathlib.Path(sys.argv[2]); "
            "sys.exit(2) if not car.is_file() or not receipt.is_file() "
            "else print(json.dumps({'receipt':json.loads(receipt.read_text()),"
            "'car_bytes_on_disk':car.stat().st_size}))")
    output = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                  args.ssh_host, shlex.join(["python3", "-c", code, car, receipt])],
                 timeout=60)
    result = last_receipt(output, "Jacob large recovery archive")
    source = result.get("receipt") if isinstance(result, dict) else None
    if (not isinstance(source, dict) or not large_archive_receipt_matches(
            source, skipped, args.sha256) or
            result.get("car_bytes_on_disk") != source["car_bytes"]):
        raise ShipError("Jacob large recovery receipt differs from skipped row")


def selected_rows(path, expected_sha, expected_count, start, max_blocks, max_bytes,
                  raw_cids=None):
    digest = hashlib.sha256()
    rows = []
    count = 0
    total = 0
    with path.open("rb") as source:
        for line in source:
            digest.update(line)
            if start <= count < start + max_blocks and total < max_bytes:
                try:
                    row = json.loads(line)
                    cid, size = row["cid"], row["bytes"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise ShipError("invalid inventory row") from exc
                if not isinstance(cid, str) or not CID.fullmatch(cid) or type(size) is not int or size < 1:
                    raise ShipError("invalid inventory CID or size")
                if cid in (raw_cids or set()):
                    pass
                elif size > MAX_CAR_BLOCK or total + size + 1024 * (len(rows) + 1) > max_bytes:
                    # A contiguous CAR ends before the first oversized row.
                    pass
                elif len(rows) == count - start:
                    rows.append({"cid": cid, "bytes": size})
                    total += size
            count += 1
    if digest.hexdigest() != expected_sha or count != expected_count:
        raise ShipError("inventory identity differs from dated snapshot")
    if start >= count:
        return [], None
    if rows:
        return rows, None
    with path.open("rb") as source:
        for index, line in enumerate(source):
            if index == start:
                row = json.loads(line)
                if row["cid"] in (raw_cids or set()):
                    return [], {"row": start, "cid": row["cid"],
                                "bytes": row["bytes"], "reason": "verified-raw-block-store"}
                if row["bytes"] > MAX_CAR_BLOCK:
                    return [], {"row": start, "cid": row["cid"],
                                "bytes": row["bytes"], "reason": "oversized-for-car"}
                raise ShipError("single block exceeds CAR byte budget")
    raise ShipError("inventory row disappeared")


def ship_once(args):
    stop = args.stop_row if args.stop_row is not None else args.count
    if (not SHA256.fullmatch(args.sha256) or args.count < 1 or
            not 0 <= args.start_row < stop <= args.count):
        raise ShipError("invalid inventory identity or start")
    if not 1 <= args.max_blocks <= 1000 or not 1 <= args.max_bytes <= 536_870_912:
        raise ShipError("invalid CAR bounds")
    raw_cids = set()
    if args.raw_cids_file:
        record = json.loads(args.raw_cids_file.read_text())
        cids = record.get("cids")
        if (record.get("inventory_sha256") != args.sha256 or
                not isinstance(cids, list) or
                not all(isinstance(cid, str) and CID.fullmatch(cid) for cid in cids) or
                len(cids) != len(set(cids)) or
                not args.remote_raw_block_store):
            raise ShipError("raw CID manifest or Jacob store argument differs")
        raw_cids = set(cids)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / "ship.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ShipError("shipper already running") from exc
        checkpoint = args.state_dir / "checkpoint.json"
        state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {
            "cursor": args.start_row, "inventory_sha256": args.sha256}
        if state.get("inventory_sha256") != args.sha256 or type(state.get("cursor")) is not int:
            raise ShipError("checkpoint identity differs")
        start = state["cursor"]
        if start > stop:
            raise ShipError("checkpoint passed configured stop row")
        if start == stop:
            return {"status": "complete", "cursor": start, "stop_row": stop}
        rows, skipped = selected_rows(args.inventory, args.sha256, args.count,
                                      start, min(args.max_blocks, stop - start),
                                      args.max_bytes,
                                      raw_cids)
        if skipped:
            if skipped["reason"] == "verified-raw-block-store":
                code = ("import sys; from raw_block_store import RawBlockStore; "
                        "data=RawBlockStore(sys.argv[1]).read(sys.argv[2],"
                        "max_bytes=int(sys.argv[3])); "
                        "assert data is not None and len(data)==int(sys.argv[3])")
                verify = ["env", "PYTHONPATH=" + str(Path(args.remote_importer).parent),
                          "python3", "-c", code, args.remote_raw_block_store,
                          skipped["cid"], str(skipped["bytes"])]
                run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     args.ssh_host, shlex.join(verify)], timeout=60)
            else:
                verify_remote_large_archive(args, skipped)
            skip = args.state_dir / ("skipped-" + str(start) + ".json")
            if skip.exists():
                if json.loads(skip.read_text()) != skipped:
                    raise ShipError("oversized skip receipt differs")
            else:
                atomic_json(skip, skipped)
            atomic_json(checkpoint, {"cursor": start + 1,
                                     "inventory_sha256": args.sha256})
            return {"status": "skipped-oversized", **skipped}
        end = start + len(rows)
        name = "row-{}-{}".format(start, end - 1)
        car = args.state_dir / (name + ".car")
        export_receipt = args.state_dir / (name + "-export.json")
        if args.source_ssh_host:
            source_car = args.source_output_dir.rstrip("/") + "/" + name + ".car"
            source_receipt = source_car + ".json"
        if export_receipt.exists():
            exported = json.loads(export_receipt.read_text())
            if (not car.is_file() or exported.get("sha256") != sha_file(car) or
                    exported.get("start-row") != start or exported.get("end-row") != end):
                raise ShipError("saved CAR differs from export receipt")
        else:
            if args.source_ssh_host:
                command = ["python3", args.source_exporter,
                           "--inventory", args.source_inventory,
                           "--sha256", args.sha256, "--count", str(args.count),
                           "--start-row", str(start), "--max-blocks", str(len(rows)),
                           "--max-bytes", str(args.max_bytes),
                           "--ipfs-bin", args.source_ipfs_bin,
                           "--ipfs-path", args.source_ipfs_path,
                           "--output", source_car, "--receipt", source_receipt]
                if args.source_rpc_url:
                    command += ["--rpc-url", args.source_rpc_url]
                ssh_opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
                output = run(["ssh", *ssh_opts, args.source_ssh_host,
                              shlex.join(command)], timeout=1800)
                run(["scp", *ssh_opts, args.source_ssh_host + ":" + source_car,
                     str(car)], timeout=900)
            else:
                classpath = run([str(args.kbb), "-Spath"], cwd=args.source, timeout=120).strip()
                command = [str(args.kbb), "--backend", "sci", "--classpath", classpath,
                           "deploy/export_lake_car.cljk", "--inventory", str(args.inventory),
                           "--inventory-sha256", args.sha256, "--inventory-count", str(args.count),
                           "--start-row", str(start), "--max-blocks", str(len(rows)),
                           "--max-bytes", str(args.max_bytes), "--base-url", args.base_url]
                if args.resolve:
                    command += ["--resolve", args.resolve]
                command += ["--output", str(car)]
                output = run(command, cwd=args.source, timeout=1800)
            exported = last_receipt(output, "CAR exporter")
            if (exported.get("status") != "complete" or
                    exported.get("start-row") != start or exported.get("end-row") != end or
                    exported.get("blocks") != len(rows) or
                    exported.get("sha256") != sha_file(car)):
                raise ShipError("CAR export receipt differs")
            atomic_json(export_receipt, exported)
        archive_path = args.state_dir / (name + ".carv2.zst")
        archive_receipt_path = args.state_dir / (name + "-carv2-zstd.json")
        archive_receipt = None
        if args.carv2_zstd:
            archive_receipt = pack_transport_archive(
                car, archive_path, archive_receipt_path, exported, len(rows),
                args.max_bytes)
            transfer_car = archive_path
            transfer_sha256 = archive_receipt["compressed_sha256"]
            import_sha256 = archive_receipt["carv2_sha256"]
        else:
            transfer_car = car
            transfer_sha256 = exported["sha256"]
            import_sha256 = exported["sha256"]
        remote_ext = ".carv2.zst" if args.carv2_zstd else ".car"
        remote_car = (args.remote_car_dir or args.remote_dir).rstrip("/") + "/" + name + remote_ext
        remote_plain_car = remote_car + ".carv2" if args.carv2_zstd else remote_car
        remote_unpack_receipt = remote_car + ".unpack.json"
        remote_receipt = args.remote_dir.rstrip("/") + "/" + name + "-import.json"
        ssh_opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        s3_object = None
        object_uri = None
        if args.s3_destination:
            suffix = (args.sha256 + "/" + name + "-" +
                      archive_receipt["compressed_sha256"] + ".carv2.zst")
            object_uri = s3_object_uri(args.s3_destination, suffix)
            metadata = {"format": "carv2+zstd",
                        "sha256": archive_receipt["compressed_sha256"],
                        "carv2-sha256": archive_receipt["carv2_sha256"],
                        "inventory-sha256": args.sha256}
            s3_object = upload_s3_file(
                transfer_car, object_uri, endpoint_url=args.s3_endpoint_url,
                region=args.s3_region,
                content_type="application/zstd", metadata=metadata)
            s3_download = ["aws", "s3", "cp", object_uri, remote_car,
                           "--only-show-errors"]
            if args.s3_endpoint_url:
                s3_download += ["--endpoint-url", args.s3_endpoint_url]
            if args.s3_region:
                s3_download += ["--region", args.s3_region]
            run(["ssh", *ssh_opts, args.ssh_host,
                 shlex.join(s3_download)], timeout=3600)
        else:
            run(["scp", *ssh_opts, str(transfer_car), args.ssh_host + ":" + remote_car], timeout=1800)
        if args.carv2_zstd:
            unpack_remote_archive(args, remote_car, remote_plain_car,
                                  remote_unpack_receipt, archive_receipt, len(rows))
        import_command = ["python3", args.remote_importer,
                          "--inventory", args.remote_inventory,
                          "--sha256", args.sha256, "--count", str(args.count),
                          "--start-row", str(start), "--max-blocks", str(len(rows)),
                          "--car", remote_plain_car, "--car-sha256", import_sha256,
                          "--ipfs-bin", args.remote_ipfs_bin,
                          "--ipfs-path", args.remote_ipfs_path,
                          "--receipt", remote_receipt]
        # OpenSSH joins remote arguments into a shell command; quote each one.
        imported = last_receipt(run(["ssh", *ssh_opts, args.ssh_host,
                                     shlex.join(import_command)], timeout=1200),
                                "Jacob importer")
        if (imported.get("status") != "complete" or
                imported.get("start_row") != start or imported.get("end_row") != end or
                imported.get("car_sha256") != import_sha256 or
                imported.get("blocks") != len(rows)):
            raise ShipError("Jacob import receipt differs")

        if args.s3_destination:
            if not archive_receipt:
                raise ShipError("S3 archival requires the CARv2/Zstandard transport mode")
            manifest_path = args.state_dir / (name + "-carv2-zstd-manifest.json")
            manifest = {
                "schema": 1,
                "format": "carv2+zstd",
                "inventory_sha256": args.sha256,
                "start_row": start,
                "end_row": end,
                "blocks": len(rows),
                "carv1_sha256": archive_receipt["carv1_sha256"],
                "carv2_sha256": archive_receipt["carv2_sha256"],
                "compressed_sha256": archive_receipt["compressed_sha256"],
                "carv2_bytes": archive_receipt["carv2_bytes"],
                "compressed_bytes": archive_receipt["compressed_bytes"],
                "ratio_uncompressed_over_compressed": archive_receipt["ratio_uncompressed_over_compressed"],
                "kubo_import_root": imported["root"],
                "s3_object": object_uri,
            }
            if manifest_path.exists():
                if json.loads(manifest_path.read_text()) != manifest:
                    raise ShipError("saved compressed archive manifest differs")
            else:
                atomic_json(manifest_path, manifest)
            manifest_sha = sha_file(manifest_path)
            s3_manifest = upload_s3_file(
                manifest_path, object_uri + ".json",
                endpoint_url=args.s3_endpoint_url, region=args.s3_region,
                content_type="application/json",
                metadata={"sha256": manifest_sha,
                          "inventory-sha256": args.sha256})
            s3_object["manifest_uri"] = object_uri + ".json"
            s3_object["manifest_bytes"] = s3_manifest["bytes"]

        # Keep the durable import receipt, but remove the transient CAR before
        # advancing. A failed cleanup leaves the checkpoint at this range so
        # the next run can retry from its local verified CAR.
        remote_cleanup = ["rm", "-f", "--", remote_car]
        if args.carv2_zstd:
            remote_cleanup += [remote_plain_car, remote_unpack_receipt]
        run(["ssh", *ssh_opts, args.ssh_host,
             shlex.join(remote_cleanup)], timeout=120)
        if args.source_ssh_host:
            run(["ssh", *ssh_opts, args.source_ssh_host,
                 shlex.join(["rm", "-f", "--", source_car, source_receipt])], timeout=120)
        checkpoint_record = {"cursor": end, "inventory_sha256": args.sha256,
                             "last_root": imported["root"],
                             "last_car_sha256": import_sha256}
        if archive_receipt:
            checkpoint_record["last_transport_sha256"] = transfer_sha256
        if s3_object:
            checkpoint_record["last_s3_object"] = s3_object["uri"]
        atomic_json(checkpoint, checkpoint_record)
        car.unlink()
        if archive_path.exists():
            archive_path.unlink()
        result = {"status": "shipped", "start_row": start, "end_row": end,
                  "blocks": len(rows), "root": imported["root"]}
        if archive_receipt:
            result["compression"] = {
                "format": "carv2+zstd",
                "carv2_bytes": archive_receipt["carv2_bytes"],
                "compressed_bytes": archive_receipt["compressed_bytes"],
                "ratio_uncompressed_over_compressed": archive_receipt["ratio_uncompressed_over_compressed"],
            }
        if s3_object:
            result["s3_object"] = s3_object["uri"]
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--kbb", type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--start-row", required=True, type=int)
    parser.add_argument("--stop-row", type=int)
    parser.add_argument("--max-blocks", type=int, default=200)
    parser.add_argument("--max-bytes", type=int, default=100_000_000)
    parser.add_argument("--base-url")
    parser.add_argument("--resolve")
    parser.add_argument("--source-ssh-host")
    parser.add_argument("--source-exporter")
    parser.add_argument("--source-inventory")
    parser.add_argument("--source-ipfs-bin")
    parser.add_argument("--source-ipfs-path")
    parser.add_argument("--source-rpc-url")
    parser.add_argument("--source-output-dir")
    parser.add_argument("--raw-cids-file", type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--remote-dir", required=True)
    parser.add_argument("--remote-car-dir")
    parser.add_argument("--remote-large-archive")
    parser.add_argument("--remote-importer", required=True)
    parser.add_argument("--remote-inventory", required=True)
    parser.add_argument("--remote-ipfs-bin", required=True)
    parser.add_argument("--remote-ipfs-path", required=True)
    parser.add_argument("--remote-raw-block-store")
    parser.add_argument("--carv2-zstd", action="store_true",
                        help="transfer bounded archives as indexed CARv2 inside Zstandard")
    parser.add_argument("--s3-destination",
                        help="optional S3-compatible bucket/prefix for .carv2.zst objects")
    parser.add_argument("--s3-endpoint-url",
                        help="optional S3-compatible endpoint URL for aws s3 cp")
    parser.add_argument("--s3-region",
                        help="optional AWS signing region (for example, auto for R2)")
    args = parser.parse_args()
    try:
        if args.s3_destination and not args.carv2_zstd:
            raise ShipError("S3 archive upload requires --carv2-zstd")
        if args.s3_endpoint_url and not args.s3_destination:
            raise ShipError("--s3-endpoint-url requires --s3-destination")
        if args.s3_region and not args.s3_destination:
            raise ShipError("--s3-region requires --s3-destination")
        source_args = (args.source_exporter, args.source_inventory, args.source_ipfs_bin,
                       args.source_ipfs_path, args.source_output_dir)
        if args.source_ssh_host:
            if not all(source_args) or args.base_url or args.resolve:
                raise ShipError("remote Kubo source arguments are incomplete or mixed with HTTPS")
        elif not args.source or not args.kbb or not args.base_url or any(source_args) or args.source_rpc_url:
            raise ShipError("HTTPS source arguments are incomplete or mixed with remote Kubo")
        print(json.dumps(ship_once(args), sort_keys=True))
        return 0
    except (ShipError, OSError, ValueError, KeyError, IndexError) as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
