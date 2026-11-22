import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('append_hfs_media', Path(__file__).parents[1] / 'build/append_hfs_media.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MediaPartitionTests(unittest.TestCase):
    def files(self, root):
        disk = bytearray(64 * 512)
        disk[:2] = b'ER'
        struct.pack_into('>HI', disk, 2, 512, 64)
        for i, start, blocks, kind in [(1, 1, 7, b'Apple_partition_map'), (2, 8, 56, b'Apple_HFS')]:
            off = i * 512
            disk[off:off+2] = b'PM'
            struct.pack_into('>III', disk, off+4, 2, start, blocks)
            disk[off+48:off+48+len(kind)] = kind
        disk[8*512:] = b'G' * (56*512)
        media = bytearray(11*512)
        media[:2] = b'ER'
        struct.pack_into('>HI', media, 2, 512, 11)
        for i, start, blocks, kind in [(1, 1, 2, b'Apple_partition_map'), (2, 3, 8, b'Apple_HFS')]:
            off = i * 512
            media[off:off+2] = b'PM'
            struct.pack_into('>III', media, off+4, 2, start, blocks)
            media[off+48:off+48+len(kind)] = kind
        media[3*512+1024:3*512+1026] = b'BD'
        image = root / 'disk.img'
        source = root / 'media.img'
        image.write_bytes(disk)
        source.write_bytes(media)
        return image, source, bytes(disk), bytes(media)

    def test_appends_original_volume_and_keeps_guest_data(self):
        with tempfile.TemporaryDirectory() as d:
            image, source, old, media = self.files(Path(d))
            result = module.append(image, source)
            data = image.read_bytes()
            self.assertEqual(data[len(old):], media[3*512:])
            self.assertEqual(data[8*512:len(old)], old[8*512:])
            self.assertEqual(source.read_bytes(), media)
            self.assertEqual(struct.unpack_from('>I', data, 4)[0], 72)
            self.assertEqual(struct.unpack_from('>III', data, 3*512+4), (3, 64, 8))
            self.assertEqual(struct.unpack_from('>I', data, 3*512+88)[0], 0x4400035f)
            self.assertEqual(result['partition'], 3)

    def test_bad_map_refused_before_writes(self):
        with tempfile.TemporaryDirectory() as d:
            image, source, old, _ = self.files(Path(d))
            data = bytearray(old)
            struct.pack_into('>I', data, 2*512+12, 100)
            image.write_bytes(data)
            with self.assertRaises(ValueError):
                module.append(image, source)
            self.assertEqual(image.read_bytes(), data)

    def test_non_hfs_media_refused_before_writes(self):
        with tempfile.TemporaryDirectory() as d:
            image, source, old, _ = self.files(Path(d))
            source.write_bytes(bytes(4096))
            with self.assertRaises(ValueError):
                module.append(image, source)
            self.assertEqual(image.read_bytes(), old)


if __name__ == '__main__':
    unittest.main()
