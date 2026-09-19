;===== nozzle load line ===============================
    G29.2 S1 ; ensure z comp turn on
    G90
    M83
    ; FARM CHUTE PRIME v1: prime relocated from the plate lip to the purge chute
    G150.3
    G90
    M109 S225
    M83
    G1 E5 F623.623
    G1 E20 F623.623
    M400
    G150.1
    G90
    G1 Z5 F1200
    G1 Y295 F30000
    G1 Y265 F18000
    G90
    M83
    G29.2 S1 ; ensure z comp turn on
;===== noozle load line end ===========================
