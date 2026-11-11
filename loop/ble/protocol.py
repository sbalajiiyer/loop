"""WHOOP 5.0 / MG GATT layout and command numbers.

Everything here comes from community reverse engineering, not WHOOP. Values marked
VERIFY have been reported to work by other projects but are not yet confirmed on
your strap; the collector stores every raw frame so nothing is lost if one is off.
"""

# Custom service and characteristics (fd4b0001 family)
SERVICE = "fd4b0001-cce1-4033-93ce-002d5875f58a"
CHAR_CMD_TO_STRAP = "fd4b0002-cce1-4033-93ce-002d5875f58a"
CHAR_CMD_FROM_STRAP = "fd4b0003-cce1-4033-93ce-002d5875f58a"
CHAR_EVENTS = "fd4b0004-cce1-4033-93ce-002d5875f58a"
CHAR_DATA = "fd4b0005-cce1-4033-93ce-002d5875f58a"
CHAR_EXTRA = "fd4b0007-cce1-4033-93ce-002d5875f58a"
NOTIFY_CHARS = {
    "cmd": CHAR_CMD_FROM_STRAP,
    "events": CHAR_EVENTS,
    "data": CHAR_DATA,
    "extra": CHAR_EXTRA,
}

# Standard Bluetooth services (reliable ground truth)
HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

# Static session-start frame (type 35 command 0x91), confirmed byte-for-byte by
# several projects; writing it with response also triggers bonding.
CLIENT_HELLO = bytes.fromhex("aa0108000001e67123019101363e5c8d")

# Packet types (first payload byte)
PT_COMMAND = 35
PT_COMMAND_RESPONSE = 36
PT_PUFFIN_COMMAND_RESPONSE = 38
PT_REALTIME = 40          # ~1 Hz live record: HR, R-R, timestamp
PT_REALTIME_RAW = 43
PT_HISTORICAL = 47        # stored history records
PT_EVENT = 48
PT_METADATA = 49          # history start / end / complete markers
PT_PUFFIN_METADATA = 56

# Command numbers (shared with 4.0, re-framed on the puffin transport). VERIFY
CMD_TOGGLE_REALTIME_HR = 3
CMD_TOGGLE_GENERIC_HR_PROFILE = 14   # turns on standard 0x2A37 broadcast
CMD_SEND_HISTORICAL_DATA = 22
CMD_HISTORICAL_DATA_RESULT = 23      # ack with trim cursor
CMD_GET_BATTERY_LEVEL = 26

# Only these commands may ever be sent. Nothing that reboots, resets, changes
# firmware or erases data is allowed through.
ALLOWED_COMMANDS = {
    CMD_TOGGLE_REALTIME_HR,
    CMD_TOGGLE_GENERIC_HR_PROFILE,
    CMD_SEND_HISTORICAL_DATA,
    CMD_HISTORICAL_DATA_RESULT,
    CMD_GET_BATTERY_LEVEL,
}

# Metadata sub-types inside PT_METADATA / PT_PUFFIN_METADATA. VERIFY
META_HISTORY_START = 1
META_HISTORY_END = 2
META_HISTORY_COMPLETE = 3
