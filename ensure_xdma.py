#!/usr/bin/env python3
"""Validate a known XDMA endpoint; print only its verified character-node paths."""
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time


def run(*args):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=30).stdout.strip()


def trusted_module(path):
    path = path.resolve(strict=True)
    for entry in (path, *path.parents):
        info = entry.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(f'Untrusted driver path: {entry}; require root ownership and no group/other writes')
    if not path.is_file():
        raise RuntimeError(f'Not a module file: {path}')
    version = run('modinfo', '-F', 'vermagic', str(path)).split()[0]
    if version != os.uname().release:
        raise RuntimeError(f'Driver kernel {version} differs from running kernel {os.uname().release}; rebuild driver')
    aliases = run('modinfo', '-F', 'alias', str(path)).splitlines()
    if not any(alias.startswith('pci:') for alias in aliases):
        raise RuntimeError('Configured module is not the PCIe XDMA driver')
    return path


def card_nodes(dev, class_root=Path('/sys/class/xdma'), node_root=Path('/dev')):
    """device_create() uses the PCI function as parent; never grant another card."""
    result = []
    for entry in sorted(class_root.glob('xdma*')):
        parent = entry / 'device'
        if not parent.exists() or parent.resolve() != dev.resolve():
            continue
        node = node_root / entry.name
        if not node.exists() or not stat.S_ISCHR(node.stat().st_mode):
            continue
        expected = (entry / 'dev').read_text().strip()
        number = node.stat().st_rdev
        if expected == f'{os.major(number)}:{os.minor(number)}':
            result.append(node)
    return result


def complete(nodes):
    # Require the same index for the image channel and user/control interfaces.
    names = {p.name for p in nodes}
    return any(re.fullmatch(r'xdma\d+_h2c_0', name) and
               name.removesuffix('_h2c_0') + '_user' in names and
               name.removesuffix('_h2c_0') + '_control' in names for name in names)


# Supported XDMA layouts: PCI device ID -> minimum (BAR index, bytes) sizes.
#   0x9048: the originally tested endpoint (BAR0 4 MiB , BAR1 64 KiB)
#   0x903f: the k5 U55C image (k5_xbox comp_fpga_u55c.py -pci: BAR0 2 MiB AXI-Lite to the k5 system space ,
#           BAR1 64 KiB XDMA DMA registers ; the DMA channels reach the 64 KB BRAM or , with -hbm , the HBM)
SUPPORTED = {
    '0x9048': ((0, 4 * 1024 * 1024), (1, 64 * 1024)),
    '0x903f': ((0, 2 * 1024 * 1024), (1, 64 * 1024)),
}


def check_bars(dev, minima=SUPPORTED['0x9048']):
    lines = (dev / 'resource').read_text().splitlines()
    for index, minimum in minima:
        start, end, flags = (int(v, 16) for v in lines[index].split())
        if not start or end - start + 1 < minimum or not flags & 0x200:
            raise RuntimeError(f'{dev.name}: BAR{index} is unassigned or smaller than {minimum}; check bridge allocation')


def ensure(dev):
    if dev.parent != Path('/sys/bus/pci/devices') or not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', dev.name):
        raise RuntimeError('Expected an exact PCI sysfs function path')
    if (dev / 'vendor').read_text().strip() != '0x10ee':
        raise RuntimeError('Not a Xilinx endpoint')
    # Only the supported layouts (SUPPORTED) require this driver.
    # Other bitstreams keep BAR-only operation; existing matching nodes can be granted.
    minima = SUPPORTED.get((dev / 'device').read_text().strip())
    if minima is None:
        return card_nodes(dev)
    check_bars(dev, minima)
    driver = dev / 'driver'
    if driver.exists() and driver.resolve().name != 'xdma':
        raise RuntimeError(f'{dev.name} is bound to {driver.resolve().name}; refusing to unbind it automatically')
    if not Path('/sys/bus/pci/drivers/xdma').exists():
        if Path('/sys/module/xdma').exists():
            raise RuntimeError('A non-PCIe xdma module is already loaded; coordinate replacement, no automatic unload')
        module = Path(__file__).resolve().parent / 'xdma' / os.uname().release / 'xdma.ko'
        module = trusted_module(module)
        print(f'PCIe: loading verified PCIe driver {module}', file=sys.stderr)
        run('insmod', str(module))
    if not driver.exists():
        Path('/sys/bus/pci/drivers_probe').write_text(dev.name)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        nodes = card_nodes(dev)
        if driver.exists() and driver.resolve().name == 'xdma' and complete(nodes):
            print(f'PCIe: {dev.name} XDMA ready ({len(nodes)} matching nodes)', file=sys.stderr)
            return nodes
        time.sleep(0.25)
    raise RuntimeError(f'{dev.name}: XDMA binding/user/control/H2C nodes not ready; inspect kernel logs')


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise RuntimeError('usage: ensure_xdma.py /sys/bus/pci/devices/BDF')
        for node in ensure(Path(sys.argv[1])):
            print(node)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        print(f'PCIe: ERROR: {exc}', file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        sys.exit(1)
