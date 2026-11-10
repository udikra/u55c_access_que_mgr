#!/bin/bash
# Load a bitstream into the U55C over JTAG, on the lab machine.
#
#   program.sh BITSTREAM.bit                  load, the PCIe link stays down
#   program.sh BITSTREAM.bit --pcie USER      load, bring the PCIe link back up, rescan the card
#                                             and give USER access to its PCIe devices
#   program.sh --pcie-grant USER              give USER access to the card's PCIe devices
#                                             (the link is already up , no load)
#   program.sh --pcie-up USER                 bring the PCIe link up and give USER the devices,
#                                             no load (recovery , or test with --pcie-only first)
#   program.sh --pcie-only                    PCIe steps only, no JTAG load: tests
#                                             that taking the link down is safe
#
# Run as a user with sudo (enics). The card's PCIe link drops while the FPGA
# is reprogrammed. An unexpected link-down hung the lab machine on 2026-10-25,
# so before loading this script:
#   1. removes any Xilinx PCIe function from the bus,
#   2. on the slot's upstream port (bridge): masks the AER Surprise Down error,
#      turns off SERR reporting, and sets Link Disable, so the link goes down
#      on purpose before the FPGA drops it.
# With --pcie, after the load it clears Link Disable, waits for the link
# (DLActive), rescans the card's bus and sets the device permissions for USER.
# The AER mask and SERR settings stay as they are, so the next load is safe.
# Never power off the lab machine; nothing can switch it back on remotely.
set -euo pipefail

# Used by the q55 queue manager: sudo never prompts (the manager keeps its sudo
# credential alive), so an expired credential fails instead of hanging.
SUDO="sudo -n"

usage="usage: $0 BITSTREAM.bit [--pcie USER] | --pcie-grant USER | --pcie-up USER | --pcie-only"
mode=load; bit=""; pcie_user=""
case "${1:-}" in
    --pcie-only)  mode=pcie_only ;;
    --pcie-grant) mode=grant; pcie_user="${2:?$usage}" ;;
    --pcie-up)    mode=up;    pcie_user="${2:?$usage}" ;;
    ""|-*)        echo "$usage" >&2; exit 2 ;;
    *)            bit=$(readlink -f "$1")
                  if [ "${2:-}" = "--pcie" ]; then pcie_user="${3:?$usage}"
                  elif [ -n "${2:-}" ]; then echo "$usage" >&2; exit 2; fi ;;
esac
if [ -n "$pcie_user" ]; then
    id -u "$pcie_user" > /dev/null 2>&1 || { echo "no such user: $pcie_user" >&2; exit 2; }
fi
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
domain=${bridge%%:*}
echo "PCIe: card slot bridge $bridge"

# The card's functions (Xilinx vendor id) on its bus, empty while the link is down
card_devs() {
    local dev
    for dev in /sys/bus/pci/devices/"$domain:$card_bus":*; do
        [ -e "$dev" ] && [ "$(cat "$dev/vendor")" = "0x10ee" ] && echo "$dev"
    done
    return 0
}

# Gives one user read/write access to the card: its BAR files (owner, like
# chown) and the XDMA character devices (ACL, the previous user's entry is
# removed). Both are recreated with root-only access on every load.
grant() {
    local user=$1 devs dev f n=0
    devs=$(card_devs)
    [ -n "$devs" ] || { echo "PCIe: no U55C function on bus $card_bus, is the link up?" >&2; return 1; }
    for dev in $devs; do
        for f in "$dev"/resource[0-9] "$dev"/resource[0-9]_wc; do      # BARs , not resourceN_resize
            [ -e "$f" ] || continue
            $SUDO chown "$user" "$f"
            $SUDO chmod u+rw "$f"
            echo "PCIe: $f -> $user"
        done
    done
    udevadm settle --timeout=10 > /dev/null 2>&1 || true   # the XDMA driver creates /dev/xdma* via udev
    for f in /dev/xdma*; do
        [ -c "$f" ] || continue
        $SUDO setfacl -b "$f"
        $SUDO setfacl -m "u:$user:rw" "$f"
        n=$((n + 1))
    done
    if [ "$n" = 0 ]; then
        echo "PCIe: WARNING: no /dev/xdma* devices, is the XDMA driver loaded? (BAR files only)"
    else
        echo "PCIe: $n /dev/xdma* devices -> $user"
    fi
}

link_status() {
    echo "LNKCTL=$($SUDO setpci -s "$bridge" CAP_EXP+0x10.w) LNKSTA=$($SUDO setpci -s "$bridge" CAP_EXP+0x12.w)"
}

# Clears Link Disable on the bridge, waits for the data link layer, rescans
# the card's bus. On failure the link is disabled again (the known safe state).
pcie_up() {
    local i sta up=0
    echo on | $SUDO tee "/sys/bus/pci/devices/$bridge/power/control" > /dev/null   # no runtime suspend
    $SUDO setpci -s "$bridge" CAP_EXP+0x10.w=0000:0010             # LNKCTL: Link Disable off
    for i in $(seq 1 10); do
        sleep 1
        sta=$($SUDO setpci -s "$bridge" CAP_EXP+0x12.w)
        if (( 0x$sta & 0x2000 )); then up=1; break; fi             # LNKSTA: DLActive
    done
    echo "PCIe: $bridge $(link_status)"
    if [ "$up" != 1 ]; then
        echo "PCIe: link did not come up (does the bitstream have a PCIe core?), disabling it again" >&2
        $SUDO setpci -s "$bridge" CAP_EXP+0x10.w=0010:0010
        return 1
    fi
    echo 1 | $SUDO tee "/sys/class/pci_bus/$domain:$card_bus/rescan" > /dev/null
    sleep 1
    local devs; devs=$(card_devs)
    [ -n "$devs" ] || { echo "PCIe: link is up but no U55C function appeared on bus $card_bus" >&2; return 1; }
    local dev drv
    for dev in $devs; do
        drv=none
        [ -e "$dev/driver" ] && drv=$(basename "$(readlink -f "$dev/driver")")
        echo "PCIe: found $(basename "$dev") ($(cat "$dev/device")), driver $drv"
    done
}

if [ "$mode" = grant ]; then
    grant "$pcie_user"
    exit 0
fi
if [ "$mode" = up ]; then
    pcie_up
    grant "$pcie_user"
    exit 0
fi

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
echo "PCIe: $bridge $(link_status)"
[ "$mode" = pcie_only ] && { echo "PCIe: --pcie-only, no JTAG load"; exit 0; }

source /tools/Xilinx/2026.1/Vivado_Lab/settings64.sh
vivado_lab -nolog -nojournal -mode batch \
    -source "$here/jtag_program.tcl" -tclargs "$bit"

if [ -n "$pcie_user" ]; then
    pcie_up
    grant "$pcie_user"
fi
# Without --pcie the link stays disabled: q55 prog -pci (or a warm reboot)
# brings it up when a PCIe image is loaded later.
