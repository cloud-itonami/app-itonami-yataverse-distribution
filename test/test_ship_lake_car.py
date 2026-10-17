import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "deploy" / "ship_lake_car.py"
spec = importlib.util.spec_from_file_location("ship_lake_car", source)
ship = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ship)

CID_A = "QmNLgJDagPKXUg4C1vKtvETQhTxEDScNYwEhYBke5uBan8"
CID_B = "QmNLhRArQBktFvoG2vgmoM3oMZC8vgWGhk9sM4Q4KkGUxh"


class ShipLakeCarTests(unittest.TestCase):
    def inventory(self, directory, sizes):
        path = Path(directory) / "inventory.jsonl"
        data = b"".join((json.dumps({"cid": cid, "bytes": size}) + "\n").encode()
                        for cid, size in zip([CID_A, CID_B], sizes))
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()

    def test_range_stops_before_oversized_and_records_exact_row(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = self.inventory(directory, [3, ship.MAX_CAR_BLOCK + 1])
            rows, skip = ship.selected_rows(path, digest, 2, 0, 2, 100_000_000)
            self.assertEqual([{"cid": CID_A, "bytes": 3}], rows)
            self.assertIsNone(skip)
            rows, skip = ship.selected_rows(path, digest, 2, 1, 2, 100_000_000)
            self.assertEqual([], rows)
            self.assertEqual({"row": 1, "cid": CID_B,
                              "bytes": ship.MAX_CAR_BLOCK + 1,
                              "reason": "oversized-for-car"}, skip)

    def test_changed_inventory_refuses_before_shipping(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = self.inventory(directory, [3, 4])
            path.write_bytes(path.read_bytes() + b"\n")
            with self.assertRaisesRegex(ship.ShipError, "identity differs"):
                ship.selected_rows(path, digest, 2, 0, 2, 100_000_000)

    def test_raw_store_row_is_skipped_only_after_prior_car_range(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = self.inventory(directory, [3, 4])
            rows, skip = ship.selected_rows(path, digest, 2, 0, 2,
                                            100_000_000, {CID_B})
            self.assertEqual([{"cid": CID_A, "bytes": 3}], rows)
            self.assertIsNone(skip)
            rows, skip = ship.selected_rows(path, digest, 2, 1, 2,
                                            100_000_000, {CID_B})
            self.assertEqual([], rows)
            self.assertEqual({"row": 1, "cid": CID_B, "bytes": 4,
                              "reason": "verified-raw-block-store"}, skip)

    def test_checkpoint_replace_is_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            ship.atomic_json(path, {"cursor": 200})
            ship.atomic_json(path, {"cursor": 400})
            self.assertEqual({"cursor": 400}, json.loads(path.read_text()))

    def test_missing_remote_receipt_refuses_without_checkpoint(self):
        with self.assertRaisesRegex(ship.ShipError, "returned no receipt"):
            ship.last_receipt("", "Jacob importer")

    def test_s3_object_uri_joins_prefix_without_duplicate_slashes(self):
        self.assertEqual("s3://bucket/lake/inventory/range.carv2.zst",
                         ship.s3_object_uri("s3://bucket/lake/",
                                            "/inventory/range.carv2.zst"))
        for invalid in ("https://bucket/lake", "s3://user@bucket/lake",
                        "s3://bucket/lake?version=1", "s3:///lake"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ship.ShipError, "s3://bucket/prefix"):
                    ship.s3_object_uri(invalid, "object")

    def test_s3_upload_checks_head_size_and_metadata(self):
        metadata = {"format": "carv2+zstd", "sha256": "a" * 64}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "range.carv2.zst"
            path.write_bytes(b"compressed archive")
            expected_size = path.stat().st_size
            with patch.object(ship, "run", side_effect=["", json.dumps({
                    "ContentLength": expected_size,
                    "Metadata": metadata})]) as command:
                uploaded = ship.upload_s3_file(
                    path, "s3://bucket/lake/archive.carv2.zst",
                    endpoint_url="https://r2.example", region="auto",
                    content_type="application/zstd", metadata=metadata)

        self.assertEqual(expected_size, uploaded["bytes"])
        self.assertEqual(metadata, uploaded["metadata"])
        upload_command, head_command = [call.args[0] for call in command.call_args_list]
        self.assertEqual("aws", upload_command[0])
        self.assertIn("https://r2.example", upload_command)
        self.assertIn("auto", head_command)
        self.assertIn("head-object", head_command)

    def test_s3_upload_refuses_head_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "range.carv2.zst"
            path.write_bytes(b"compressed archive")
            with patch.object(ship, "run", side_effect=["", json.dumps({
                    "ContentLength": 0, "Metadata": {}})]):
                with self.assertRaisesRegex(ship.ShipError,
                                            "HEAD differs from uploaded object"):
                    ship.upload_s3_file(
                        path, "s3://bucket/lake/archive.carv2.zst",
                        endpoint_url=None, region=None,
                        content_type="application/zstd",
                        metadata={"format": "carv2+zstd"})

    def test_oversized_skip_requires_matching_jacob_archive(self):
        skipped = {"row": 1, "cid": CID_B, "bytes": ship.MAX_CAR_BLOCK + 1,
                   "reason": "oversized-for-car"}
        args = SimpleNamespace(remote_large_archive="/recovery", ssh_host="jacob",
                               sha256="a" * 64)
        receipt = {"schema": 1, "row": 1, "inventory_sha256": args.sha256,
                   "original_cid": CID_B, "original_bytes": skipped["bytes"],
                   "validation": "fresh-offline-kubo-import-cat-and-original-cid-rehydration",
                   "car_sha256": "b" * 64, "car_bytes": 8100000}
        with patch.object(ship, "run", return_value=json.dumps(
                {"receipt": receipt, "car_bytes_on_disk": 8100000})) as remote:
            ship.verify_remote_large_archive(args, skipped)
        self.assertIn("row-1-recovery.car", remote.call_args.args[0][-1])
        with patch.object(ship, "run", return_value=json.dumps(
                {"receipt": receipt, "car_bytes_on_disk": 8100001})):
            with self.assertRaisesRegex(ship.ShipError, "receipt differs"):
                ship.verify_remote_large_archive(args, skipped)
        with patch.object(ship, "run", return_value=json.dumps(
                {"receipt": {**receipt, "original_cid": CID_A},
                 "car_bytes_on_disk": 8100000})):
            with self.assertRaisesRegex(ship.ShipError, "receipt differs"):
                ship.verify_remote_large_archive(args, skipped)
        args.remote_large_archive = None
        with self.assertRaisesRegex(ship.ShipError, "no Jacob recovery archive"):
            ship.verify_remote_large_archive(args, skipped)


if __name__ == "__main__":
    unittest.main()
