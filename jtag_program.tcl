# Load a bitstream into the U55C over JTAG (volatile: lost at power-off).
#
#   vivado_lab -mode batch -source jtag_program.tcl -tclargs u55c_py_access.bit
#
# Use program.sh, which first removes the card from the PCIe bus.
set bit [file normalize [lindex $argv 0]]
open_hw_manager
connect_hw_server -allow_non_jtag
open_hw_target
# Vivado Lab names the U55C device xcu280_u55c_0 (same silicon as the U280).
set dev [lindex [get_hw_devices *u55c*] 0]
if {$dev eq ""} { error "no U55C found on the JTAG chain: [get_hw_devices]" }
current_hw_device $dev
refresh_hw_device -update_hw_probes false $dev
set_property PROGRAM.FILE $bit $dev
program_hw_devices $dev
refresh_hw_device -update_hw_probes false $dev
# program_hw_devices errors out unless End of startup goes HIGH (DONE).
puts "JTAG: programmed $dev with $bit"
close_hw_target
disconnect_hw_server
