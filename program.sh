#!/bin/bash
# Load a bitstream into the U55C over JTAG, on the lab machine.
#
#   program.sh build/u55c_py_access.bit
#   program.sh --pcie-only     (PCIe steps only, no JTAG load: tests
#                                       that taking the link down is safe)
#
# Run as a user with sudo (enics). The card's PCIe link drops while the FPGA
# is reprogrammed. An unexpected link-down hung the lab machine on 2026-10-25,
# so before loading this script:
#   1. removes any Xilinx PCIe function from the bus,
#   2. on the slot's upstream port (bridge): masks the AER Surprise Down error,
#      turns off SERR reporting, and sets Link Disable, so the link goes down
#      on purpose before the FPGA drops it.
# Never power off the lab machine; nothing can switch it back on remotely.
set -euo pipefail

# Used by the q55 queue manager: sudo never prompts (the manager keeps its sudo
# credential alive), so an expired credential fails instead of hanging.
SUDO="sudo -n"

usage="usage: $0 BITSTREAM.bit | --pcie-only"
pcie_only=0
if [ "${1:-}" = "--pcie-only" ]; then pcie_only=1; else bit=$(readlink -f "${1:?$usage}"); fi
here=$(dirname "$(readlink -f "$0")")
card_bus=4e                 # secondary bus of the U55C slot (lspci: 4e:00.0)

# The bridge whose secondary bus is the card's bus; found from config space,
# so it works whether or not the card currently enumerates.
bridge=""
for dev in /sys/bus/pci/devices/*; do
    [ "$(( $(cat "$dev/class") >> 8 ))" = "$(( 0x0604 ))" ] || continue
    sec=$($SUDO setpci -s "$(basename "$dev")" SECONDARY_BUS.b)
    if [ "$sec" = "$card_bus" ]; then bridge=$(basename "$dev"); break; fi
done
[ -n "$bridge" ] || { echo "no bridge found for bus $card_bus" >&2; exit 1; }
echo "PCIe: card slot bridge $bridge"

for dev in /sys/bus/pci/devices/*; do
    if [ "$(cat "$dev/vendor")" = "0x10ee" ]; then
        echo "PCIe: removing $(basename "$dev") ($(cat "$dev/device"))"
        echo 1 | $SUDO tee "$dev/remove" > /dev/null
    fi
done

if $SUDO setpci -s "$bridge" ECAP_AER+0x08.l > /dev/null 2>&1; then
    $SUDO setpci -s "$bridge" ECAP_AER+0x08.l=00000020:00000020   # UEMSK: Surprise Down
fi
$SUDO setpci -s "$bridge" BRIDGE_CONTROL.w=0000:0002             # SERR# enable off
$SUDO setpci -s "$bridge" COMMAND.w=0000:0100                    # SERR# enable off
$SUDO setpci -s "$bridge" CAP_EXP+0x10.w=0010:0010               # LNKCTL: Link Disable
sleep 1
echo "PCIe: $bridge LNKCTL=$($SUDO setpci -s "$bridge" CAP_EXP+0x10.w)" \
     "LNKSTA=$($SUDO setpci -s "$bridge" CAP_EXP+0x12.w)"
[ "$pcie_only" = 1 ] && { echo "PCIe: --pcie-only, no JTAG load"; exit 0; }

source /tools/Xilinx/2026.1/Vivado_Lab/settings64.sh
vivado_lab -nolog -nojournal -mode batch \
    -source "$here/jtag_program.tcl" -tclargs "$bit"

# The link stays disabled: the new image has no PCIe. A warm reboot resets
# the bridge if a PCIe image is loaded later.
