-- WirePlumber 0.4: set this after 50-bluez-config and before 90-enable-all.
-- Keep Bluetooth available without an active local seat.
bluez_monitor.properties = bluez_monitor.properties or {}
bluez_monitor.properties["with-logind"] = false
