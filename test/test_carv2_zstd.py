import hashlib
import importlib.util
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
source = ROOT / "deploy" / "carv2_zstd.py"
spec = importlib.util.spec_from_file_location("carv2_zstd", source)
car = importlib.util.module_from_spec(spec)
spec.loader.exec_module(car)


def car_v1_fixture(payload=b"Yataverse block payload"):
    digest = hashlib.sha256(payload).digest()
    cid = (car.uvarint(1) + car.uvarint(0x55) + car.uvarint(car.SHA2_256) +
           car.uvarint(len(digest)) + digest)
    header = (b"\xa2\x65roots\x81\xd8\x2a\x58" +
              bytes((len(cid) + 1,)) + b"\x00" + cid + b"\x67version\x01")
    frame = car.uvarint(len(cid) + len(payload)) + cid + payload
    return car.uvarint(len(header)) + header + frame


def car_v2_from_car_v1(carv1):
    payload = io.BytesIO()
    rows = car.copy_carv1_payload(io.BytesIO(carv1), payload, len(carv1), 10)
    index = car.encode_index(rows)
    offset = car.CARV2_DATA_OFFSET + len(carv1)
    return (car.PRAGMA + car.carv2_header(len(carv1), offset) +
            carv1 + index)


class Carv2ZstdTests(unittest.TestCase):
    def test_carv1_fixture_has_a_valid_block_digest(self):
        fixture = car_v1_fixture()
        rows = car.copy_carv1_payload(io.BytesIO(fixture), io.BytesIO(),
                                      len(fixture), 10)
        self.assertEqual(1, len(rows))
        self.assertEqual(hashlib.sha256(b"Yataverse block payload").digest(),
                         rows[0][1])

    def test_indexed_carv2_validation_rejects_corrupt_index_and_truncation(self):
        carv2 = car_v2_from_car_v1(car_v1_fixture())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.carv2"
            path.write_bytes(carv2)
            self.assertEqual(1, car.validate_carv2(path, 10)["blocks"])

            path.write_bytes(carv2[:-1] + bytes((carv2[-1] ^ 1,)))
            with self.assertRaisesRegex(car.ArchiveError, "index does not match"):
                car.validate_carv2(path, 10)

            path.write_bytes(carv2[:-1])
            with self.assertRaises(car.ArchiveError):
                car.validate_carv2(path, 10)

    def test_block_digest_mismatch_is_rejected(self):
        fixture = bytearray(car_v1_fixture())
        fixture[-1] ^= 1
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.car"
            source_path.write_bytes(fixture)
            with self.assertRaisesRegex(car.ArchiveError,
                                        "block bytes do not match"):
                car.copy_carv1_payload(io.BytesIO(fixture), io.BytesIO(),
                                       len(fixture), 10)

    @unittest.skipUnless(shutil.which("zstd"), "zstd CLI is required")
    def test_zstd_pack_unpack_round_trip_preserves_carv1_and_index(self):
        carv1 = car_v1_fixture()
        expected_carv2 = car_v2_from_car_v1(carv1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.car"
            compressed = root / "archive.carv2.zst"
            pack_receipt = root / "pack.json"
            restored = root / "restored.carv2"
            unpack_receipt = root / "unpack.json"
            source_path.write_bytes(carv1)

            packed = car.pack(SimpleNamespace(
                carv1=source_path, output=compressed, receipt=pack_receipt,
                max_blocks=10, max_bytes=1024 * 1024, timeout=20))
            self.assertEqual("carv2+zstd", packed["format"])
            self.assertEqual(len(expected_carv2), packed["carv2_bytes"])
            self.assertEqual(len(expected_carv2),
                             car.zstd_frame_content_size(compressed))

            unpacked = car.unpack(SimpleNamespace(
                compressed=compressed, output=restored, receipt=unpack_receipt,
                expected_compressed_sha256=packed["compressed_sha256"],
                expected_carv2_sha256=packed["carv2_sha256"],
                max_blocks=10, max_bytes=packed["carv2_bytes"], timeout=20))
            restored_bytes = restored.read_bytes()
            self.assertEqual(expected_carv2, restored_bytes)
            self.assertEqual(carv1, restored_bytes[
                car.CARV2_DATA_OFFSET:car.CARV2_DATA_OFFSET + len(carv1)])
            self.assertEqual(packed["carv2_sha256"], unpacked["carv2_sha256"])
            self.assertEqual(1, unpacked["blocks"])

            reused = car.unpack(SimpleNamespace(
                compressed=compressed, output=restored, receipt=unpack_receipt,
                expected_compressed_sha256=packed["compressed_sha256"],
                expected_carv2_sha256=packed["carv2_sha256"],
                max_blocks=10, max_bytes=packed["carv2_bytes"], timeout=20))
            self.assertEqual(unpacked, reused)

    @unittest.skipUnless(shutil.which("zstd"), "zstd CLI is required")
    def test_unpack_fails_closed_on_compressed_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.car"
            compressed = root / "archive.carv2.zst"
            source_path.write_bytes(car_v1_fixture())
            packed = car.pack(SimpleNamespace(
                carv1=source_path, output=compressed, receipt=root / "pack.json",
                max_blocks=10, max_bytes=1024 * 1024, timeout=20))

            with self.assertRaisesRegex(car.ArchiveError,
                                        "compressed SHA-256 differs"):
                car.unpack(SimpleNamespace(
                    compressed=compressed, output=root / "restored.carv2",
                    receipt=root / "unpack.json",
                    expected_compressed_sha256="0" * 64,
                    expected_carv2_sha256=packed["carv2_sha256"],
                    max_blocks=10, max_bytes=packed["carv2_bytes"], timeout=20))


if __name__ == "__main__":
    unittest.main()
