"""Farm auto-eject pipeline services.

Modules:
- ``generator`` — produces the machine-end eject G-code block from a profile.
- ``validator`` — re-checks generated G-code against the profile's safety guards.
- ``dispatch`` — scheduler helper that turns a queue item's eject profile into
  the end-snippet to inject, or an error that fails the item before dispatch.
- ``monitor`` — cooldown-verified plate-clear watcher that releases the
  ``awaiting_plate_clear`` gate once the bed is confirmed cool via MQTT.
"""
