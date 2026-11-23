# XDMA recovery

For the supported Xilinx XDMA endpoints, PCIe link-up alone is insufficient.
`program.sh` calls `ensure_xdma.py` before granting access. It verifies the BAR
sizes of the endpoint's layout, the PCIe XDMA driver, and matching user/control/H2C
nodes. Layouts (`SUPPORTED` in `ensure_xdma.py`):

| Device | BAR0 | BAR1 | Image |
|---|---|---|---|
| 10ee:9048 | 4 MiB | 64 KiB | the originally tested endpoint |
| 10ee:903f | 2 MiB | 64 KiB | k5 U55C image (`comp_fpga_u55c.py -pci`, k5_xbox): BAR0 AXI-Lite to the k5 system space , DMA to the 64 KB BRAM or (`-hbm`) the 16 GB HBM |

A missing driver is loaded from `/opt/q55/xdma/$(uname -r)/xdma.ko`.
The module and all parent directories must be root-owned and not writable by
other users. Kernel version and PCI aliases are checked before loading.
The unrelated in-tree `platform:xdma` module is not used. Conflicting loaded
drivers are not unloaded automatically. Failures propagate to the queue manager.

Granting access is scoped to nodes whose sysfs parent is the target PCI function;
other cards are not modified. Other PCI IDs keep BAR-only operation. Link training
gets 30 seconds. Pre-programming removal is restricted to the configured slot bus.
The existing asynchronous manager permission-transfer logic is unchanged.

## Driver installation (enics, idle maintenance window)

Build the Xilinx PCIe XDMA driver for the running kernel, using source commit
`a918c5196a09f95756af244a607d543d9dce5bfb` from Xilinx/dma_ip_drivers.
From its `XDMA/linux-kernel/xdma` directory:

```sh
make -j4 "ccflags-y=-I$(realpath ../include)"
modinfo -F vermagic ./xdma.ko
modinfo -F alias ./xdma.ko
sudo install -d -o root -g root -m 755 "/opt/q55/xdma/$(uname -r)"
sudo install -o root -g root -m 644 ./xdma.ko "/opt/q55/xdma/$(uname -r)/xdma.ko"
```

Install `ensure_xdma.py` alongside the patched `program.sh` per README.md.
After a kernel upgrade rebuild the module; do not reuse the previous binary.
If signature enforcement rejects the module, use the lab's signing procedure.
No prebuilt driver binaries are committed to this repository.

## Validation

```sh
python3 -m unittest discover -p 'test_*.py' -v
bash -n program.sh
```

Eleven hardware-free tests cover BAR assignment, matching device indices,
conflicting kernel/platform modules, trusted module paths, unrelated-card
exclusion, and propagation of readiness failure.

On 2026-11-15, enics installed this fix on enics-ThinkStation-P5 with a driver
rebuilt for 6.17.0-35-generic. A fresh q55 `-pci` programming cycle completed;
`lspci` showed `Kernel driver in use: xdma`, device nodes appeared and almog
could read/write H2C. The profile10009 smoke-A-B-A run verified all three jobs
and its independent returned-data audit passed without manual PCIe recovery.
These are operator-reported results, not a general hardware qualification.
