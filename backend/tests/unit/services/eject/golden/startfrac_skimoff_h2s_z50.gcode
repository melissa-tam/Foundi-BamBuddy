; ===== FARM EJECT BLOCK profile=tall =====
; --- prologue: re-engage motors, home X/Y (never Z) ---
M17
G28 X Y
G90
G1 Z60 F900
; --- bed heater off ---
M140 S0
; --- cooldown: hold until the bed reaches the release threshold ---
M106 S255
M190 R28
M190 R28
M190 R28
M190 R28
M190 R28
M106 S0
; --- sweep: push part off the front edge ---
G1 X3 Y322 F9000
G1 Z25 F600
G1 X3 F9000
G1 Y-2 F3000
G1 Y322 F9000
G1 X170 F9000
G1 Y-2 F3000
G1 Y322 F9000
G1 X337 F9000
G1 Y-2 F3000
G1 Y322 F9000
G1 Z0.4 F600
G1 X3 F9000
G1 Y-2 F3000
G1 Y322 F9000
G1 X170 F9000
G1 Y-2 F3000
G1 Y322 F9000
G1 X337 F9000
G1 Y-2 F3000
G1 Y322 F9000
G1 X170 Y160 Z10 F9000
; ===== FARM EJECT BLOCK END =====
