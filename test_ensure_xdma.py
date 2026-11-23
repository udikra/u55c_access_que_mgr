import importlib.util
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ensure_xdma', Path(__file__).with_name('ensure_xdma.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ReadinessTests(unittest.TestCase):
    def test_grant_propagates_readiness_failure(self):
        script = Path(__file__).with_name('program.sh').read_text()
        grant = script[script.index('grant() {'):script.index('\nlink_status()')]
        code = """set -euo pipefail
here=/unused
card_bus=4e
card_devs() { echo /fake/card; }
reject() { return 23; }
SUDO=reject
""" + grant + '\ngrant almog\necho FALSE_SUCCESS\n'
        result = subprocess.run(['bash', '-c', code], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('FALSE_SUCCESS', result.stdout)

    def test_complete_single_card(self):
        self.assertTrue(m.complete([Path('/dev/xdma3_' + suffix) for suffix in ('h2c_0', 'user', 'control')]))

    def test_missing_h2c(self):
        self.assertFalse(m.complete([Path('/dev/xdma0_user'), Path('/dev/xdma0_control')]))

    def test_different_card_indices_do_not_satisfy_readiness(self):
        self.assertFalse(m.complete([Path('/dev/xdma0_h2c_0'), Path('/dev/xdma1_user'), Path('/dev/xdma1_control')]))

    def test_allocated_bars(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev = Path(tmp)
            (dev / 'resource').write_text('a8000000 a83fffff 40200\na8400000 a840ffff 40200\n')
            m.check_bars(dev)

    def test_unassigned_bar_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev = Path(tmp)
            (dev / 'resource').write_text('0 0 0\na8400000 a840ffff 40200\n')
            with self.assertRaisesRegex(RuntimeError, 'BAR0'):
                m.check_bars(dev)

    def test_small_bar_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev = Path(tmp)
            (dev / 'resource').write_text('a8000000 a81fffff 40200\na8400000 a840ffff 40200\n')
            with self.assertRaisesRegex(RuntimeError, 'BAR0'):
                m.check_bars(dev)

    def test_903f_layout_bars(self):
        # k5 U55C image: BAR0 2 MiB , BAR1 64 KiB (the 0x9048 4 MiB BAR0 rule stays for 0x9048)
        with tempfile.TemporaryDirectory() as tmp:
            dev = Path(tmp)
            (dev / 'resource').write_text('a8000000 a81fffff 40200\na8200000 a820ffff 40200\n')
            m.check_bars(dev, m.SUPPORTED['0x903f'])
            with self.assertRaisesRegex(RuntimeError, 'BAR0'):
                m.check_bars(dev, m.SUPPORTED['0x9048'])

    def test_903f_small_bar0_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev = Path(tmp)
            (dev / 'resource').write_text('a8000000 a80fffff 40200\na8200000 a820ffff 40200\n')
            with self.assertRaisesRegex(RuntimeError, 'BAR0'):
                m.check_bars(dev, m.SUPPORTED['0x903f'])

    def test_unsupported_id_is_bar_only(self):
        # an unknown device ID loads nothing: only already existing nodes of that card are returned
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dev = base / '0000:4e:00.0'; dev.mkdir()
            (dev / 'vendor').write_text('0x10ee\n'); (dev / 'device').write_text('0x7038\n')
            with patch.object(m, 'card_nodes', return_value=['NODES']) as cn, \
                 patch.object(Path, 'parent', new_callable=lambda: property(lambda s: Path('/sys/bus/pci/devices'))), \
                 patch.object(m, 'run') as run:
                self.assertEqual(m.ensure(dev), ['NODES'])
                run.assert_not_called()
                cn.assert_called_once()

    def test_untrusted_module_rejected(self):
        with tempfile.NamedTemporaryFile() as tmp:
            with self.assertRaisesRegex(RuntimeError, 'Untrusted driver path'):
                m.trusted_module(Path(tmp.name))

    def test_non_pci_driver_rejected(self):
        with patch.object(Path, 'resolve', return_value=Path('/xdma.ko')), \
             patch.object(Path, 'stat') as info, patch.object(Path, 'is_file', return_value=True), \
             patch.object(m, 'run', side_effect=[m.os.uname().release, 'platform:xdma']):
            info.return_value.st_uid = 0
            info.return_value.st_mode = 0o100644
            with self.assertRaisesRegex(RuntimeError, 'not the PCIe'):
                m.trusted_module(Path('/xdma.ko'))

    def test_kernel_mismatch_rejected(self):
        with patch.object(Path, 'resolve', return_value=Path('/xdma.ko')), \
             patch.object(Path, 'stat') as info, patch.object(Path, 'is_file', return_value=True), \
             patch.object(m, 'run', return_value='wrong-kernel SMP'):
            info.return_value.st_uid = 0
            info.return_value.st_mode = 0o100644
            with self.assertRaisesRegex(RuntimeError, 'differs from running kernel'):
                m.trusted_module(Path('/xdma.ko'))

    def test_unrelated_card_nodes_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dev = base / 'card'; dev.mkdir()
            other = base / 'other'; other.mkdir()
            cls = base / 'class'; cls.mkdir()
            entry = cls / 'xdma9_h2c_0'; entry.mkdir()
            (entry / 'device').symlink_to(other)
            self.assertEqual(m.card_nodes(dev, cls, base / 'nodes'), [])

if __name__ == '__main__':
    unittest.main()
