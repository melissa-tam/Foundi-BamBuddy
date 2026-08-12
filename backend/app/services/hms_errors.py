"""HMS Error Code Descriptions.

Auto-generated from frontend/src/components/HMSErrorModal.tsx
Source: https://github.com/greghesp/ha-bambulab
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from backend.app.services.hms_catalog import lookup_full_code, lookup_wiki_path

# HMS error code to human-readable description mapping
# Format: "XXXX_YYYY" where XXXX is module code, YYYY is error code
HMS_ERROR_DESCRIPTIONS: dict[str, str] = {
    "0300_4000": "Z axis homing failed; the task has been stopped.",
    "0300_4001": "The printer timed out waiting for the nozzle to cool down before homing.",
    "0300_4002": "Auto Bed Leveling failed; the task has been stopped.",
    "0300_4005": "The hotend cooling fan speed is abnormal.",
    "0300_4006": "The nozzle is clogged.",
    "0300_4008": "The AMS failed to change filament.",
    "0300_4009": "Homing XY axis failed.",
    "0300_400A": "Mechanical resonance frequency identification failed.",
    "0300_400B": "Internal communication exception",
    "0300_400C": "The task was canceled.",
    "0300_400D": "Resume failed after power loss.",
    "0300_400E": "The motor self-check failed.",
    "0300_400F": "The power supply voltage does not match the printer.",
    "0300_4010": "Nozzle offset calibration failed.",
    "0300_4011": "Flow Dynamics Calibration failed; please reinitiate printing or calibration.",
    "0300_4013": "Printing cannot be initiated while AMS is drying.",
    "0300_4014": "Homing Z axis failed: temperature control abnormality.",
    "0300_4015": "Nozzle clumping detection calibration failed. Please go to 'Assistant' for troubleshooting.",
    "0300_4016": "Nozzle cleaning failed. Please click the Assistant for troubleshooting.",
    "0300_401F": "The hotend is not installed, and the toolhead cannot perform homing. Please install the hotend and then continue.",
    "0300_4020": "The nozzle presence detection failed. Please check the Assistant for details.",
    "0300_4021": "Nozzle offset calibration sensor signal abnormality detected. Please check the sensor and retry.",
    "0300_4042": "The Laser Safety Window is not properly installed. The task has been stopped.",
    "0300_4044": "The Flame Sensor is abnormal. The sensor may be short-circuited. Please troubleshoot the issue before starting a print job.",
    "0300_404B": "Task aborted because the front door or top cover is open.",
    "0300_404D": "The current temperature of the hotend, heatbed, or chamber is too high. Please wait for it to cool down to room temperature before restarting the task.",
    "0300_4050": "Liveview Camera calibration timeout; please restart the printer.",
    "0300_4052": "Blade Z-axis homing failed",
    "0300_4057": "Z-axis step loss detected. The task has stopped. Please check if there are any obstructions beneath the heatbed.",
    "0300_4066": "Calibration of motion precision failed.",
    "0300_4067": "Calibration result is over the threshold.",
    "0300_4068": "Step loss occurred during the motion accuracy enhancement process. Please try again.",
    "0300_8000": "Printing was paused for unknown reason. You can select 'Resume' to resume the print job.",
    "0300_8001": "Printing was paused by the user. You can select 'Resume' to continue printing.",
    "0300_8002": "First layer defects were detected by the Micro Lidar. Please check the quality of the printed model before continuing your print.",
    "0300_8003": "Spaghetti defects were detected by the AI Print Monitoring. Please check the quality of the printed model before continuing your print.",
    "0300_8004": "Filament ran out. Please load new filament.",
    "0300_8005": "Toolhead front cover fell off. Please remount the front cover and check to make sure your print is going okay.",
    "0300_8006": "The build plate marker was not detected. Please confirm the build plate is correctly positioned on the heatbed with all four corners aligned, and the marker is visible.",
    "0300_8007": "There was an unfinished print job when the printer lost power. If the model is still adhered to the build plate, you can try resuming the print job.",
    "0300_8008": "Nozzle temperature malfunction",
    "0300_8009": "Heatbed temperature malfunction",
    "0300_800A": "A Filament pile-up was detected by AI Print Monitoring. Please clean filament from the waste chute.",
    "0300_800B": "The cutter is stuck. Please make sure the cutter handle is out and check the filament sensor cable connection.",
    "0300_800C": "Skipped step detected: auto-recover complete; please resume print and check if there are any layer shift problems.",
    "0300_800D": "Detected that the extruder is not extruding normally. If the defects are acceptable, select 'Resume' to resume the print job.",
    "0300_800E": "The print file is not available. Please check to see if the storage media has been removed.",
    "0300_800F": "The door seems to be open, so printing was paused.",
    "0300_8010": "The hotend cooling fan speed is abnormal.",
    "0300_8011": "Detected build plate is not the same as the Gcode file. Please adjust slicer settings or use the correct plate.",
    "0300_8013": "Printing paused due to the pause command added to the printing file.",
    "0300_8014": "The nozzle is covered with filament, or the build plate is installed incorrectly. Please cancel this print and clean the nozzle or adjust the build plate according to the actual status. You can als...",
    "0300_8015": "The filament on external spool has run out; please load new filament. If the filament is loaded, please select 'Resume'.",
    "0300_8016": "The nozzle is clogged with filament. Please cancel this print and clean the nozzle or select 'Resume' to resume the print job.",
    "0300_8017": "Foreign objects detected on heatbed. Please check and clean the heatbed. Then, select 'Resume' to resume the print job.",
    "0300_8018": "Chamber temperature malfunction.",
    "0300_8019": "No build plate is placed.",
    "0300_801A": "Filament extrusion error; please check the assistant for troubleshooting. After resolving the issue, decide whether to cancel or resume the print job based on the actual print status.",
    "0300_801B": "Nozzle temperature problem detected. Refer to Assistant to re-connect the hotend connector. POWER OFF the printer before this operation to avoid short circuits.",
    "0300_801C": "The extrusion resistance is abnormal. The extruder may be clogged; please refer to the assistant. After trouble shooting, you can select 'Resume' to resume the print job.",
    "0300_801D": "The extruder servo motor position sensor is malfunctioning. Please power off the printer first and check if the connection cable is loose.",
    "0300_801E": "The extrusion motor is overloaded, please check the Assistant for details.",
    "0300_8021": "The nozzle may not be installed or not properly installed. Please ensure the nozzle is correctly installed before proceeding.",
    "0300_8022": "The heatbed may be obstructed while moving downward. Please clear any objects beneath the heatbed and check for any resistance or jamming during its movement.",
    "0300_8028": "Nozzle offset calibration sensor error. If using a single hotend or the calibration function is disabled, you may ignore this and continue printing; otherwise, it is recommended to check the sensor...",
    "0300_8041": "Platform detection timeout: please restart the printer.",
    "0300_8042": "Task paused because the door is open.",
    "0300_8043": "The laser module is abnormal.",
    "0300_8044": "Fire was detected inside the chamber.",
    "0300_8045": "Material detection timeout: please restart the printer.",
    "0300_8046": "Foreign object detect timeout: please restart the printer.",
    "0300_8047": "Quick-release lever detection time out: please restart the printer.",
    "0300_8048": "Laser Module unlock has timed out, and the task cannot proceed. Please restart the printer and try again.",
    "0300_8049": "The current plate is invalid.",
    "0300_804A": "Emergency stop button improperly installed. Please reinstall according to the Wiki before proceeding.",
    "0300_804B": "Task paused. The Laser Safety Window is open.",
    "0300_804E": "This is a printing task. Please detach the Laser/Cutting Module from the Toolhead.",
    "0300_804F": "The loading/unloading process is currently ongoing. Please stop the process or remove the laser/cutting module.",
    "0300_8050": "This device does not support the 40W Laser Module. Please remove it or replace it with a 10W Laser Module.",
    "0300_8051": "The cutting module has dropped or the cutting module cable is disconnected; please check the module.",
    "0300_8053": "Laser module detected. Please install the right nozzle correctly to ensure proper Laser Module Mounting Calibration.",
    "0300_8054": "Please place the paper required for Print Then Cut.",
    "0300_8055": "The module mounted on the toolhead does not match the task. Please install the correct module.",
    "0300_8057": "The rotary attachment is disconnected. Please ensure it is properly installed and the cable is securely plugged in.",
    "0300_8058": "The rotary attachment is detected. Please remove it before continuing.",
    "0300_8061": "The mode of Airflow System failed to activate; check the air door condition.",
    "0300_8062": "The chamber temperature is too high. It may be due to high environmental temperature.",
    "0300_8063": "The chamber temperature is too high. Please open the top cover and front door to cool down.",
    "0300_8064": "The chamber temperature is too high. Please open the top cover and front door to cool down. (Open door detection for this print job will be set to 'Notification' level)",
    "0300_8065": "The temperature of the MC module is too high. Please check the Wiki for possible explanations.",
    "0300_8071": "The Toolhead Enhanced Cooling Fan module is malfunctioning.",
    "0300_807D": "Fire Extinguisher not detected, the automatic extinguishing function will be unavailable.",
    "0300_807E": "Fire Extinguisher not detected, the automatic extinguishing function will be unavailable.",
    "0300_807F": "Fire Extinguisher is malfunctioning.",
    "0300_8080": "Fire extinguisher motor reset failed.",
    "0300_8081": "Fire extinguisher cylinder not installed. Please confirm on the extinguisher page.",
    "0300_8082": "The Fire Extinguisher Gas Cylinder is empty.",
    "0300_C012": "Please heat the nozzle to above 170°C.",
    "0300_C056": "A minor fire was detected inside the chamber, and the Auto Fire Extinguishing process has been aborted.",
    "0300_C070": "The fire extinguisher has been detected and is ready for use after the laser module is connected.",
    "0500_4001": "Failed to connect to Bambu Cloud. Please check your network connection.",
    "0500_4002": "Unsupported print file path or name. Please resend the print job.",
    "0500_4003": "Printing stopped because the printer was unable to parse the file. Please resend your print job.",
    "0500_4004": "Device is busy and cannot start new task. Please wait for current task to complete before sending new task.",
    "0500_4005": "Print jobs are not allowed to be sent while updating firmware.",
    "0500_4006": "There is not enough free storage space for the print job. Restoring to factory settings can free up available space.",
    "0500_4007": "The device requires a repair upgrade, and printing is currently unavailable.",
    "0500_4008": "Starting printing failed; please power cycle the printer and resend the print job.",
    "0500_4009": "Print jobs are not allowed to be sent while updating logs.",
    "0500_400A": "The file name is not supported. Please rename and restart the print job.",
    "0500_400B": "There was a problem downloading a file. Please check your network connection and resend the print job.",
    "0500_400C": "Please insert a MicroSD card and restart the print job.",
    "0500_400D": "Please run a self-test and restart the print job.",
    "0500_400E": "Printing was cancelled.",
    "0500_400F": "AMS is initializing and cannot be upgraded at the moment. Please try again later.",
    "0500_4010": "AMS is drying and cannot be upgraded at the moment. Please try again later.",
    "0500_4011": "The printer is loading or unloading filament and cannot be upgraded at the moment. Please try again later.",
    "0500_4012": "The device is printing and cannot be upgraded at the moment. Please try again later.",
    "0500_4013": "AMS is in operation and cannot be upgraded at the moment. Please try again when it is idle.",
    "0500_4014": "Slicing for the print job failed. Please check your settings and restart the print job.",
    "0500_4015": "There is not enough free storage space for the print job. Please format or clear files from the MicroSD card to free up space.",
    "0500_4016": "The MicroSD Card is write-protected. Please replace the MicroSD Card.",
    "0500_4017": "Binding failed. Please retry or restart the printer and retry.",
    "0500_4018": "Binding configuration information parsing failed; please try again.",
    "0500_4019": "The printer has already been bound. Please unbind it and try again.",
    "0500_401A": "Cloud access failed. Possible reasons include network instability caused by interference, inability to access the internet, or router firewall configuration restrictions. You can try moving the pri...",
    "0500_401B": "Cloud response is invalid. If you have tried multiple times and are still failing, please contact customer support.",
    "0500_401C": "Cloud access is rejected. If you have tried multiple times and are still failing, please contact customer support.",
    "0500_401D": "Cloud access failed, which may be caused by network instability due to interference. You can try moving the printer closer to the router before you try again.",
    "0500_401E": "Cloud response is invalid. If you have tried multiple times and are still failing, please contact customer support.",
    "0500_401F": "Authorization timed out. Please make sure that your phone or PC has access to the internet, and ensure that the Bambu Studio/Bambu Handy APP is running in the foreground during the binding operation.",
    "0500_4020": "Cloud access rejected. If you have tried multiple times and are still failing, please contact customer support.",
    "0500_4021": "Cloud access failed, which may be caused by network instability due to interference. You can try moving the printer closer to the router before you try again.",
    "0500_4022": "Cloud response is invalid. If you have tried multiple times and are still failing, please contact customer support.",
    "0500_4023": "Cloud access rejected. If you have tried multiple times and are still failing, please contact customer support.",
    "0500_4024": "Cloud access failed. Possible reasons include network instability caused by interference, inability to access the internet, or router firewall configuration restrictions. You can try moving the pri...",
    "0500_4025": "Cloud response is invalid. If you have tried multiple times and are still failing, please contact customer support.",
    "0500_4026": "Cloud access rejected. If you have tried multiple times and are still failing, please contact customer support.",
    "0500_4027": "Cloud access failed; this may be caused by network instability due to interference. You can try moving the printer closer to the router before you try again.",
    "0500_4028": "Cloud response is invalid. If you have tried multiple times and are still failing, please contact customer support.",
    "0500_4029": "Cloud access is rejected. If you have tried multiple times and are still failing, please contact customer support.",
    "0500_402A": "Failed to connect to the router, which may be caused by wireless interference or being too far away from the router. Please try again or move the printer closer to the router and try again.",
    "0500_402B": "Router connection failed due to incorrect password. Please check the password and try again.",
    "0500_402C": "Failed to obtain IP address, which may be caused by wireless interference resulting in data transmission failure or the DHCP address pool of the router being full. Please move the printer closer to...",
    "0500_402D": "System exception",
    "0500_402E": "System does not support the file system currently used by the USB flash drive. Please replace or format the USB flash drive to FAT32.",
    "0500_402F": "The MicroSD card sector data is damaged. Please use the SD card repair tool to repair or format it. If it still cannot be identified, please replace the MicroSD card.",
    "0500_4030": "The device is currently upgrading. Please try again when it is idle.",
    "0500_4031": "The accessory firmware does not match the printer. Please update it on the 'Firmware' page.",
    "0500_4033": "The AMS firmware does not match the printer. Please update it on the 'Firmware' page.",
    "0500_4034": "The Laser Module firmware does not match the printer. Please update it on the 'Firmware' page.",
    "0500_4035": "The BirdsEye Camera is malfunctioning. Please try restarting the device. If the issue persists after multiple restarts, check the camera connection status or contact customer support.",
    "0500_4037": "Your sliced file is not compatible with current printer model. This file can't be printed on this printer.",
    "0500_4038": "The nozzle diameter in sliced file is not consistent with the current nozzle setting. This file can't be printed.",
    "0500_4039": "The current task does not allow the installation of the laser/cutting module, and the task has been halted.",
    "0500_403A": "The current temperature is too low. In order to protect you and your printer, printing tasks, moving an axis and other operations are disabled. Please move the printer to an environment above 10 de...",
    "0500_403B": "Laser/cutting tasks cannot be initiated on the machine at the moment. Please use the computer software to start the task.",
    "0500_403C": "The current nozzle setting does not match the slicing file. Continuing to print may affect print quality. It is recommended to re-slice before starting the print.",
    "0500_403D": "The toolhead module is not set up. Please set it up before initiating the task.",
    "0500_403E": "The current tool head does not support initialization.",
    "0500_403F": "Failed to download print job; please check your network connection.",
    "0500_4040": "The printer has reached its power limit. Please connect a dedicated power adapter to this AMS to enable drying.",
    "0500_4041": "The AMS drying cannot be started during printing.",
    "0500_4042": "Due to power limitations, starting AMS drying will pause current operations such as nozzle heating and fan running. Do you want to proceed with drying?",
    "0500_4043": "Due to power limitations, only one AMS is allowed to use the device's power for drying.",
    "0500_4044": "BirdsEye Camera malfunction: please contact customer support.",
    "0500_4045": "Hotend check in progress. This operation is temporarily unavailable. Please wait.",
    "0500_4050": "Error detected on the print board.",
    "0500_4052": "Error detected on the hot end.",
    "0500_4054": "Error detected on the mat.",
    "0500_405D": "Laser module Serial Number error: unable to calibrate or make project.",
    "0500_4065": "The task requires a Laser Platform, but the current one is a Cutting Platform. Please replace it, measure the material thickness in the software, and then restart the task.",
    "0500_4070": "The laser or cutter module is connected, so the device cannot initiate a 3D printing task.",
    "0500_4075": "No Laser Platform was detected, which may affect thickness measurement accuracy. Please place the laser platform correctly and ensure the rear markers are not blocked, then restart the thickness me...",
    "0500_4076": "Please place the Laser Platform correctly and ensure the rear markers are not blocked, then restart the thickness measurement in the software before initiating the task.",
    "0500_4097": "The device cannot detect the Laser Module. Please reconnect the module cable or restart the printer.",
    "0500_4098": "The device cannot detect AMS A. Please reconnect the AMS cable or restart the printer.",
    "0500_4099": "The firmware of Cutting Module does not match the printer; the device cannot continue working. Please update it on the 'Firmware' page.",
    "0500_409A": "The firmware of the Air Pump does not match the printer; the device cannot continue working. Please update it on the 'Firmware' page.",
    "0500_409B": "The firmware of the Laser Module does not match the printer; the device cannot continue working. Please update it on the 'Firmware' page.",
    "0500_409D": "The firmware of AMS A does not match the printer; the device cannot continue working. Please upgrade it on the 'Firmware' page.",
    "0500_409E": "The device cannot detect the Cutting Module. Please reconnect the module cable or restart the printer.",
    "0500_409F": "The device cannot detect the Air Pump.  Please reconnect the module cable or restart the printer.",
    "0500_40A0": "The Rotary Attachment module is not detected. Please reconnect the cable or restart the printer.",
    "0500_40A1": "The Auto Fire Extinguishing System is not detected.  Please reconnect the module cable or restart the printer.",
    "0500_40A3": "AMS(or AMS lite) A communication is abnormal. Please reconnect the module cable or restart the printer.",
    "0500_40A4": "The current firmware only supports 1 AMS Lite. Please remove all AMS units before reconnecting the supported AMS Lite device.",
    "0500_40A5": "The current firmware only supports AMS/AMS 2 Pro/AMS HT, with a maximum of 4 units. Please remove all AMS units before reconnecting the supported one.",
    "0500_8013": "The print file is not available. Please check to see if the storage media has been removed.",
    "0500_8036": "Your sliced file is not consistent with the current printer model. Continue?",
    "0500_803C": "The current nozzle setting does not match the slicing file. Continuing to print may affect print quality. It is recommended to re-slice before starting the print.",
    "0500_8040": "Toolhead front cover is detached. Moving the toolhead may damage the printer. Do you want to continue?",
    "0500_8041": "The filament in hotend is too cold. Extrusion may damage the extruder. Still feeding in/out the filament?",
    "0500_8048": "The module on the toolhead is not calibrated. Please cancel the task to perform calibration or switch to a calibrated module.",
    "0500_8051": "Detected build plate is not the same as the Gcode file. Please adjust slicer settings or use the correct plate.",
    "0500_8053": "Nozzle mismatch was detected during printing. Please initiate the print after re-slicing, or continue printing after replacing with the correct nozzle. Caution: the hotend temperature is high.",
    "0500_8055": "Laser module is installed, but a Cutting Platform is detected. Please place a Laser Platform and perform laser calibration.",
    "0500_8056": "Cutting module is installed, but the laser platform is detected. Please place the cutting platform for calibration.",
    "0500_8058": "Please place the light grip cutting mat correctly and ensure the marker is exposed.",
    "0500_8059": "Cutting platform base is not correctly aligned. Please ensure that the four corners of the platform are aligned with the heatbed.",
    "0500_805A": "Please place the cutting mat on cutting protection base.",
    "0500_805B": "The cutting mat type is unknown; please replace it with the correct cutting mat.",
    "0500_805C": "The grip cutting mat type does not match; please place a LightGrip cutting mat.",
    "0500_805E": "Cutting module Serial Number error: unable to calibrate or make project.",
    "0500_8060": "The current module on toolhead does not meet requirements. Please replace the module as per the on-screen instructions.",
    "0500_8061": "No print plate detected. Please make sure it is placed correctly.",
    "0500_8062": "The print plate marker was not detected. Please confirm the print plate is correctly positioned on the heatbed with all four corners aligned, and the marker is visible. If strong light is shining o...",
    "0500_8063": "The platform is not detected during calibration; please make sure the Laser Platform is properly placed.",
    "0500_8064": "Please place the Laser Platform correctly and ensure the rear markers are not blocked for laser calibration.",
    "0500_8066": "The task requires a Cutting Platform, but the current one is a Laser Platform. Please replace it with a Cutting Platform (Cutting Protection Base + LightGrip cutting mat).",
    "0500_8067": "Please place a LightGrip cutting mat on the cutting protection base.",
    "0500_8068": "Please place the strong grip cutting mat correctly and ensure the marker is exposed.",
    "0500_8069": "Unable to recognize the left and right hotends. They might be third party hotends, or the hotend marks may be dirty. Please manually set the hotend types.",
    "0500_806A": "Unable to recognize the left and right hotends. They might be third party hotends, or the hotend marks may be dirty. Please set hotend types on printer screen before next print.",
    "0500_806B": "Quick-release Lever is not locked. Please press down the external toolhead module to ensure it is properly seated, then push down the level to lock it in place.",
    "0500_806C": "Please place the cutting platform correctly and ensure the marker is exposed.",
    "0500_806D": "Material not detected. Please confirm placement and continue.",
    "0500_806E": "Foreign objects detected on heatbed; please check and clean up the heatbed.",
    "0500_806F": "The grip cutting mat type does not match; please place a StrongGrip cutting mat.",
    "0500_8071": "No cutting platform was detected. Please confirm that it has been correctly placed.",
    "0500_8072": "Live View camera is blocked",
    "0500_8073": "Heatbed limit block is obstructed or contaminated. Please clean and ensure the limit block is visible, otherwise platform position offset detection may be inaccurate.",
    "0500_8074": "The Laser Platform is offset. Please ensure that the four corners of the platform are aligned with the heatbed, and the marker is not obstructed.",
    "0500_8077": "The visual marker was not detected. Please ensure the paper is properly placed.",
    "0500_8078": "Current material does not match the sliced file settings. Please load the correct material and ensure the QR code on the material is not damaged or dirty.",
    "0500_8079": "Please place the Laser Test Material (350g paperboard) and position support strips underneath to prevent material warping.",
    "0500_807A": "The foreign object detection function is not working. You can continue the task or check the assistant for troubleshooting.",
    "0500_807B": "Please place the cutting platform (cutting protection base + LightGrip cutting mat).",
    "0500_807C": "Please place the cutting platform (cutting protection base + StrongGrip cutting mat).",
    "0500_807D": "This task requires a Cutting Platform, but the current one is a Laser Platform. Please replace it with a Cutting Platform (Cutting Protection Base + StrongGrip Cutting Mat).",
    "0500_807E": "Please place a StrongGrip cutting mat on the cutting protection base.",
    "0500_8080": "The left and right hotends are not installed.",
    "0500_8081": "The left and right hotends are not installed.",
    "0500_8082": "Please remove the protective film on the Opaque Glossy Acrylic before processing",
    "0500_8083": "Material is not allowed in Mounting Calibration. Please remove the material from the platform.",
    "0500_8084": "The Live View Camera is dirty; please clean it and continue.",
    "0500_8085": "Toolhead camera is obstructed",
    "0500_8086": "Toolhead Camera is dirty, which affects the AI function; please clean the lens surface.",
    "0500_8087": "BirdsEye camera is obstructed",
    "0500_8088": "The Birdseye Camera is dirty",
    "0500_8089": "Task paused due to Presence Check failed. Please check the printer to continue.",
    "0500_808A": "The BirdsEye Camera is installed offset. Please refer to the assistant to reinstall it.",
    "0500_808B": "The BirdsEye Camera setup failed. Please remove all objects and the mat on the heatbed to ensure the heatbed markers are visible. Meanwhile, please ensure the BirdsEye Camera is installed correctly...",
    "0500_808C": "Detected build plate offset. Please align the build plate with the heatbed, and then continue.",
    "0500_808D": "The Cutting Module offset calibration failed, which may result in inaccurate cuts. Please ensure the cutting material is properly positioned and check whether the cutting blade tip is worn.",
    "0500_808E": "BirdsEye Camera initialization failed. The toolhead camera did not detect the Heatbed features. Please clean the Heatbed, remove all objects and pads, and ensure the bed markings are visible. Check...",
    "0500_808F": "Nozzle camera lens is dirty, affecting AI monitoring. Clean the lens with a non-woven cloth and a small amount of alcohol. Beware of hotend heat; wait for it to cool before handling.",
    "0500_8090": "Please attach the 80g White Printing Paper to the center area of the platform.",
    "0500_8091": "The Cutting Module offset calibration failed, which may result in inaccurate cuts. Please ensure the 80g white printer paper(letter paper thickness) is properly positioned and check whether the cut...",
    "0500_8092": "Toolhead Camera initialization failed. This print can still continue, but some AI functions will be disabled. If you encounter this issue again after restarting, please contact customer support.",
    "0500_8093": "The nozzle silicone sleeve is not installed; there is a risk of temperature control failure. Please install it correctly and try again.",
    "0500_80A0": "The visual encoder board was not detected. Please check if the board is properly placed and aligned at all four corners, and ensure the positioning markings are clear and free from wear.",
    "0500_C010": "MicroSD Card read/write exception: please reinsert or replace the MicroSD Card.",
    "0500_C032": "Laser/Cutting module connected to the toolhead. The drying process has been automatically stopped.",
    "0500_C036": "This is a printing task. Please detach the Laser/Cutting Module from the Toolhead.",
    "0500_C07F": "Device is busy and cannot perform this operation. To proceed, please pause or stop the current task.",
    "0501_4017": "Binding failed. Please retry or restart the printer and retry.",
    "0501_4018": "Binding configuration information parsing failed; please try again.",
    "0501_4019": "The printer has already been bound. Please unbind it and try again.",
    "0501_401A": "Cloud access failed. Possible reasons include network instability caused by interference, inability to access the internet, or router firewall configuration restrictions. You can try moving the pri...",
    "0501_401B": "Cloud response is invalid. If you have tried multiple times and are still failing, please contact customer support.",
    "0501_401C": "Cloud access is rejected. If you have tried multiple times and are still failing, please contact customer support.",
    "0501_401D": "Cloud access failed, which may be caused by network instability due to interference. You can try moving the printer closer to the router before you try again.",
    "0501_401E": "Cloud response is invalid. If you have tried multiple times and are still failing, please contact customer support.",
    "0501_401F": "Authorization timed out. Please make sure that your phone or PC has access to the internet, and ensure that the Bambu Studio/Bambu Handy APP is running in the foreground during the binding operation.",
    "0501_4020": "Cloud access rejected. If you have tried multiple times and are still failing, please contact customer support.",
    "0501_4021": "Cloud access failed, which may be caused by network instability due to interference. You can try moving the printer closer to the router before you try again.",
    "0501_4022": "Cloud response is invalid. If you have tried multiple times and are still failing, please contact customer support.",
    "0501_4023": "Cloud access rejected. If you have tried multiple times and are still failing, please contact customer support.",
    "0501_4024": "Cloud access failed. Possible reasons include network instability caused by interference, inability to access the internet, or router firewall configuration restrictions. You can try moving the pri...",
    "0501_4025": "Cloud response is invalid. If you have tried multiple times and are still failing, please contact customer support.",
    "0501_4026": "Cloud access rejected. If you have tried multiple times and are still failing, please contact customer support.",
    "0501_4027": "Cloud access failed; this may be caused by network instability due to interference. You can try moving the printer closer to the router before you try again.",
    "0501_4028": "Cloud response is invalid. If you have tried multiple times and are still failing, please contact customer support.",
    "0501_4029": "Cloud access is rejected. If you have tried multiple times and are still failing, please contact customer support.",
    "0501_4031": "Device discovery binding is in progress, and the QR code cannot be displayed on the screen. You can wait for the binding to finish or abort the device discovery binding process in the APP/Studio an...",
    "0501_4032": "QR code binding is in progress, so device discovery binding cannot be performed. You can scan the QR code on the screen for binding or exit the QR code display page on screen and try device discove...",
    "0501_4033": "Your APP region does not match with your printer; please download the APP in the corresponding region and register your account again.",
    "0501_4034": "The slicing progress has not been updated for a long time, and the printing task has exited. Please confirm the parameters and reinitiate printing.",
    "0501_4035": "The device is in the process of binding and cannot respond to new binding requests.",
    "0501_4038": "The regional settings do not match the printer; please check the printer's regional settings.",
    "0501_4039": "Device login has expired; please try to bind again.",
    "0501_4098": "The device cannot detect AMS B. Please reconnect the AMS cable or restart the printer.",
    "0501_409D": "The firmware of AMS B does not match the printer; the device cannot continue working. Please update it on the 'Firmware' page.",
    "0501_40A3": "AMS(or AMS lite) B communication is abnormal. Please reconnect the module cable or restart the printer.",
    "0502_4001": "Current filament will be used in this print job. Settings cannot be changed.",
    "0502_4002": "Please go to “Settings > Calibration” to run the Motion Accuracy Enhancement Calibration before turning on Motion Accuracy Enhancement mode.",
    "0502_4003": "The printer is currently printing and the motion accuracy enhancement feature cannot be turned on or off.",
    "0502_4004": "Some features are not supported by the current device. Please check the Studio feature settings or update the firmware to the latest version.",
    "0502_4005": "The AMS has not been calibrated yet, so printing cannot be initiated.",
    "0502_4006": "Unknown module detected; please try updating the firmware to the latest version.",
    "0502_400D": "Failed to start a new task: filament loading/unloading not completed.",
    "0502_400E": "Failed to start a new task: The nozzle cold pull was not completed.",
    "0502_4013": "This device is not compatible with the 40W laser module. Please replace it with a 10W laser module or remove it.",
    "0502_4098": "The device cannot detect AMS C. Please reconnect the AMS cable or restart the printer.",
    "0502_409D": "The firmware of AMS C does not match the printer; the device cannot continue working. Please upgrade it on the 'Firmware' page.",
    "0502_40A3": "AMS(or AMS lite) C communication is abnormal. Please reconnect the module cable or restart the printer.",
    "0502_C00F": "The device is busy and cannot perform nozzle identification.",
    "0502_C010": "Due to printer power limitations, printing, calibration, controls and other actions cannot be performed during AMS drying. Please stop the drying process before proceeding with any other operation.",
    "0502_C011": "Currently in 2D production mode. Please continue the operation on the printer",
    "0502_C012": "The task cannot be paused.",
    "0502_C014": "The AMS Remaining Filament Estimation is enabled by default and cannot be disabled.",
    "0502_C024": "The flow dynamic calibration records have exceeded the storage limit. Please delete some historical records in the slicer software before adding new calibration data.",
    "0503_4098": "The device cannot detect AMS D. Please reconnect the AMS cable or restart the printer.",
    "0503_409D": "The firmware of AMS D does not match the printer; the device cannot continue working. Please update it on the 'Firmware' page.",
    "0503_40A3": "AMS(or AMS lite) D communication is abnormal. Please reconnect the module cable or restart the printer.",
    "0580_4096": "The device cannot detect AMS-HT A. Please reconnect the AMS-HT cable or restart the printer.",
    "0580_409C": "The firmware of AMS-HT A does not match the printer; the device cannot continue working. Please update it on the 'Firmware' page.",
    "0580_40A2": "AMS-HT A communication is abnormal. Please reconnect the module cable or restart the printer.",
    "0581_4096": "The device cannot detect AMS-HT B. Please reconnect the AMS-HT cable or restart the printer.",
    "0581_409C": "The firmware of AMS-HT B does not match the printer; the device cannot continue working. Please update it on the 'Firmware' page.",
    "0581_40A2": "AMS-HT B communication is abnormal. Please reconnect the module cable or restart the printer.",
    "0582_4096": "The device cannot detect AMS-HT C. Please reconnect the AMS-HT cable or restart the printer.",
    "0582_409C": "The firmware of AMS-HT C does not match the printer; the device cannot continue working. Please update it on the 'Firmware' page.",
    "0582_40A2": "AMS-HT C communication is abnormal. Please reconnect the module cable or restart the printer.",
    "0583_4096": "The device cannot detect AMS-HT D. Please reconnect the AMS-HT cable or restart the printer.",
    "0583_409C": "The firmware of AMS-HT D does not match the printer; the device cannot continue working. Please update it on the 'Firmware' page.",
    "0583_40A2": "AMS-HT D communication is abnormal. Please reconnect the module cable or restart the printer.",
    "0584_4096": "The device cannot detect AMS-HT F. Please reconnect the AMS-HT cable or restart the printer.",
    "0584_409C": "The firmware of AMS-HT E does not match the printer; the device cannot continue working. Please update it on the 'Firmware' page.",
    "0584_40A2": "AMS-HT E communication is abnormal. Please reconnect the module cable or restart the printer.",
    "0585_4096": "The device cannot detect AMS-HT E. Please reconnect the AMS-HT cable or restart the printer.",
    "0585_409C": "The firmware of AMS-HT F does not match the printer; the device cannot continue working. Please update it on the 'Firmware' page.",
    "0585_40A2": "AMS-HT F communication is abnormal. Please reconnect the module cable or restart the printer.",
    "0586_4096": "The device cannot detect AMS-HT G. Please reconnect the AMS-HT cable or restart the printer.",
    "0586_409C": "The firmware of AMS-HT G does not match the printer; the device cannot continue working. Please update it on the 'Firmware' page.",
    "0586_40A2": "AMS-HT G communication is abnormal. Please reconnect the module cable or restart the printer.",
    "0587_4096": "The device cannot detect AMS-HT H. Please reconnect the AMS-HT cable or restart the printer.",
    "0587_409C": "The firmware of AMS-HT H does not match the printer; the device cannot continue working. Please upgrade it on the 'Firmware' page.",
    "0587_40A2": "AMS-HT H communication is abnormal. Please reconnect the module cable or restart the printer.",
    "05FE_8053": "The left nozzle is not matched with slicing file. Please initiate the print after re-slicing, or continue printing after replacing with the correct nozzle. Caution: the hotend temperature is high.",
    "05FE_8069": "Unable to recognize the left hotend. It might be a third party hotend, or the hotend mark may be dirty. Please manually set the hotend type.",
    "05FE_806A": "Unable to recognize the left hotend. It might be a third party hotend, or the hotend mark may be dirty. Please set hotend type on printer screen before next print.",
    "05FE_8080": "The left hotend is not installed.",
    "05FE_8081": "The left hotend is not installed.",
    "05FF_8053": "The right nozzle is not matched with slicing file. Please initiate the print after re-slicing, or continue printing after replacing with the correct nozzle. Caution: the hotend temperature is high.",
    "05FF_8069": "Unable to recognize the right hotend. It might be a third party hotend, or the hotend mark may be dirty. Please manually set the hotend type.",
    "05FF_806A": "Unable to recognize the right hotend. It might be a third party hotend, or the hotend mark may be dirty. Please set hotend type on printer screen before next print.",
    "05FF_8080": "The right hotend is not installed.",
    "05FF_8081": "The right hotend is not installed.",
    "0700_4001": "The AMS has been disabled for a print, but it still has filament loaded. Please unload the AMS filament and switch to the spool holder filament for printing.",
    "0700_4025": "Failed to read the filament information.",
    "0700_8001": "Failed to cut the filament. Please check the cutter.",
    "0700_8002": "The cutter is stuck. Please make sure the cutter handle is out.",
    "0700_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "0700_8004": "AMS failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "0700_8005": "The AMS failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "0700_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "0700_8007": "Extruding filament failed. The extruder might be clogged.",
    "0700_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS A to the extruder is properly connected.",
    "0700_8010": "The AMS assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "0700_8011": "AMS filament ran out. Please insert a new filament into the same AMS slot.",
    "0700_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "0700_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "0700_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "0700_8017": "AMS A is drying. Please stop drying process before loading/unloading material.",
    "0700_8021": "AMS setup failed; please refer to the assistant.",
    "0700_8023": "AMS A cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "0700_C069": "An error occurred during AMS A drying. Please go to Assistant for more details.",
    "0700_C06A": "AMS A is reading RFID. Unable to start drying. Please try again later.",
    "0700_C06B": "AMS A is changing filament. Unable to start drying. Please try again later.",
    "0700_C06C": "AMS A is in Feed Assist Mode. Unable to start drying. Please try again later.",
    "0700_C06D": "AMS A is assisting in filament insertion. Unable to start drying. Please try again later.",
    "0700_C06E": "AMS A motor is performing self-test. Unable to start drying. Please try again later.",
    "0701_4001": "Filament is still loaded from the AMS after it has been disabled. Please unload the filament, load from the spool holder, and restart printing.",
    "0701_4025": "Failed to read the filament information.",
    "0701_8001": "Failed to cut the filament. Please check the cutter.",
    "0701_8002": "The cutter is stuck. Please make sure the cutter handle is out.",
    "0701_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "0701_8004": "AMS failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "0701_8005": "The AMS failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "0701_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "0701_8007": "Extruding filament failed. The extruder might be clogged.",
    "0701_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS B to the extruder is properly connected.",
    "0701_8010": "The AMS assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "0701_8011": "AMS filament ran out. Please insert a new filament into the same AMS slot.",
    "0701_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "0701_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "0701_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "0701_8017": "AMS B is drying. Please stop drying process before loading/unloading material.",
    "0701_8021": "AMS setup failed; please refer to the assistant.",
    "0701_8023": "AMS B cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "0701_C069": "An error occurred during AMS B drying. Please go to Assistant for more details.",
    "0701_C06A": "AMS B is reading RFID. Unable to start drying. Please try again later.",
    "0701_C06B": "AMS B is changing filament. Unable to start drying. Please try again later.",
    "0701_C06C": "AMS B is in Feed Assist Mode. Unable to start drying. Please try again later.",
    "0701_C06D": "AMS B is assisting in filament insertion. Unable to start drying. Please try again later.",
    "0701_C06E": "AMS B motor is performing self-test. Unable to start drying. Please try again later.",
    "0702_4001": "Filament is still loaded from the AMS after it has been disabled. Please unload the filament, load from the spool holder, and restart printing.",
    "0702_4025": "Failed to read the filament information.",
    "0702_8001": "Failed to cut the filament. Please check the cutter.",
    "0702_8002": "The cutter is stuck. Please make sure the cutter handle is out.",
    "0702_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "0702_8004": "AMS failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "0702_8005": "The AMS failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "0702_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "0702_8007": "Extruding filament failed. The extruder might be clogged.",
    "0702_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS C to the extruder is properly connected.",
    "0702_8010": "The AMS assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "0702_8011": "AMS filament ran out. Please insert a new filament into the same AMS slot.",
    "0702_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "0702_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "0702_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "0702_8017": "AMS C is drying. Please stop drying process before loading/unloading material.",
    "0702_8021": "AMS setup failed; please refer to the assistant.",
    "0702_8023": "AMS C cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "0702_C069": "An error occurred during AMS C drying. Please go to Assistant for more details.",
    "0702_C06A": "AMS C is reading RFID. Unable to start drying. Please try again later.",
    "0702_C06B": "AMS C is changing filament. Unable to start drying. Please try again later.",
    "0702_C06C": "AMS C is in Feed Assist Mode. Unable to start drying. Please try again later.",
    "0702_C06D": "AMS C is assisting in filament insertion. Unable to start drying. Please try again later.",
    "0702_C06E": "AMS C motor is performing self-test. Unable to start drying. Please try again later.",
    "0703_4001": "Filament is still loaded from the AMS after it has been disabled. Please unload the filament, load from the spool holder, and restart printing.",
    "0703_4025": "Failed to read the filament information.",
    "0703_8001": "Failed to cut the filament. Please check the cutter.",
    "0703_8002": "The cutter is stuck. Please make sure the cutter handle is out.",
    "0703_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "0703_8004": "AMS failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "0703_8005": "The AMS failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "0703_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "0703_8007": "Extruding filament failed. The extruder might be clogged.",
    "0703_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS D to the extruder is properly connected.",
    "0703_8010": "The AMS assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "0703_8011": "AMS filament ran out. Please insert a new filament into the same AMS slot.",
    "0703_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "0703_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "0703_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "0703_8017": "AMS D is drying. Please stop drying process before loading/unloading material.",
    "0703_8021": "AMS setup failed; please refer to the assistant.",
    "0703_8023": "AMS D cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "0703_C069": "An error occurred during AMS D drying. Please go to Assistant for more details.",
    "0703_C06A": "AMS D is reading RFID. Unable to start drying. Please try again later.",
    "0703_C06B": "AMS D is changing filament. Unable to start drying. Please try again later.",
    "0703_C06C": "AMS D is in Feed Assist Mode. Unable to start drying. Please try again later.",
    "0703_C06D": "AMS D is assisting in filament insertion. Unable to start drying. Please try again later.",
    "0703_C06E": "AMS D motor is performing self-test. Unable to start drying. Please try again later.",
    "0704_4025": "Failed to read the filament information.",
    "0704_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "0704_8004": "AMS failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "0704_8005": "The AMS failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "0704_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "0704_8007": "Extruding filament failed. The extruder might be clogged.",
    "0704_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS E to the extruder is properly connected.",
    "0704_8010": "The AMS assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "0704_8011": "AMS filament ran out. Please insert a new filament into the same AMS slot.",
    "0704_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "0704_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "0704_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "0704_8021": "AMS setup failed; please refer to the assistant.",
    "0704_8023": "AMS E cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "0705_4025": "Failed to read the filament information.",
    "0705_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "0705_8004": "AMS failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "0705_8005": "The AMS failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "0705_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "0705_8007": "Extruding filament failed. The extruder might be clogged.",
    "0705_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS F to the extruder is properly connected.",
    "0705_8010": "The AMS assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "0705_8011": "AMS filament ran out. Please insert a new filament into the same AMS slot.",
    "0705_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "0705_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "0705_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "0705_8021": "AMS setup failed; please refer to the assistant.",
    "0705_8023": "AMS F cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "0706_4025": "Failed to read the filament information.",
    "0706_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "0706_8004": "AMS failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "0706_8005": "The AMS failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "0706_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "0706_8007": "Extruding filament failed. The extruder might be clogged.",
    "0706_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS G to the extruder is properly connected.",
    "0706_8010": "The AMS assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "0706_8011": "AMS filament ran out. Please insert a new filament into the same AMS slot.",
    "0706_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "0706_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "0706_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "0706_8021": "AMS setup failed; please refer to the assistant.",
    "0706_8023": "AMS G cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "0707_4025": "Failed to read the filament information.",
    "0707_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "0707_8004": "AMS failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "0707_8005": "The AMS failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "0707_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "0707_8007": "Extruding filament failed. The extruder might be clogged.",
    "0707_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS H to the extruder is properly connected.",
    "0707_8010": "The AMS assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "0707_8011": "AMS filament ran out. Please insert a new filament into the same AMS slot.",
    "0707_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "0707_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "0707_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "0707_8021": "AMS setup failed; please refer to the assistant.",
    "0707_8023": "AMS H cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "07FE_8001": "Failed to cut the filament of the left extruder. Please check the cutter.",
    "07FE_8002": "The cutter of the left extruder is stuck. Please pull out the cutter handle.",
    "07FE_8003": "Please pull out the filament on the spool holder  of the left extruder. If this message persists, please check to see if there is filament broken in the extruder. (Connect a PTFE tube if you are ab...",
    "07FE_8004": "Failed to pull back the filament from the left extruder. Please check whether the filament is stuck inside the extruder.",
    "07FE_8005": "Failed to feed the filament outside the AMS. Please clip the end of the filament flat and check to see if the spool is stuck.",
    "07FE_8006": "Please feed filament into the PTFE tube of the left extruder until it can not be pushed any farther.",
    "07FE_8007": "Please observe the nozzle of the left extruder. If the filament has been extruded, select 'Continue'; if it has not, please push the filament forward slightly, and then select 'Retry'.",
    "07FE_8010": "Check if the left external filament spool or filament is stuck.",
    "07FE_8011": "The external filament connected to the left extruder has run out; please load a new filament.",
    "07FE_8012": "Failed to get mapping table; please select 'Resume' to retry.",
    "07FE_8013": "Timeout purging old filament of the left extruder: Please check if the filament is stuck or the extruder is clogged.",
    "07FE_8020": "Extruder change failed; please refer to the assistant.",
    "07FE_8021": "AMS setup failed; please refer to the assistant.",
    "07FE_8024": "Extruder position calibration failed; please refer to the assistant.",
    "07FE_8025": "Cold pull timed out. Please promptly operate or check whether the filament is broken inside the extruder, and click the Assistant for details.",
    "07FE_8030": "The filament specified in the slicer has been used up. Printing is paused. Please go to the machine to replace the material and resume printing.",
    "07FE_C003": "Please pull out the filament on the spool holder of the left extruder. If this message persists, please check to see if there is filament broken in the extruder or PTFE tube. (Connect a PTFE tube i...",
    "07FE_C006": "Please feed filament into the PTFE tube of the left extruder until it can not be pushed any farther.",
    "07FE_C008": "Please pull out the filament on the spool holder of the left extruder. If this message persists, please check to see if there is filament broken in the extruder or PTFE tube. (Connect a PTFE tube i...",
    "07FE_C009": "Please feed filament into the PTFE tube of the left extruder until it can not be pushed any farther.",
    "07FE_C00A": "Please observe the nozzle of the left extruder. If the filament has been extruded, select 'Continue'; if not, please push the filament forward slightly and then select 'Retry'.",
    "07FE_C010": "Insert the filament (over 30cm long) until it stops. You might see slight smoke during flushing. After insertion, close the front door and top cover.",
    "07FE_C011": "Please manually and slowly pull out the filament from the extruder. Then click “Continue”.",
    "07FE_C012": "Press the black PTFE tube coupler and unplug the PTFE tube. After completing the operation, click 'Continue.'",
    "07FF_4001": "Filament is still loaded from the AMS after it has been disabled. Please unload the filament, load from the spool holder, and restart printing.",
    "07FF_8001": "Failed to cut the filament of the right extruder. Please check the cutter.",
    "07FF_8002": "The cutter is stuck. Please make sure the cutter handle is out.",
    "07FF_8003": "Please pull out the filament on the spool holder  of the right extruder. If this message persists, please check to see if there is filament broken in the extruder. (Connect a PTFE tube if you are a...",
    "07FF_8004": "Failed to pull back the filament from the right extruder. Please check whether the filament is stuck inside the extruder.",
    "07FF_8005": "Failed to feed the filament outside the AMS. Please clip the end of the filament flat and check to see if the spool is stuck.",
    "07FF_8006": "Please feed filament into the PTFE tube of the right extruder until it can not be pushed any farther.",
    "07FF_8007": "Please observe the nozzle of the right extruder. If the filament has been extruded, select 'Continue'; if it has not, please push the filament forward slightly, and then select 'Retry'.",
    "07FF_8010": "Check if the external filament spool or filament is stuck.",
    "07FF_8011": "External filament has run out; please load a new filament.",
    "07FF_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "07FF_8013": "Timeout purging old filament of the right extruder: Please check if the filament is stuck or the extruder is clogged.",
    "07FF_8020": "Extruder change failed; please refer to the assistant.",
    "07FF_8021": "AMS setup failed; please refer to the assistant.",
    "07FF_8024": "Extruder position calibration failed; please refer to the assistant.",
    "07FF_8025": "Cold pull timed out. Please promptly operate or check whether the filament is broken inside the extruder, and click the Assistant for details.",
    "07FF_8030": "The filament specified in the slicer has been used up. Printing is paused. Please go to the machine to replace the material and resume printing.",
    "07FF_C003": "Please pull out the filament on the spool holder of the right extruder. If this message persists, please check to see if there is filament broken in the extruder or PTFE tube. (Connect a PTFE tube ...",
    "07FF_C006": "Please feed filament into the PTFE tube of the right extruder until it can not be pushed any farther.",
    "07FF_C008": "Please pull out the filament on the spool holder of the right extruder. If this message persists, please check to see if there is filament broken in the extruder or PTFE tube. (Connect a PTFE tube ...",
    "07FF_C009": "Please feed filament into the PTFE tube of the right extruder until it can not be pushed any farther.",
    "07FF_C00A": "Please observe the nozzle of the right extruder. If the filament has been extruded, select 'Continue'; if not, please push the filament forward slightly and then select 'Retry'.",
    "07FF_C010": "Insert the filament (over 30cm long) until it stops. You might see slight smoke during flushing. After insertion, close the front door and top cover.",
    "07FF_C011": "Hold the driven wheel bracket, slowly pull the filament from the extruder, then press 'Continue'.",
    "07FF_C012": "Press the black PTFE tube coupler and unplug the PTFE tube. After completing the operation, click 'Continue.'",
    "0C00_4020": "The setup of BirdsEye Camera failed. Please clear all objects and remove the mat. Make sure the marker is not obstructed. Meanwhile, clean both the BirdsEye Camera and Toolhead Camera, and remove a...",
    "0C00_4021": "The setup of BirdsEye Camera failed; please reboot the printer.",
    "0C00_4022": "The setup of BirdsEye Camera failed.  Please check if the laser module is working properly.",
    "0C00_4024": "The Birdseye Camera is installed offset. Please refer to the assistant to reinstall it.",
    "0C00_4025": "The Birdseye Camera is dirty. Please clean it and restart the process.",
    "0C00_4026": "The Live View Camera initialization failed; please reboot the printer.",
    "0C00_4027": "The Live View Camera calibration failed. Please refer to the assistant for details and recalibrate the camera after processing.",
    "0C00_4029": "Material not detected. Please confirm placement and continue.",
    "0C00_402A": "The visual marker was not detected. Please re-paste the paper in the correct position.",
    "0C00_402C": "Device data link error. Please reboot the printer",
    "0C00_402D": "The toolhead camera is not working properly; please reboot the device.",
    "0C00_403D": "The vision encoder plate was not detected. Please confirm it is correctly positioned on the heatbed.",
    "0C00_403E": "The high-precision nozzle offset calibration has failed, possibly due to a damaged pattern or the similarity of the colors of the two selected filaments. Please clear the printed pattern and replac...",
    "0C00_4041": "Toolhead camera calibration failed. Please ensure the Calibration Marker on the heatbed or Height Calibration Marker on the homing area is clean and undamaged, then re-run the calibration process.",
    "0C00_8001": "First layer defects were detected. If the defects are acceptable, select 'Resume' to resume the print job.",
    "0C00_8005": "Purged filament has piled up in the waste chute, which may cause a tool head collision.",
    "0C00_8009": "Build plate localization marker was not found.",
    "0C00_800B": "The heatbed marker was not detected. Please clear all objects and remove the mat. Make sure the marker is not obstructed.",
    "0C00_8015": "Objects detected on the platform; please clean them up in a timely manner.",
    "0C00_8016": "The foreign object detection function is not working. You can continue the task or check assistant for solutions.",
    "0C00_8017": "Foreign objects detected on the platform; please clean them up on time.",
    "0C00_8018": "The foreign object detection function is not working. You can continue the task or view the assistant for troubleshooting.",
    "0C00_8033": "Quick-release Lever is not locked. Please push it down to secure.",
    "0C00_8034": "Liveview Camera initialization failed. This print can still continue, but some AI functions will be disabled. If you encounter this issue again after restarting, please contact customer support.",
    "0C00_803F": "AI detected nozzle clumping. Please check the nozzle condition. Refer to assistant for solutions.",
    "0C00_8040": "AI detected air-printing defect. Please check the hotend extrusion status. Refer to assistant for solutions.",
    "0C00_8042": "The AI print monitor has detected a spaghetti defect. Please check the print and take the necessary action.",
    "0C00_8043": "AI detected nozzle clumping. Please check the nozzle condition. Refer to assistant for solutions.",
    "0C00_C003": "Possible defects were detected in the first layer.",
    "0C00_C004": "Possible spaghetti failure was detected.",
    "0C00_C006": "Purged filament may have piled up in the waste chute.",
    "1000_C001": "High bed temperature may lead to filament clogging in the nozzle. You may open the chamber door.",
    "1000_C002": "Printing CF material with stainless steel may cause nozzle damage.",
    "1000_C003": "Enabling Timelapse in traditional mode may cause defects; please activate this feature as needed.",
    "1001_4001": "Timelapse is not supported as Spiral Vase mode is enabled in slicing presets.",
    "1001_4002": "Timelapse is not supported as the Print sequence is set to 'By object'.",
    "1001_8003": "The time-lapse mode is set to Traditional in the slicing file. This may cause surface defects. Would you like to enable it?",
    "1001_8004": "Prime Tower is not enabled and time-lapse mode is set to Smooth in slicing file. This may cause surface defects. Would you like to enable it?",
    "1200_4001": "Filament is still loaded from the AMS when it has been disabled. Please unload AMS filament, load from spool holder, and restart print job.",
    "1200_8001": "Cutting the filament failed. Please check to see if the cutter is stuck. Refer to the Assistant for solutions.",
    "1200_8002": "The cutter is stuck. Please pull out the cutter handle.",
    "1200_8003": "Failed to pull out the filament from the extruder. Please check whether the extruder is clogged or whether the filament is broken inside the extruder.",
    "1200_8004": "Failed to pull back the filament from the toolhead. Please check whether the filament is stuck.",
    "1200_8005": "The filament is not inserted. Please insert the filament.",
    "1200_8006": "Unable to feed filament into the extruder. This could be due to tangled filament or a stuck spool. If not, please check if the AMS PTFE tube is connected.",
    "1200_8007": "Failed to extrude the filament. This might be caused by clogged extruder or stuck filament. Refer to the Assistant for solutions.",
    "1200_8010": "Filament or spool may be stuck.",
    "1200_8011": "AMS filament has run out. Please insert a new filament into the same AMS slot.",
    "1200_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "1200_8013": "Timeout while purging old filament. Please check if the filament is stuck or the extruder clogged.",
    "1200_8014": "The filament location in the toolhead was not found. Refer to the Assistant for solutions.",
    "1200_8015": "Failed to pull out the filament from the toolhead. Please check if the filament is stuck, or if it is broken inside the extruder or PTFE tube.",
    "1200_8016": "The extruder is not extruding normally. Refer to the Assistant for troubleshooting. There may be defects in this layer, but you may resume if the defects are acceptable.",
    "1201_4001": "Filament is still loaded from the AMS when it has been disabled. Please unload AMS filament, load from spool holder, and restart print job.",
    "1201_8001": "Failed to cut the filament. Please check the cutter.",
    "1201_8002": "The cutter is stuck. Please pull out the cutter handle.",
    "1201_8003": "Failed to pull out the filament from the extruder. Please check whether the extruder is clogged or whether the filament is broken inside the extruder.",
    "1201_8004": "Failed to pull back the filament from the toolhead. Please check whether the filament is stuck.",
    "1201_8005": "Failed to feed the filament. Please load the filament and then select 'Retry'.",
    "1201_8006": "Failed to feed the filament into the toolhead. Please check whether the filament is stuck.",
    "1201_8007": "Failed to extrude the filament. The extruder may be clogged or the filament may be stuck; please refer to HMS.",
    "1201_8010": "Please check if the spool or filament is stuck.",
    "1201_8011": "AMS filament has run out. Please insert a new filament into the same AMS slot.",
    "1201_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "1201_8013": "Timeout while purging old filament. Please check if the filament is stuck or the extruder clogged.",
    "1201_8014": "Failed to check the filament location in the tool head; please refer to the HMS.",
    "1201_8015": "Failed to pull back the filament from the toolhead. Please check if the filament is stuck or the filament is broken inside the extruder.",
    "1201_8016": "The extruder is not extruding normally; please refer to the HMS. After trouble shooting, if the defects are acceptable, please resume printing.",
    "1202_4001": "Filament is still loaded from the AMS when it has been disabled. Please unload AMS filament, load from spool holder, and restart print job.",
    "1202_8001": "Failed to cut the filament. Please check the cutter.",
    "1202_8002": "The cutter is stuck. Please pull out the cutter handle.",
    "1202_8003": "Failed to pull out the filament from the extruder. Please check whether the extruder is clogged or whether the filament is broken inside the extruder.",
    "1202_8004": "Failed to pull back the filament from the toolhead. Please check whether the filament is stuck.",
    "1202_8005": "The filament is not inserted. Please insert the filament.",
    "1202_8006": "Failed to feed the filament into the toolhead. Please check whether the filament is stuck.",
    "1202_8007": "Failed to extrude the filament. The extruder may be clogged or the filament may be stuck; please refer to HMS.",
    "1202_8010": "Please check if the spool or filament is stuck.",
    "1202_8011": "AMS filament has run out. Please insert a new filament into the same AMS slot.",
    "1202_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "1202_8013": "Timeout while purging old filament. Please check if the filament is stuck or the extruder clogged.",
    "1202_8014": "Failed to check the filament location in the tool head; please refer to the HMS.",
    "1202_8015": "Failed to pull back the filament from the toolhead. Please check if the filament is stuck or is broken inside the extruder.",
    "1202_8016": "The extruder is not extruding normally; please refer to the HMS. After trouble shooting, if the defects are acceptable, please resume printing.",
    "1203_4001": "Filament is still loaded from the AMS when it has been disabled. Please unload AMS filament, load from spool holder, and restart print job.",
    "1203_8001": "Failed to cut the filament. Please check the cutter.",
    "1203_8002": "The cutter is stuck. Please pull out the cutter handle.",
    "1203_8003": "Failed to pull out the filament from the extruder. Please check whether the extruder is clogged or whether the filament is broken inside the extruder.",
    "1203_8004": "Failed to pull back the filament from the toolhead. Please check whether the filament is stuck.",
    "1203_8005": "The filament is not inserted. Please insert the filament.",
    "1203_8006": "Failed to feed the filament into the toolhead. Please check whether the filament is stuck.",
    "1203_8007": "Failed to extrude the filament. The extruder may be clogged or the filament may be stuck; please refer to HMS.",
    "1203_8010": "Please check if the spool or filament is stuck.",
    "1203_8011": "AMS filament has run out. Please insert a new filament into the same AMS slot.",
    "1203_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "1203_8013": "Timeout while purging old filament. Please check if the filament is stuck or the extruder clogged.",
    "1203_8014": "Failed to check the filament location in the tool head; please refer to the HMS.",
    "1203_8015": "Failed to pull back the filament from the toolhead. Please check if the filament is stuck or is broken inside the extruder.",
    "1203_8016": "The extruder is not extruding normally; please refer to the HMS. After trouble shooting, if the defects are acceptable, please resume printing.",
    "12FF_4001": "Filament is still loaded from the AMS when it has been disabled. Please unload AMS filament, load from spool holder, and restart print job.",
    "12FF_8001": "Failed to cut the filament. Please check the cutter.",
    "12FF_8002": "The cutter is stuck. Please pull out the cutter handle.",
    "12FF_8003": "Please pull out the filament on the spool holder. If this message persists, please check to see if there is filament broken in the extruder or PTFE tube. (Connect a PTFE tube if you are about to us...",
    "12FF_8004": "Failed to pull back the filament from the toolhead. Please check whether the filament is stuck.",
    "12FF_8005": "The filament is not inserted. Please insert the filament.",
    "12FF_8006": "Please feed filament into the PTFE tube until it can not be pushed any farther.",
    "12FF_8007": "Check nozzle. Select 'Done' if filament was extruded, otherwise push filament forward slightly and select 'Retry.'",
    "12FF_8010": "Please check if the filament or the spool is stuck.",
    "12FF_8011": "AMS filament has run out. Please insert a new filament into the same AMS slot.",
    "12FF_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "12FF_8013": "Timeout while purging old filament. Please check if the filament is stuck or the extruder clogged.",
    "12FF_C003": "Please pull out the filament on the spool holder. If this message persists, please check to see if there is filament broken in the extruder or PTFE Tube. (Connect a PTFE tube if you are about to us...",
    "12FF_C006": "Please feed filament into the PTFE tube until it can not be pushed any farther.",
    "1800_4025": "Failed to read the filament information.",
    "1800_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "1800_8004": "AMS-HT failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "1800_8005": "The AMS-HT failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "1800_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "1800_8007": "Extruding filament failed. The extruder might be clogged.",
    "1800_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS-HT A to the extruder is properly connected.",
    "1800_8010": "The AMS-HT assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "1800_8011": "AMS-HT filament ran out. Please insert a new filament into the same AMS-HT slot.",
    "1800_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "1800_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "1800_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "1800_8017": "AMS-HT A is drying. Please stop drying process before loading/unloading material.",
    "1800_8021": "AMS setup failed; please refer to the assistant.",
    "1800_8023": "AMS-HT A cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "1800_C069": "An error occurred during AMS-HT A drying. Please go to Assistant for more details.",
    "1800_C06A": "AMS-HT A is reading RFID. Unable to start drying. Please try again later.",
    "1800_C06B": "AMS-HT A is changing filament. Unable to start drying. Please try again later.",
    "1800_C06C": "AMS-HT A is in Feed Assist Mode. Unable to start drying. Please try again later.",
    "1800_C06D": "AMS-HT A is assisting in filament insertion. Unable to start drying. Please try again later.",
    "1800_C06E": "AMS-HT A motor is performing self-test. Unable to start drying. Please try again later.",
    "1801_4025": "Failed to read the filament information.",
    "1801_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "1801_8004": "AMS-HT failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "1801_8005": "The AMS-HT failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "1801_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "1801_8007": "Extruding filament failed. The extruder might be clogged.",
    "1801_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS-HT B to the extruder is properly connected.",
    "1801_8010": "The AMS-HT assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "1801_8011": "AMS-HT filament ran out. Please insert a new filament into the same AMS-HT slot.",
    "1801_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "1801_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "1801_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "1801_8017": "AMS-HT B is drying. Please stop drying process before loading/unloading material.",
    "1801_8021": "AMS setup failed; please refer to the assistant.",
    "1801_8023": "AMS-HT B cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "1801_C069": "An error occurred during AMS-HT B drying. Please go to Assistant for more details.",
    "1801_C06A": "AMS-HT B is reading RFID. Unable to start drying. Please try again later.",
    "1801_C06B": "AMS-HT B is changing filament. Unable to start drying. Please try again later.",
    "1801_C06C": "AMS-HT B is in Feed Assist Mode. Unable to start drying. Please try again later.",
    "1801_C06D": "AMS-HT B is assisting in filament insertion. Unable to start drying. Please try again later.",
    "1801_C06E": "AMS-HT B motor is performing self-test. Unable to start drying. Please try again later.",
    "1802_4025": "Failed to read the filament information.",
    "1802_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "1802_8004": "AMS-HT failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "1802_8005": "The AMS-HT failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "1802_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "1802_8007": "Extruding filament failed. The extruder might be clogged.",
    "1802_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS-HT C to the extruder is properly connected.",
    "1802_8010": "The AMS-HT assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "1802_8011": "AMS-HT filament ran out. Please insert a new filament into the same AMS-HT slot.",
    "1802_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "1802_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "1802_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "1802_8017": "AMS-HT C is drying. Please stop drying process before loading/unloading material.",
    "1802_8021": "AMS setup failed; please refer to the assistant.",
    "1802_8023": "AMS-HT C cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "1802_C069": "An error occurred during AMS-HT C drying. Please go to Assistant for more details.",
    "1802_C06A": "AMS-HT C is reading RFID. Unable to start drying. Please try again later.",
    "1802_C06B": "AMS-HT C is changing filament. Unable to start drying. Please try again later.",
    "1802_C06C": "AMS-HT C is in Feed Assist Mode. Unable to start drying. Please try again later.",
    "1802_C06D": "AMS-HT C is assisting in filament insertion. Unable to start drying. Please try again later.",
    "1802_C06E": "AMS-HT C motor is performing self-test. Unable to start drying. Please try again later.",
    "1803_4025": "Failed to read the filament information.",
    "1803_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "1803_8004": "AMS-HT failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "1803_8005": "The AMS-HT failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "1803_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "1803_8007": "Extruding filament failed. The extruder might be clogged.",
    "1803_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS-HT D to the extruder is properly connected.",
    "1803_8010": "The AMS-HT assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "1803_8011": "AMS-HT filament ran out. Please insert a new filament into the same AMS-HT slot.",
    "1803_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "1803_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "1803_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "1803_8017": "AMS-HT D is drying. Please stop drying process before loading/unloading material.",
    "1803_8021": "AMS setup failed; please refer to the assistant.",
    "1803_8023": "AMS-HT D cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "1803_C069": "An error occurred during AMS-HT D drying. Please go to Assistant for more details.",
    "1803_C06A": "AMS-HT D is reading RFID. Unable to start drying. Please try again later.",
    "1803_C06B": "AMS-HT D is changing filament. Unable to start drying. Please try again later.",
    "1803_C06C": "AMS-HT D is in Feed Assist Mode. Unable to start drying. Please try again later.",
    "1803_C06D": "AMS-HT D is assisting in filament insertion. Unable to start drying. Please try again later.",
    "1803_C06E": "AMS-HT D motor is performing self-test. Unable to start drying. Please try again later.",
    "1804_4025": "Failed to read the filament information.",
    "1804_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "1804_8004": "AMS-HT failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "1804_8005": "The AMS-HT failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "1804_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "1804_8007": "Extruding filament failed. The extruder might be clogged.",
    "1804_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS-HT E to the extruder is properly connected.",
    "1804_8010": "The AMS-HT assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "1804_8011": "AMS-HT filament ran out. Please insert a new filament into the same AMS-HT slot.",
    "1804_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "1804_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "1804_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "1804_8021": "AMS setup failed; please refer to the assistant.",
    "1804_8023": "AMS-HT E cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "1804_C069": "An error occurred during AMS-HT E drying. Please go to Assistant for more details.",
    "1804_C06A": "AMS-HT E is reading RFID. Unable to start drying. Please try again later.",
    "1804_C06B": "AMS-HT E is changing filament. Unable to start drying. Please try again later.",
    "1804_C06C": "AMS-HT E is in Feed Assist Mode. Unable to start drying. Please try again later.",
    "1804_C06D": "AMS-HT E is assisting in filament insertion. Unable to start drying. Please try again later.",
    "1804_C06E": "AMS-HT E motor is performing self-test. Unable to start drying. Please try again later.",
    "1805_4025": "Failed to read the filament information.",
    "1805_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "1805_8004": "AMS-HT failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "1805_8005": "The AMS-HT failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "1805_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "1805_8007": "Extruding filament failed. The extruder might be clogged.",
    "1805_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS-HT F to the extruder is properly connected.",
    "1805_8010": "The AMS-HT assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "1805_8011": "AMS-HT filament ran out. Please insert a new filament into the same AMS-HT slot.",
    "1805_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "1805_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "1805_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "1805_8021": "AMS setup failed; please refer to the assistant.",
    "1805_8023": "AMS-HT F cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "1805_C069": "An error occurred during AMS-HT F drying. Please go to Assistant for more details.",
    "1805_C06A": "AMS-HT F is reading RFID. Unable to start drying. Please try again later.",
    "1805_C06B": "AMS-HT F is changing filament. Unable to start drying. Please try again later.",
    "1805_C06C": "AMS-HT F is in Feed Assist Mode. Unable to start drying. Please try again later.",
    "1805_C06D": "AMS-HT F is assisting in filament insertion. Unable to start drying. Please try again later.",
    "1805_C06E": "AMS-HT F motor is performing self-test. Unable to start drying. Please try again later.",
    "1806_4025": "Failed to read the filament information.",
    "1806_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "1806_8004": "AMS-HT failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "1806_8005": "The AMS-HT failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "1806_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "1806_8007": "Extruding filament failed. The extruder might be clogged.",
    "1806_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS-HT G to the extruder is properly connected.",
    "1806_8010": "The AMS-HT assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "1806_8011": "AMS-HT filament ran out. Please insert a new filament into the same AMS-HT slot.",
    "1806_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "1806_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "1806_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "1806_8021": "AMS setup failed; please refer to the assistant.",
    "1806_8023": "AMS-HT G cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "1806_C069": "An error occurred during AMS-HT G drying. Please go to Assistant for more details.",
    "1806_C06A": "AMS-HT G is reading RFID. Unable to start drying. Please try again later.",
    "1806_C06B": "AMS-HT G is changing filament. Unable to start drying. Please try again later.",
    "1806_C06C": "AMS-HT G is in Feed Assist Mode. Unable to start drying. Please try again later.",
    "1806_C06D": "AMS-HT G is assisting in filament insertion. Unable to start drying. Please try again later.",
    "1806_C06E": "AMS-HT G motor is performing self-test. Unable to start drying. Please try again later.",
    "1807_4025": "Failed to read the filament information.",
    "1807_8003": "Failed to pull out the filament from the extruder. This might be caused by clogged extruder or filament broken inside the extruder.",
    "1807_8004": "AMS-HT failed to pull back filament. This could be due to a stuck spool or the end of the filament being stuck in the path.",
    "1807_8005": "The AMS-HT failed to send out filament. You can clip the end of your filament flat, and reinsert. If this message persists, please check the PTFE tubes in AMS for any signs of wear and tear.",
    "1807_8006": "Unable to feed filament into the extruder. The AMS may be mismatched with the extruder. You can rerun the AMS Setup. This could also be due to an entangled filament or a stuck spool. If not, please...",
    "1807_8007": "Extruding filament failed. The extruder might be clogged.",
    "1807_800A": "PTFE tube disconnection detected. Please check if the PTFE tube from AMS-HT H to the extruder is properly connected.",
    "1807_8010": "The AMS-HT assist motor is overloaded. This could be due to entangled filament or a stuck spool.",
    "1807_8011": "AMS-HT filament ran out. Please insert a new filament into the same AMS-HT slot.",
    "1807_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "1807_8013": "Timeout purging old filament: Please check if the filament is stuck or the extruder is clogged.",
    "1807_8016": "The extruder is not extruding normally; please refer to the Assistant. After trouble shooting. If the defects are acceptable, please resume.",
    "1807_8021": "AMS setup failed; please refer to the assistant.",
    "1807_8023": "AMS-HT H cooling failed. The ambient temperature may be too high. Please operate the device in a suitable environment.",
    "1807_C069": "An error occurred during AMS-HT H drying. Please go to Assistant for more details.",
    "1807_C06A": "AMS-HT H is reading RFID. Unable to start drying. Please try again later.",
    "1807_C06B": "AMS-HT H is changing filament. Unable to start drying. Please try again later.",
    "1807_C06C": "AMS-HT H is in Feed Assist Mode. Unable to start drying. Please try again later.",
    "1807_C06D": "AMS-HT H is assisting in filament insertion. Unable to start drying. Please try again later.",
    "1807_C06E": "AMS-HT H motor is performing self-test. Unable to start drying. Please try again later.",
    "18FE_8001": "Failed to cut the filament of the left extruder. Please check the cutter.",
    "18FE_8002": "The cutter of the left extruder is stuck. Please pull out the cutter handle.",
    "18FE_8003": "Please pull out the filament on the spool holder  of the left extruder. If this message persists, please check to see if there is filament broken in the extruder. (Connect a PTFE tube if you are ab...",
    "18FE_8004": "Failed to pull back the filament from the left extruder. Please check whether the filament is stuck inside the extruder.",
    "18FE_8005": "Failed to feed the filament outside the AMS-HT. Please clip the end of the filament flat and check to see if the spool is stuck.",
    "18FE_8006": "Please feed filament into the PTFE tube of the left extruder until it can not be pushed any farther.",
    "18FE_8007": "Please observe the nozzle of the left extruder. If the filament has been extruded, select 'Continue'; if it has not, please push the filament forward slightly, and then select 'Retry'.",
    "18FE_8011": "The external filament connected to the left extruder has run out; please load a new filament.",
    "18FE_8012": "Failed to get mapping table; please select 'Resume' to retry.",
    "18FE_8013": "Timeout purging old filament of the left extruder: Please check if the filament is stuck or the extruder is clogged.",
    "18FE_8020": "Extruder change failed; please refer to the assistant.",
    "18FE_8021": "AMS setup failed; please refer to the assistant.",
    "18FE_8024": "Extruder position calibration failed; please refer to the assistant.",
    "18FE_C003": "Please pull out the filament on the spool holder of the left extruder. If this message persists, please check to see if there is filament broken in the extruder or PTFE tube. (Connect a PTFE tube i...",
    "18FE_C006": "Please feed filament into the PTFE tube of the left extruder until it can not be pushed any farther.",
    "18FE_C008": "Please pull out the filament on the spool holder of the left extruder. If this message persists, please check to see if there is filament broken in the extruder or PTFE tube. (Connect a PTFE tube i...",
    "18FE_C009": "Please feed filament into the PTFE tube of the left extruder until it can not be pushed any farther.",
    "18FE_C00A": "Please observe the nozzle of the left extruder. If the filament has been extruded, select 'Continue'; if not, please push the filament forward slightly and then select 'Retry'.",
    "18FF_8001": "Failed to cut the filament of the right extruder. Please check the cutter.",
    "18FF_8002": "The cutter of the right extruder is stuck. Please pull out the cutter handle.",
    "18FF_8003": "Please pull out the filament on the spool holder  of the right extruder. If this message persists, please check to see if there is filament broken in the extruder. (Connect a PTFE tube if you are a...",
    "18FF_8004": "Failed to pull back the filament from the right extruder. Please check whether the filament is stuck inside the extruder.",
    "18FF_8005": "Failed to feed the filament outside the AMS-HT. Please clip the end of the filament flat and check to see if the spool is stuck.",
    "18FF_8006": "Please feed filament into the PTFE tube of the right extruder until it can not be pushed any farther.",
    "18FF_8007": "Please observe the nozzle of the right extruder. If the filament has been extruded, select 'Continue'; if it has not, please push the filament forward slightly, and then select 'Retry'.",
    "18FF_8011": "The external filament connected to the right extruder has run out; please load a new filament.",
    "18FF_8012": "Failed to get AMS mapping table; please select 'Resume' to retry.",
    "18FF_8013": "Timeout purging old filament of the right extruder: Please check if the filament is stuck or the extruder is clogged.",
    "18FF_8020": "Extruder change failed; please refer to the assistant.",
    "18FF_8021": "AMS setup failed; please refer to the assistant.",
    "18FF_8024": "Extruder position calibration failed; please refer to the assistant.",
    "18FF_C003": "Please pull out the filament on the spool holder of the right extruder. If this message persists, please check to see if there is filament broken in the extruder or PTFE tube. (Connect a PTFE tube ...",
    "18FF_C006": "Please feed filament into the PTFE tube of the right extruder until it can not be pushed any farther.",
    "18FF_C008": "Please pull out the filament on the spool holder of the right extruder. If this message persists, please check to see if there is filament broken in the extruder or PTFE tube. (Connect a PTFE tube ...",
    "18FF_C009": "Please feed filament into the PTFE tube of the right extruder until it can not be pushed any farther.",
    "18FF_C00A": "Please observe the nozzle of the right extruder. If the filament has been extruded, select 'Continue'; if not, please push the filament forward slightly and then select 'Retry'.",
}


def get_error_description(error_code: str) -> str | None:
    """Get human-readable description for an HMS error code.

    Args:
        error_code: Error code in format "XXXX_YYYY" (e.g., "0300_400C")

    Returns:
        Human-readable description or None if not found
    """
    return HMS_ERROR_DESCRIPTIONS.get(error_code.upper())


def hms_severity(code: int | str) -> int:
    """Decode HMS severity from the firmware's error ``code`` word.

    Bambu encodes severity in the high 16 bits of the 32-bit ``code``:
    1=fatal, 2=serious, 3=common, 4=info. (The legacy path incorrectly read
    ``(attr >> 8) & 0xF``, which decoded every real fault as fatal.) Accepts a
    hex string like ``hms_short_code`` does. Anything outside {1,2,3,4} falls
    back to 2 (serious) so an unrecognised value never silences a fault.
    """
    if isinstance(code, str):
        code_int = int(code.replace("0x", ""), 16) if code else 0
    else:
        code_int = int(code or 0)
    sev = (code_int >> 16) & 0xFFFF
    return sev if sev in (1, 2, 3, 4) else 2


# The wiki origin. Per-code deep links come from the vendored catalog
# (hms_catalog.lookup_wiki_path); codes with no deep link fall back to the HMS
# landing page. Defined once here so the REST and WebSocket payloads share a
# single source of truth.
HMS_WIKI_URL_ORIGIN = "https://wiki.bambulab.com"
HMS_WIKI_URL = HMS_WIKI_URL_ORIGIN + "/en/hms/home"


def hms_short_code(attr: int, code: int | str) -> str:
    """Build the canonical "MMMM_CCCC" HMS short code from raw attr/code values."""
    if isinstance(code, str):
        code_int = int(code.replace("0x", ""), 16) if code else 0
    else:
        code_int = int(code or 0)
    attr_int = int(attr or 0)
    return f"{(attr_int >> 16) & 0xFFFF:04X}_{code_int & 0xFFFF:04X}"


def lookup_description_any(attr: int | str, code: int | str) -> str | None:
    """Resolve HMS fault text from raw attr/code for dict-shaped consumers.

    Tries the lossless full ``ecode`` (16-hex ``attr``+``code``) against the
    vendored catalog first, then falls back to the legacy 2-group
    ``MMMM_CCCC`` table. Returns None when neither matches. Accepts hex strings
    the same way ``hms_short_code`` does.
    """
    if isinstance(attr, str):
        attr_int = int(attr.replace("0x", ""), 16) if attr else 0
    else:
        attr_int = int(attr or 0)
    if isinstance(code, str):
        code_int = int(code.replace("0x", ""), 16) if code else 0
    else:
        code_int = int(code or 0)
    full_code = f"{attr_int:08X}{code_int:08X}"
    return lookup_full_code(full_code) or get_error_description(hms_short_code(attr, code))


# Firmware runout ``code`` words (low 32 bits) that carry a per-slot attribution
# in their ``attr`` — the ``0700_2X00`` family that names the exhausted slot on the
# printer screen ("AMS A Slot 3 filament has run out …"). Probe-verified against the
# vendored catalog (128 real ecodes) and the two live 2026-07-19 incident attrs.
# The slot-agnostic "insert into the SAME slot" runout (07xx_8011) is deliberately
# absent — it names no slot, so the resolver falls back to tray_now/mapping there.
_RUNOUT_SLOT_CODE32: frozenset[int] = frozenset({0x00020001, 0x00020005, 0x00030001, 0x00030002})

# The DEMAND subset of :data:`_RUNOUT_SLOT_CODE32` — the code words that mean "the
# firmware is asking for filament in THIS slot RIGHT NOW". Every member of the
# parent set names a slot; only these members name a slot the operator must FILL.
# Classified 006-H2S 2026-07-26 from the vendored catalog text of the ``0700_2X00``
# family (``app/data/hms_error_text_en.json.gz``, quoted verbatim below):
#
#   * 0x00020001 IN — "AMS A Slot 3 filament has run out. Please insert a new
#     filament." The canonical insert-here demand, and the exact code standing on
#     006-H2S for slot 3 (``0700_2200_0002_0001``) while the escalation text named
#     slot 1 off the stale dispatch mapping.
#   * 0x00020005 OUT — "AMS A Slot 1 filament has run out, and purging the old
#     filament went abnormally; please check whether the filament is stuck in the
#     tool head." The ask is a TOOL-HEAD inspection, not an insert; treating it as a
#     demand would let the refill auto-resume drive a print back into a purge fault.
#   * 0x00030001 OUT — "AMS A Slot 1 filament has run out. Please wait while old
#     filament is purged." An in-progress notice ("please wait"), not an ask —
#     the firmware follows it with the 0x00020001 demand or the 0x00030002
#     auto-switch, whichever way the purge lands.
#   * 0x00030002 OUT — "AMS A Slot 1 filament has run out and automatically
#     switched to the slot with the same filament." NEVER a demand: the firmware
#     backup already rescued the print and nothing is being asked for. It remains
#     VALID SPENT EVIDENCE, which is why ``spool_respool._resolve_exhausted_tray``
#     deliberately keeps consuming the whole parent set — do not narrow that.
_RUNOUT_DEMAND_CODE32: frozenset[int] = frozenset({0x00020001})

# The SPENT-EVIDENCE subset of :data:`_RUNOUT_SLOT_CODE32` — the code words whose
# catalog text is the firmware's own statement that a roll PHYSICALLY RAN DRY, and
# so may stamp ``spool.spent_at`` (``spool_respool.mark_spent_on_slot_runout``).
# Deliberately narrower than both sets above, because a spent stamp is a ledger
# mutation on the operator's inventory — a false one archives a healthy roll:
#
#   * 0x00030002 IN — "AMS A Slot 1 filament has run out and automatically switched
#     to the slot with the same filament." The one family member that can ONLY mean
#     the roll ended: the firmware is not asking for anything, it is REPORTING a
#     completed backup switch it would never perform on a slot still feeding.
#   * 0x00020001 OUT — the bare demand. 006-H2S 2026-07-26 proved firmware can latch
#     a BOGUS demand for a slot that never ran dry (a load command issued during a
#     runout hold resurfaced 12 h later as a demand for the latched slot); stamping
#     on a demand would have marked a healthy roll spent.
#   * 0x00030001 OUT — "please wait while old filament is purged" is transitional and
#     is ALWAYS followed by the demand or by the 0x00030002 auto-switch, whichever way
#     the purge lands — so it adds zero coverage while inheriting the demand's risk.
#   * 0x00020005 OUT — purge-abnormal, entangled with a tool-head fault ("check whether
#     the filament is stuck in the tool head") where a misread of the runout itself is
#     plausible. Residual coverage stays with the Tier-3 / fresh-roll prompts.
#
# All four remain in :data:`_RUNOUT_SLOT_CODE32` — this narrowing governs only WHETHER
# to stamp, never slot RESOLUTION, which must keep consuming the whole parent set.
_RUNOUT_AUTO_SWITCH_SPENT_CODE32: frozenset[int] = frozenset({0x00030002})

# Code words whose OPERATOR NOTIFICATION is suppressed — a statement the firmware
# makes about work it already finished, where the farm's only correct reaction is
# bookkeeping. Notification only: ``hms_event`` recording, the spent stamp and every
# recovery lane read these codes exactly as before.
#
#   * 0x00030002 — "…has run out and automatically switched to the slot with the same
#     filament." The AMS backup RESCUED the print; nothing stopped and nothing is
#     asked for. The firmware sends it at severity 3 (common), which sits inside the
#     notify band, so it paged the operator on every successful rescue — 87 alerts in
#     14 days for prints that never paused.
#
# It is deliberately absent from the fault TAXONOMY and must stay absent: it is THE
# spent evidence (:data:`_RUNOUT_AUTO_SWITCH_SPENT_CODE32`), and a second consumer
# reading it as a generic fault would double-stamp the operator's ledger. Suppressing
# a notification is not classifying a fault, which is why the two live apart.
NOTIFY_SUPPRESSED_CODE32: frozenset[int] = frozenset({0x00030002})


def is_notify_suppressed(attr: int, code: int | str) -> bool:
    """True when this fault's raw operator alert is suppressed (see the set above).

    Scoped by the SAME shape the spent lane consumes — an AMS-module attr that
    decodes to a slot — never by the short code: ``0x00030002`` masks to the short
    form ``07xx_0002``, which the assist-motor overload (``0x00020002``) also masks
    to, and that one must keep alerting. Fails toward NOTIFYING on anything
    malformed.
    """
    try:
        return _code_word(code) in NOTIFY_SUPPRESSED_CODE32 and ams_slot_from_attr(int(attr or 0)) is not None
    except (TypeError, ValueError):
        return False


def ams_slot_from_attr(attr: int) -> tuple[int, int] | None:
    """Decode the AMS unit + slot a slot-attributed HMS ``attr`` names, or ``None``.

    Pure layout decode, shared by every slot-attributed AMS fault family: the high
    byte is the module class (``0x07`` = AMS), the next byte is the AMS unit id, and
    the third byte encodes the slot as ``0x20 + tray`` (``0x20``..``0x23`` → tray
    0..3). Fails closed (``None``) for a non-AMS module, an attr that carries no slot
    byte, or an out-of-range unit — so callers fall back to their own attribution.

    The CODE word decides whether a given fault family actually uses this layout;
    that judgement stays with the per-family predicates (:func:`runout_slot_from_hms`,
    :func:`filament_read_failure_slot`), never here.
    """
    if (attr >> 24) & 0xFF != 0x07:  # not an AMS-module fault
        return None
    slot_byte = (attr >> 8) & 0xFF
    if not (0x20 <= slot_byte <= 0x23):  # not a slot-attributed attr
        return None
    ams_id = (attr >> 16) & 0xFF
    if not (0 <= ams_id <= 7):
        return None
    return (ams_id, slot_byte - 0x20)


def ams_unit_from_attr(attr: int) -> int | None:
    """Decode the AMS unit id an AMS-module HMS ``attr`` names, or ``None``.

    The unit byte is present on EVERY ``0x07`` fault (``0700_*`` = unit 0,
    ``0701_*`` = unit 1 …), including the ones that name no slot — so a unit-scoped
    fault such as ``0700_4025`` ("Failed to read the filament information") can still
    be attributed to its AMS. Fails closed for a non-AMS module.
    """
    if (attr >> 24) & 0xFF != 0x07:
        return None
    ams_id = (attr >> 16) & 0xFF
    return ams_id if 0 <= ams_id <= 7 else None


# Firmware ``code`` words for "the AMS could not read the filament tag". Two shapes,
# both live-observed on the fleet:
#   * ``0x00010081`` — the slot-attributed ``0700_2X00_0001_0081`` family ("Failed to
#     read the filament information from AMS A slot 1. The AMS main board may be
#     malfunctioning."); its attr carries the slot byte, so it decodes to an exact slot.
#   * low-16 ``0x4025`` — ``07XX_4025`` ("Failed to read the filament information."),
#     which names the AMS unit but NO slot.
# A commanded RFID read on a slot that holds no tag can only fail this way, and the
# resulting code can never self-clear on a tagless slot — which is why the farm
# suppresses its OWN discovery reads' failures (services/ams_presence).
_READ_FAILURE_CODE32: frozenset[int] = frozenset({0x00010081})
_READ_FAILURE_CODE16: frozenset[int] = frozenset({0x4025})


def is_filament_read_failure(attr: int, code: int) -> bool:
    """True when (attr, code) is an AMS "failed to read the filament information" fault.

    Restricted to the AMS module (``0x07``) so an unrelated module reusing the same
    low-16 code word can never be classified as a tag-read failure.
    """
    if (attr >> 24) & 0xFF != 0x07:
        return False
    return code in _READ_FAILURE_CODE32 or (code & 0xFFFF) in _READ_FAILURE_CODE16


def filament_read_failure_slot(attr: int, code: int) -> tuple[int, int] | None:
    """The exact ``(ams_id, tray_id)`` a read-failure HMS names, or ``None``.

    ``None`` means either "not a read failure" or "this read failure names no slot"
    (the ``07XX_4025`` shape) — callers that need to tell those apart pair this with
    :func:`is_filament_read_failure` / :func:`ams_unit_from_attr`.
    """
    if not is_filament_read_failure(attr, code):
        return None
    return ams_slot_from_attr(attr)


def runout_slot_from_hms(attr: int, code: int) -> tuple[int, int] | None:
    """Decode the AMS unit + slot a per-slot runout HMS names, or ``None``.

    The firmware's ``0700_2X00`` slot-attributed runout family, decoded through the
    shared :func:`ams_slot_from_attr` layout reader. ``code`` (the full 32-bit code
    word) must be a runout code that carries slot attribution
    (:data:`_RUNOUT_SLOT_CODE32`) — this predicate is what keeps the decode
    runout-specific, since other families share the attr layout. Returns
    ``(ams_id, tray_id)`` on a match, else ``None`` — so a non-runout code, the
    slot-agnostic 8011 "insert same slot" runout, and any malformed value all fail
    closed and let the caller fall back to tray_now/mapping inference.

    Verified: attr ``117448704`` / code ``0x00020001`` → ``(0, 0)`` ("AMS A Slot 1");
    attr ``117449216`` → ``(0, 2)`` ("AMS A Slot 3") — the two live incident faults.
    """
    if code not in _RUNOUT_SLOT_CODE32:
        return None
    return ams_slot_from_attr(attr)


# ===========================================================================
# AMS fault taxonomy — ONE classification of the AMS fault vocabulary
# ===========================================================================
# Every consumer that needs to know what KIND of AMS fault a code is reads it
# from here (doctrine invariant 1, one origin per magic value): the swap machine,
# the runout lane, the read-failure suppression and the spent-evidence gate all
# describe the same firmware vocabulary and must never disagree about a code.
#
# TWO LANES, because the firmware speaks two (see hms_short_code / §"anatomy"):
#   * :func:`classify_ams_fault` — the ``hms[]`` array lane, 32-bit attr + 32-bit
#     code word. The code word names the fault; the attr's SUBMODULE byte scopes
#     what that word means, so a row is only honored under the submodules whose
#     catalog text it was read from.
#   * :func:`classify_short_code` — the ``print_error`` lane, whose ``MMMM_CCCC``
#     short form has already thrown the submodule byte away. Necessarily coarser.
#
# EVERY row is backed by the verbatim text of the vendored catalog
# (``app/data/hms_error_text_en.json.gz``, AMS-unit-A variants quoted; the other
# units carry the same sentence with the unit letter swapped). A code word with no
# row under the observed attr classifies ``None`` — meaning "not AMS-fault-
# actionable, the generic notify lane owns it", never "benign".


class AmsFaultClass(str, Enum):
    """What KIND of AMS fault a code is — the farm's reaction vocabulary.

    ``str`` mixin so the value serializes straight onto the wire / into logs.
    """

    RUNOUT = "runout"  # an AMS SLOT ran dry and the print is HELD for a same-slot refill
    RUNOUT_EXTERNAL = "runout_external"  # the EXTERNAL spool holder ran dry — no AMS slot, no swap
    MECHANICAL_FEED = "mechanical_feed"  # the path is obstructed/slipping — fresh filament can clear it
    PHYSICAL_FAULT = "physical_fault"  # breakage/clog/hardware — needs physical work, never a swap
    RFID_READ = "rfid_read"  # the tag could not be read (expected on a tagless slot)
    INFORMATIONAL = "informational"  # a progress notice or a precursor — no farm action


@dataclass(frozen=True)
class ClassifiedAmsFault:
    """The taxonomy's verdict for one fault.

    ``extruder_side`` marks a fault whose common factor is the EXTRUDER rather than
    the spool (a re-fault after a swap must not penalize the replacement).
    ``slot`` is the ``(ams_id, tray_id)`` the attr names via :func:`ams_slot_from_attr`,
    and is always ``None`` on the short-code lane — the short form discards the attr
    low byte that carries it.
    ``external`` marks a fault on the EXTERNAL spool holder rather than inside an AMS
    (see :data:`_EXTERNAL_UNIT_BYTES`). It is orthogonal to the class — the holder can
    run out, fail to feed or need hands — and it is always paired with ``slot=None``,
    because a holder has no AMS slot at all. Consumers use it to route away from every
    AMS-shaped reaction (no swap, no tray to unload, no slot to send an operator to).
    """

    fault_class: AmsFaultClass
    extruder_side: bool
    slot: tuple[int, int] | None
    external: bool = False


# AMS-bearing modules: 0x07 AMS / AMS 2 Pro, 0x12 AMS lite, 0x18 AMS-HT. A code
# word reused by a non-AMS module (0x03 motion, 0x05 mainboard/camera) is never an
# AMS fault, so the lane gates on the module first — the same fail-closed shape
# :func:`is_filament_read_failure` uses.
_AMS_MODULES: frozenset[int] = frozenset({0x07, 0x12, 0x18})

# The attr SUBMODULE byte (``(attr >> 8) & 0xFF``) that scopes a code word's meaning.
# 0x00020002 is the proof this scoping is required, not decoration — the ONE code
# word carries three unrelated faults under three submodules (rows below).
_TRAY_ATTR_BYTES: frozenset[int] = frozenset({0x20, 0x21, 0x22, 0x23})  # per-slot faults
_MOTOR_ATTR_BYTES: frozenset[int] = frozenset({0x01, 0x10, 0x11, 0x12, 0x13})  # assist + feeder motors
_RFID_ATTR_BYTES: frozenset[int] = frozenset({0x30, 0x31, 0x32, 0x33})  # per-slot RFID reader

# The AMS-UNIT byte (``(attr >> 16) & 0xFF``) an EXTERNAL SPOOL HOLDER speaks under.
# It is not an AMS unit at all: the firmware reuses the unit field to say "this fault
# is on the spool holder", 0xFF for the (right/main) holder and 0xFE for the second
# one dual-nozzle hardware carries. Both appear under the AMS module classes (07 and
# 18), which is why the holder's faults reach an AMS classifier at all — and why
# :func:`ams_slot_from_attr` fails closed on them (it accepts units 0..7 only), so an
# external verdict can never carry a slot.
_EXTERNAL_UNIT_BYTES: frozenset[int] = frozenset({0xFE, 0xFF})


@dataclass(frozen=True)
class _CodeWordRow:
    """One (code word × submodule) classification in the ``hms[]`` lane."""

    attr_bytes: frozenset[int]
    fault_class: AmsFaultClass
    extruder_side: bool = False


_MECHANICAL = AmsFaultClass.MECHANICAL_FEED
_PHYSICAL = AmsFaultClass.PHYSICAL_FAULT
_RFID = AmsFaultClass.RFID_READ
_INFO = AmsFaultClass.INFORMATIONAL

# --- The hms[] code-word table ---------------------------------------------
# Scoped to the submodules whose text was read; catalog sentence quoted per row.
#
# THREE code words are deliberately ABSENT so they classify None — each is owned by
# a dedicated decoder and routing it through the generic taxonomy would regress a
# ratified design:
#   * 0x00020001 (tray attrs) — "AMS A Slot 3 filament has run out. Please insert a
#     new filament." The DEMAND, owned by :func:`current_runout_demand`. Doctrine
#     rule 9: runouts escalate for a SAME-slot refill, jams swap — a demand that
#     reached a fault classifier could route a runout into the swap machine. It is
#     also NOT spent evidence: 006-H2S 2026-07-26 proved the firmware latches a
#     BOGUS demand for a slot that never ran dry.
#   * 0x00030002 (tray attrs) — "…has run out and automatically switched to the slot
#     with the same filament." THE spent evidence, owned by
#     :data:`_RUNOUT_AUTO_SWITCH_SPENT_CODE32` / ``spool_respool``. A second consumer
#     reading it as a generic fault would double-stamp the operator's ledger.
#   * 0x00020002 under TRAY attrs — "AMS A Slot 1 is empty; please insert a new
#     filament." An empty-slot ASK, hazard-identical to the 0x00020001 demand: an
#     empty slot is not evidence a roll ran dry and not a fault a swap can fix. The
#     generic notify lane tells the operator; no farm machine consumes it. (The SAME
#     code word under the motor and RFID submodules IS classified — see below.)
_CODE_WORD_TAXONOMY: dict[int, tuple[_CodeWordRow, ...]] = {
    # -- 0x00020002, the three-meaning code word -----------------------------
    # tray attrs: "AMS A Slot 1 is empty; please insert a new filament." -> None (above)
    0x00020002: (
        # "The AMS A assist motor is overloaded. The filament may be tangled or
        # stuck." / "The AMS A slot 1 motor is overloaded. The filament may be
        # tangled or stuck." — the 16-hex twin of the 8010 swap trigger.
        _CodeWordRow(_MOTOR_ATTR_BYTES, _MECHANICAL),
        # "The RFID-tag on AMS A Slot1 is damaged, or its content cannot be identified."
        _CodeWordRow(_RFID_ATTR_BYTES, _RFID),
    ),
    # -- MECHANICAL_FEED: the path is obstructed or slipping ------------------
    # "Failed to adjust the buffer position. The AMS A Slot 1 filament or the
    # buffer itself may be jammed."
    0x0002000A: (_CodeWordRow(_TRAY_ATTR_BYTES, _MECHANICAL),),
    # "AMS A slot 1 feeds filament out of AMS timeout."
    0x00020010: (_CodeWordRow(_TRAY_ATTR_BYTES, _MECHANICAL),),
    # "AMS A slot 1 feeder unit motor is stalled, cannot rotate the spool."
    0x00020012: (_CodeWordRow(_TRAY_ATTR_BYTES, _MECHANICAL),),
    # "AMS A slot 1 assist motor has slipped. Please pull out the filament, cut
    # off the worn part, and then try again."
    0x00020016: (_CodeWordRow(_TRAY_ATTR_BYTES, _MECHANICAL),),
    # The tube-resistance ladder — one code word per tube segment, AMS side:
    # "…assist motor is stalled，due to excessive resistance in the tube
    # between AMS and the printer / near AMS / between AMS and the filament
    # buffer / near the filament buffer."
    0x00020017: (_CodeWordRow(_TRAY_ATTR_BYTES, _MECHANICAL),),
    0x00020018: (_CodeWordRow(_TRAY_ATTR_BYTES, _MECHANICAL),),
    0x00020019: (_CodeWordRow(_TRAY_ATTR_BYTES, _MECHANICAL),),
    0x00020020: (_CodeWordRow(_TRAY_ATTR_BYTES, _MECHANICAL),),
    # …and the two toolhead-side segments of the same ladder: "…in the tube
    # between the filament buffer and the toolhead" / "…in the tube near the
    # toolhead". Past the buffer the EXTRUDER is the common factor, not the spool.
    0x00020021: (_CodeWordRow(_TRAY_ATTR_BYTES, _MECHANICAL, extruder_side=True),),
    0x00020022: (_CodeWordRow(_TRAY_ATTR_BYTES, _MECHANICAL, extruder_side=True),),
    # "AMS A slot 1 assist motor overloaded. Excessive resistance in the filament
    # tube between the AMS and the filament track switch / between the filament
    # track switch and the filament buffer." (FTS-equipped models.)
    0x00020026: (_CodeWordRow(_TRAY_ATTR_BYTES, _MECHANICAL),),
    0x00020027: (_CodeWordRow(_TRAY_ATTR_BYTES, _MECHANICAL),),
    # -- PHYSICAL_FAULT: breakage / clog / hardware — a swap cannot fix it -----
    # "AMS A Slot 1's filament may be broken in AMS."
    0x00020003: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # "AMS A Slot 1 filament may be broken in the tool head."
    0x00020004: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # "AMS A Slot 1 filament has run out, and purging the old filament went
    # abnormally; please check whether the filament is stuck in the tool head."
    # A runout ENTANGLED with a tool-head fault: the ask is an inspection, which is
    # why it is neither RUNOUT nor spent evidence (see _RUNOUT_AUTO_SWITCH_SPENT_CODE32).
    0x00020005: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # "AMS A has detected a breakage of the PTFE tube during filament loading…"
    0x00020006: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # "Failed to extrude AMS A Slot 1 filament; the extruder may be clogged or the
    # filament may be too thin, causing the extruder to slip."
    0x00020009: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # "AMS A slot 1 pulls filament back to AMS timeout." Pull-BACK, the family an
    # auto-load can grind (see the swap set's exclusion note).
    0x00020011: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # "AMS A slot 1 feeder unit motor has no signal, which may be due to poor
    # contact in the motor connector or a motor fault." Wiring/motor hardware —
    # NOT a feed obstruction, so fresh filament cannot clear it.
    0x00020013: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # "AMS A slot 1 filament status is abnormal, which may be due to a filament
    # breakage inside the AMS."
    0x00020015: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # "AMS A slot 1 the tube inside the AMS is broken, or feed-out hall sensor is
    # faulty and cannot detect the filament."
    0x00020023: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # "AMS A slot 1 failed to rotate the filament spool when pulling filament back to AMS."
    0x00020024: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # -- RFID_READ: the tag could not be read ---------------------------------
    # "Failed to read the filament information from AMS A slot 1. …" — one code
    # word per cause: AMS main board malfunction (0081, the code a commanded read
    # on a tagless slot mints and that can never self-clear), third-party tag
    # (0082), damaged tag (0083), tag at the edge of the reader (0084), tag
    # verification failed (0085), tag cannot rotate due to a jam (0086).
    0x00010081: (_CodeWordRow(_TRAY_ATTR_BYTES, _RFID),),
    0x00010082: (_CodeWordRow(_TRAY_ATTR_BYTES, _RFID),),
    0x00010083: (_CodeWordRow(_TRAY_ATTR_BYTES, _RFID),),
    0x00010084: (_CodeWordRow(_TRAY_ATTR_BYTES, _RFID),),
    0x00010085: (_CodeWordRow(_TRAY_ATTR_BYTES, _RFID),),
    0x00010086: (_CodeWordRow(_TRAY_ATTR_BYTES, _RFID),),
    # "The RFID-tag on AMS A Slot 1 cannot be identified."
    0x00020057: (_CodeWordRow(_TRAY_ATTR_BYTES, _RFID),),
    # "RFID cannot be read because of a hardware or structural error." Carried on
    # the RFID submodule attr, which names no tray — so ``slot`` decodes to None.
    0x00030003: (_CodeWordRow(_RFID_ATTR_BYTES, _RFID),),
    # -- INFORMATIONAL: a notice or a precursor, never a trigger --------------
    # "AMS A slot 1 feed resistance is too high. Please reduce spool rotation
    # resistance and avoid over-bent or over-long filament tubes." Observed
    # 2026-07-20 07:48 on 009-H2S ~5 min BEFORE the 8010 that actually wedged the
    # change — one incident is not a lead-time proof, and the 8010 always follows
    # and IS the trigger, so acting on this would only widen the surface.
    0x00020025: (_CodeWordRow(_TRAY_ATTR_BYTES, _INFO),),
    # "AMS A Slot 1 filament has run out. Please wait while old filament is
    # purged." An in-progress notice, not an ask — the firmware follows it with the
    # 0x00020001 demand or the 0x00030002 auto-switch, whichever way the purge lands.
    0x00030001: (_CodeWordRow(_TRAY_ATTR_BYTES, _INFO),),
    # "Checking the filament location of all AMS slots, please wait."
    0x00030007: (_CodeWordRow(_TRAY_ATTR_BYTES, _INFO),),
}


# --- The EXTERNAL-holder code-word table -----------------------------------
# The SAME code words the AMS table above classifies mean something else entirely
# under an external unit byte, so the holder gets its own table rather than
# borrowing rows that were read from AMS-unit catalog text. 003-H2S 2026-08-11 is
# the proof: ``07FF_2000_0002_0002`` ("External filament is missing") fell through
# the AMS table's deliberate tray-attr ``None`` for 0x00020002 and classified as
# nothing at all, so the honest firmware demand was invisible while the follow-up
# ``07FF_8006`` routed the print into the AMS jam machine.
#
# Scoped by the SAME submodule byte the AMS rows use: every row below was read from
# the ``…2000…`` form, i.e. the holder presents as its unit's first "tray", so
# :data:`_TRAY_ATTR_BYTES` is the scope and no second literal for 0x20 is minted.
# The scoping is load-bearing, not decoration — the external unit byte also carries
# whole families this table must NOT claim (``07FF_8000_0002_0002`` is "The position
# of left hotend is abnormal during printing", ``07FF_6000_0002_0001`` is "External
# spool may be tangled or jammed"), and they keep classifying ``None``.
#
# Catalog texts quoted verbatim per row (``app/data/hms_error_text_en.json.gz``);
# the 0xFF variants are quoted, the 0xFE ones carry the same sentence naming the
# left extruder, and the 18FE/18FF (AMS-HT) forms are byte-identical to the 07 ones.
_EXTERNAL_CODE_WORD_TAXONOMY: dict[int, tuple[_CodeWordRow, ...]] = {
    # -- RUNOUT_EXTERNAL: the holder has nothing to feed ----------------------
    # "External filament has run out; please load a new filament."
    0x00020001: (_CodeWordRow(_TRAY_ATTR_BYTES, AmsFaultClass.RUNOUT_EXTERNAL),),
    # "External filament is missing; please load a new filament." (0xFE: "No
    # filament was detected in the left extruder from the external spool; please
    # load the new filament.") — THE 003-H2S code. It is the holder's twin of the
    # AMS "slot is empty" ask, and unlike that one it HAS a farm consumer: the
    # external lane holds the print and guides the operator to the holder.
    0x00020002: (_CodeWordRow(_TRAY_ATTR_BYTES, AmsFaultClass.RUNOUT_EXTERNAL),),
    # -- PHYSICAL_FAULT: hands at the printer, never a swap -------------------
    # "Filament remains were detected in the PTFE tube between the Auxiliary
    # Extruder and the Toolhead. Please refer to the Wiki for removal instructions."
    0x00020003: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # "Please pull the external filament from the extruder."
    0x00020004: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # "Auxiliary extruder feeding failed, possibly due to a clogged filament tube or
    # worn filament, causing the extruder to slip. Please remove the filament, clear
    # the tube, trim the worn section, and try again."
    0x00020009: (_CodeWordRow(_TRAY_ATTR_BYTES, _PHYSICAL),),
    # -- INFORMATIONAL --------------------------------------------------------
    # "Flushing the remaining filament between the Auxiliary Extruder and the
    # Toolhead. Please wait." An in-progress notice; no farm action.
    0x00030007: (_CodeWordRow(_TRAY_ATTR_BYTES, _INFO),),
}


@dataclass(frozen=True)
class _ShortRow:
    """One classification in the lossy ``MMMM_CCCC`` short-code lane.

    ``external`` marks a row whose modules are the EXTERNAL SPOOL HOLDER's — the
    reason the mixed AMS+external families below are split in two: one fault text,
    two different pieces of hardware, and only one of them has a tray to swap.
    """

    fault_class: AmsFaultClass
    extruder_side: bool = False
    external: bool = False


_AMS_UNITS: tuple[str, ...] = ("0700", "0701", "0702", "0703", "0704", "0705", "0706", "0707")
_AMS_HT_UNITS: tuple[str, ...] = ("1800", "1801", "1802")
_AMS_LITE_UNITS: tuple[str, ...] = ("1200", "1201", "1202", "1203")
# The external spool holder's module prefixes under the AMS module class — the short
# form of :data:`_EXTERNAL_UNIT_BYTES`. BOTH sides: 07FF is the main/right holder and
# 07FE the second one on dual-nozzle hardware, which carries the same catalog
# sentences naming the left extruder. One origin — every external row spells the
# family with this tuple, never a literal pair.
_EXTERNAL_SPOOL: tuple[str, ...] = ("07FF", "07FE")

# --- The print_error short-code table --------------------------------------
# ``0700_0001`` is deliberately ABSENT and must never be added: the same low-16
# word appears under the slot-attributed runout attr family 0700_2X00_0002_0001,
# so a short-code match would route runouts into the jam-swap machine (doctrine
# rule 9). Telling the two apart needs the attr-aware code-word lane above, which
# is exactly where that decision lives. On H2C the same short code means "A new AMS
# detected" — a second reason a bare match is meaningless.
_SHORT_TAXONOMY_ROWS: tuple[tuple[tuple[str, ...], str, _ShortRow], ...] = (
    # -- MECHANICAL_FEED ------------------------------------------------------
    # "The AMS assist motor is overloaded. This could be due to entangled filament
    # or a stuck spool." / AMS-HT + AMS lite equivalents ("spool or filament may be
    # stuck"). All carry stuck-spool semantics and all PAUSE the print, so a false
    # positive cannot fire on a healthy print (acting requires a PAUSE anyway).
    (
        (*_AMS_UNITS, *_AMS_HT_UNITS, *_AMS_LITE_UNITS, "12FF"),
        "8010",
        _ShortRow(_MECHANICAL),
    ),
    # "The extrusion motor is overloaded, please check the Assistant for details."
    # The MAIN extruder, not the AMS assist motor (004-H2S 2026-07-17 sat PAUSEd
    # ~2h40m because this code was outside the trigger set). A swap still helps,
    # but the extruder is the common factor on a re-fault.
    (("0300",), "801E", _ShortRow(_MECHANICAL, extruder_side=True)),
    # The send-out / feed-into-extruder families. Classified from the start (WS2a)
    # and swap triggers since the 2026-08-09 operator-ratified widening (WS2b) —
    # the fault they name is the same obstruction the 8010 family names, one step
    # further along the path.
    #
    # SPLIT AMS-vs-EXTERNAL (003-H2S 2026-08-11): one fault text, two different
    # pieces of hardware. The AMS rows keep feeding the swap machine; the holder's
    # rows carry ``external`` so recovery routes them to their own escalation — there
    # is no AMS to unload, no sibling tray to swap to and no slot to blame, and the
    # incident that proved it invented one, escalated ``jammed_tray_unresolved`` and
    # quarantined the printer for "AMS hardware".
    # "The AMS failed to send out filament. You can clip the end of your filament
    # flat, and reinsert…"
    ((*_AMS_UNITS,), "8005", _ShortRow(_MECHANICAL)),
    # external: "Failed to feed the filament outside the AMS. Please clip the end of
    # the filament flat and check to see if the spool is stuck."
    (_EXTERNAL_SPOOL, "8005", _ShortRow(_MECHANICAL, external=True)),
    # "Unable to feed filament into the extruder. This could be due to an entangled
    # filament or a stuck spool…"
    ((*_AMS_UNITS,), "8006", _ShortRow(_MECHANICAL)),
    # external: "Please feed filament into the PTFE tube until it can not be pushed
    # any farther." — the code standing on 003-H2S at the 21:45 PAUSE.
    (_EXTERNAL_SPOOL, "8006", _ShortRow(_MECHANICAL, external=True)),
    # "Failed to feed filament to the extruder. Check the Assistant for troubleshooting."
    (("0700",), "8028", _ShortRow(_MECHANICAL)),
    (_EXTERNAL_SPOOL, "8028", _ShortRow(_MECHANICAL, external=True)),
    # C006's catalog text is BYTE-IDENTICAL to 8006's ("Please feed filament into the
    # PTFE tube until it can not be pushed any farther.") — the firmware raises the
    # C-form as the interactive prompt of the same ask. Twins share a lane.
    (_EXTERNAL_SPOOL, "C006", _ShortRow(_MECHANICAL, external=True)),
    # -- RUNOUT: an AMS slot ran dry and the print is HELD --------------------
    # "AMS filament ran out. Please insert a new filament into the same AMS slot."
    # / printer-side "Filament ran out. Please load new filament."
    ((*_AMS_UNITS,), "8011", _ShortRow(AmsFaultClass.RUNOUT)),
    (("0300",), "8004", _ShortRow(AmsFaultClass.RUNOUT)),
    # -- RUNOUT_EXTERNAL: the spool holder ran dry — no slot, no swap ---------
    # "External filament has run out; please load a new filament." / per-side on
    # dual-nozzle H2 ("connected to the left / right extruder") / "The filament on
    # external spool has run out…". Never spent-stamps (no AMS slot to attribute)
    # and never swaps (there is no sibling tray to swap to).
    ((*_EXTERNAL_SPOOL, "18FE", "18FF"), "8011", _ShortRow(AmsFaultClass.RUNOUT_EXTERNAL, external=True)),
    # The printer-module form of the same statement — it names the external spool in
    # so many words, so it is external whatever module reports it.
    (("0300",), "8015", _ShortRow(AmsFaultClass.RUNOUT_EXTERNAL, external=True)),
    # -- PHYSICAL_FAULT: needs physical work — never auto-recovered -----------
    # "Failed to pull out the filament from the extruder. This might be caused by
    # clogged extruder or filament broken inside the extruder."
    ((*_AMS_UNITS,), "8003", _ShortRow(_PHYSICAL)),
    # external: "Please pull out the filament on the spool holder. If this message
    # persists, please check to see if there is filament broken in the extruder."
    (_EXTERNAL_SPOOL, "8003", _ShortRow(_PHYSICAL, external=True)),
    # "AMS failed to pull back filament. This could be due to a stuck spool or the
    # end of the filament being stuck in the path."
    ((*_AMS_UNITS,), "8004", _ShortRow(_PHYSICAL)),
    # external: "Failed to pull back the filament from the toolhead to AMS. Please
    # check whether the filament or the spool is stuck."
    (_EXTERNAL_SPOOL, "8004", _ShortRow(_PHYSICAL, external=True)),
    # "Extruding filament failed. The extruder might be clogged."
    ((*_AMS_UNITS,), "8007", _ShortRow(_PHYSICAL)),
    # "Timeout purging old filament: Please check if the filament is stuck or the
    # extruder is clogged."
    (("0700",), "8013", _ShortRow(_PHYSICAL)),
    # "The extruder is not extruding normally; please refer to the Assistant…"
    (("0700",), "8016", _ShortRow(_PHYSICAL)),
    # The clog family. A swap cannot fix a clog — the pause-stall watchdog
    # escalates these instead. "Filament extrusion error…" / "The extrusion
    # resistance is abnormal. The extruder may be clogged…" / "The nozzle is
    # clogged with filament…" / "The nozzle is clogged."
    (("0300",), "801A", _ShortRow(_PHYSICAL)),
    (("0300",), "801C", _ShortRow(_PHYSICAL)),
    (("0300",), "8016", _ShortRow(_PHYSICAL)),
    (("0300",), "4006", _ShortRow(_PHYSICAL)),
    # The manual-clearing asks the firmware raises for the external spool path:
    # "Please manually and slowly pull out the filament from the extruder…" /
    # "Press the black PTFE tube coupler and unplug the PTFE tube…"
    (_EXTERNAL_SPOOL, "C011", _ShortRow(_PHYSICAL, external=True)),
    (_EXTERNAL_SPOOL, "C012", _ShortRow(_PHYSICAL, external=True)),
    # -- RFID_READ ------------------------------------------------------------
    # "Failed to read the filament information." Names the AMS unit but NO slot.
    ((*_AMS_UNITS,), "4025", _ShortRow(_RFID)),
    # -- INFORMATIONAL --------------------------------------------------------
    # The short form of the 0x00020025 feed-resistance precursor (no device_error
    # entry of its own — it reaches consumers only as the lossy short of
    # 07xx_2X00_0002_0025). Classified so the precursor can never be mistaken for
    # a swap trigger; see the code-word row for the evidence.
    ((*_AMS_UNITS,), "0025", _ShortRow(_INFO)),
)


def _build_short_taxonomy() -> dict[str, _ShortRow]:
    """Expand the family rows into the flat short-code table.

    Raises on a duplicate key: two rows claiming one short code would mean the
    taxonomy silently holds two opinions about the same fault.
    """
    table: dict[str, _ShortRow] = {}
    for modules, suffix, row in _SHORT_TAXONOMY_ROWS:
        for module in modules:
            short = f"{module}_{suffix}"
            if short in table:
                raise ValueError(f"duplicate short code in the AMS fault taxonomy: {short}")
            table[short] = row
    return table


_SHORT_CODE_TAXONOMY: dict[str, _ShortRow] = _build_short_taxonomy()


def _shorts_where(predicate: Callable[[_ShortRow], bool]) -> frozenset[str]:
    """The short codes whose taxonomy row satisfies ``predicate``."""
    return frozenset(short for short, row in _SHORT_CODE_TAXONOMY.items() if predicate(row))


# Derived once at import — the consumer-facing sets. Each is a VIEW of the table
# above, never a second literal (doctrine invariant 1).
_RUNOUT_SHORTS: frozenset[str] = _shorts_where(lambda r: r.fault_class is AmsFaultClass.RUNOUT)
_RUNOUT_EXTERNAL_SHORTS: frozenset[str] = _shorts_where(lambda r: r.fault_class is AmsFaultClass.RUNOUT_EXTERNAL)
_MECHANICAL_FEED_SHORTS: frozenset[str] = _shorts_where(lambda r: r.fault_class is _MECHANICAL)
_EXTRUDER_SIDE_SHORTS: frozenset[str] = _shorts_where(lambda r: r.extruder_side)


def runout_short_codes() -> frozenset[str]:
    """AMS-slot UNRESCUED runout short codes — "the slot ran dry and the print is HELD".

    A runout the AMS backup rescued raises NONE of these (it raises the
    slot-attributed auto-switch instead, :data:`_RUNOUT_AUTO_SWITCH_SPENT_CODE32`),
    which is why this set is the unrescued vocabulary only. The external-spool
    runouts are a SEPARATE set (:func:`runout_external_short_codes`) — they name no
    AMS slot, so nothing that resolves a tray may consume them.
    """
    return _RUNOUT_SHORTS


def runout_external_short_codes() -> frozenset[str]:
    """EXTERNAL spool-holder runout short codes — no AMS slot, no sibling to swap to."""
    return _RUNOUT_EXTERNAL_SHORTS


def mechanical_feed_short_codes() -> frozenset[str]:
    """Feed faults an obstruction or a slipping path causes — fresh filament can clear them.

    Since the 2026-08-09 operator-ratified widening (WS2b) this IS the jam-swap
    machine's trigger vocabulary: ``spool_recovery`` derives its two trigger sets by
    splitting this class on :func:`extruder_side_short_codes`, with no second marker
    in between. The WS2a ``legacy_swap`` pin that held the machine at the 8010/801E
    subset was scaffolding for a behavior-neutral relocation and is deleted — a
    marker whose only job is "do not act on the classification yet" cannot outlive
    the wave that acts on it.
    """
    return _MECHANICAL_FEED_SHORTS


def extruder_side_short_codes() -> frozenset[str]:
    """Faults whose common factor is the EXTRUDER, not the spool.

    A re-fault after a swap must not penalize the replacement roll — the extruder
    is what both faults share.
    """
    return _EXTRUDER_SIDE_SHORTS


# The single origin of the unrescued-runout vocabulary, consumed by the runout
# hook, the recovery trigger set and the hold predicate below. Defined AS the
# taxonomy's own view so the constant and the classifier can never drift.
RUNOUT_HMS_CODES: frozenset[str] = runout_short_codes()


def classify_ams_fault(attr: int, code: int) -> ClassifiedAmsFault | None:
    """Classify an ``hms[]`` fault from its raw ``attr`` + 32-bit ``code`` word.

    Returns ``None`` when the fault is not AMS-fault-actionable — a non-AMS module,
    an unclassified code word, or a code word whose meaning under THIS attr's
    submodule is owned by a dedicated decoder (the runout demand and the auto-switch
    spent evidence; see the table's header). ``None`` routes the fault to the generic
    notify lane; it never means "benign".

    The AMS-UNIT byte picks the TABLE. An external spool holder (
    :data:`_EXTERNAL_UNIT_BYTES`) is not an AMS unit and its code words say something
    else entirely, so it is read from :data:`_EXTERNAL_CODE_WORD_TAXONOMY` alone —
    borrowing an AMS row for it is what made ``07FF_2000_0002_0002`` invisible on
    003-H2S. Every external verdict carries ``external=True`` and ``slot=None`` (a
    holder has no slot, which :func:`ams_slot_from_attr` already enforces).
    """
    if (attr >> 24) & 0xFF not in _AMS_MODULES:
        return None
    external = (attr >> 16) & 0xFF in _EXTERNAL_UNIT_BYTES
    rows = (_EXTERNAL_CODE_WORD_TAXONOMY if external else _CODE_WORD_TAXONOMY).get(code)
    if not rows:
        return None
    attr_byte = (attr >> 8) & 0xFF
    for row in rows:
        if attr_byte in row.attr_bytes:
            return ClassifiedAmsFault(
                fault_class=row.fault_class,
                extruder_side=row.extruder_side,
                slot=ams_slot_from_attr(attr),
                external=external,
            )
    return None


def classify_short_code(short: str) -> ClassifiedAmsFault | None:
    """Classify a ``MMMM_CCCC`` short code (the ``print_error`` lane).

    Necessarily coarser than :func:`classify_ams_fault`: the short form has already
    discarded the attr low byte, so ``slot`` is always ``None`` and any code word
    whose meaning depends on its submodule is unclassifiable here (which is why
    ``0700_0001`` has no row — see the table's header).

    ``external`` survives the lossy form intact: the short code keeps the whole
    MODULE group (``07FF``/``07FE`` vs ``0700``…), which is the very field that says
    "spool holder, not AMS" — so the split families classify to the right hardware
    here even though the slot is gone.
    """
    row = _SHORT_CODE_TAXONOMY.get(short.upper())
    if row is None:
        return None
    return ClassifiedAmsFault(
        fault_class=row.fault_class,
        extruder_side=row.extruder_side,
        slot=None,
        external=row.external,
    )


def classify_hms_entry(e) -> ClassifiedAmsFault | None:
    """Classify ONE live ``state.hms_errors`` entry, whichever lane it arrived on.

    ``state.hms_errors`` is a MIXED list — the MQTT parser appends entries from two
    wire shapes and they carry their identity differently:

    * the ``hms[]`` array lane — ``attr`` is the full 32-bit attribute (module byte,
      submodule byte, slot byte) and ``code`` is the 32-bit code WORD, so
      :func:`classify_ams_fault` applies and can name the slot;
    * the ``print_error`` lane — one 32-bit integer split into module/error, stored
      as ``attr`` = the whole value and ``code`` = the low 16 bits. Its code word is
      not a code word at all, so only :func:`classify_short_code` applies and the
      verdict can never carry a slot.

    Trying the attr-aware lane FIRST is what makes this safe: every code word whose
    meaning is submodule-scoped (the runout demand, the auto-switch spent evidence,
    the empty-slot ask) is decided there — including the deliberate ``None``s — and
    the short lane is only reached for entries the code-word table does not claim.
    That ordering is why no ``print_error``-shaped fallback can smuggle a runout
    demand into the swap machine (doctrine rule 9): the short table has no
    ``0700_0001`` row at all.

    Malformed entries classify ``None`` rather than raising — this runs inside the
    MQTT status callback (invariant 10).
    """
    try:
        attr = int(getattr(e, "attr", 0) or 0)
        verdict = classify_ams_fault(attr, _code_word(getattr(e, "code", 0)))
        if verdict is not None:
            return verdict
        return classify_short_code(hms_short_code(attr, getattr(e, "code", 0)))
    except (TypeError, ValueError, AttributeError):
        return None


def _code_word(code: int | str) -> int:
    """Parse an HMSError ``code`` (int or hex string like ``"0x20001"``) to its full
    32-bit int — the form :func:`runout_slot_from_hms` expects."""
    if isinstance(code, str):
        return int(code.replace("0x", ""), 16) if code else 0
    return int(code or 0)


def current_runout_demand(hms_list) -> tuple[int, int] | None:
    """The ``(ams_id, tray_id)`` the firmware is CURRENTLY demanding filament in.

    The canonical answer to "which slot does the printer want refilled" — the ONE
    decoder every runout-guidance consumer reads (escalation text, hourly reminders,
    the refill auto-resume gate, the load-route 409). 006-H2S 2026-07-26: the
    escalation named "AMS A slot 1" because it took the slot from the DISPATCH
    MAPPING while the firmware's own demand for slot 3 was standing right there in
    ``hms_errors`` as ``0700_2200_0002_0001``; the operator refilled the wrong slot
    and the print sat 12 h.

    Only the DEMAND family counts (:data:`_RUNOUT_DEMAND_CODE32` — see its comment
    for the per-code catalog evidence): a "please wait while purging", a
    purge-abnormal runout and an "automatically switched" INFO all name a slot but
    ask for nothing. The bare ``07xx_8011`` "insert into the same AMS slot" runout
    names NO slot and so never matches — callers fall back to their own attribution.

    LAST match wins: the firmware APPENDS newer faults, so a demand that MOVED (a
    second roll runs out, or the operator refilled the wrong slot) is the later
    entry. Proven from the incident's own list order — at 01:23 the list was
    [slot-1 auto-switched, slot-3 demand, bare 8011] → slot 3; at 13:51 a slot-2
    demand had been appended → slot 2.

    EXTERNAL demands are deliberately NOT decoded here, even though the holder's
    runout shares the ``0x00020001`` code word: an external holder has no AMS slot,
    so :func:`ams_slot_from_attr` fails closed on its unit byte and this decoder
    correctly answers ``None``. Nothing is missing — the external lane does not want
    a slot. It watches CLASS MEMBERSHIP instead (``RUNOUT_EXTERNAL`` codes appearing
    and disappearing on the wire, ``spool_recovery.note_demand_watch``), which is the
    same auto-resume spawn the AMS lane reaches through a demand edge.

    Pure decode over any HMSError-shaped sequence (``.attr`` + ``.code``, the same
    shapes :func:`runout_slot_from_hms` consumers pass). A malformed entry is
    skipped, never raised — this runs inside MQTT callbacks (invariant 10).
    """
    demand: tuple[int, int] | None = None
    for e in hms_list or []:
        try:
            if _code_word(getattr(e, "code", 0)) not in _RUNOUT_DEMAND_CODE32:
                continue
            slot = ams_slot_from_attr(int(getattr(e, "attr", 0) or 0))
        except (TypeError, ValueError):  # a malformed HMS entry must not break the decode
            continue
        if slot is not None:
            demand = slot
    return demand


def runout_hold_active(state) -> bool:
    """True when the printer is PAUSEd holding for a same-slot filament refill.

    The shared predicate behind every runout-hold decision (guidance refresh, refill
    auto-resume, the ``/ams/load`` 409 gate) so they can never disagree about
    whether the printer is in the state where the AMS executes no filament change.
    Two legs, both required:

    * live ``gcode_state == "PAUSE"``, and
    * a runout code standing in ``hms_errors`` — either the slot-agnostic
      "insert into the SAME slot" family (``RUNOUT_HMS_CODES``) or any
      slot-attributed DEMAND (:func:`current_runout_demand`).

    Both legs are AMS-SLOT vocabulary by design, so an EXTERNAL-holder runout is
    deliberately not a "runout hold" here: this predicate exists to gate AMS writes
    (the ``/ams/load`` 409, the refill auto-resume's slot reasoning) and a holder
    fault neither blocks nor names one. The external lane holds through its own
    incident and its own class-membership watch instead.

    Wire-proven in this state (006-H2S 2026-07-26, matching the 2026-07-19
    cross-slot finding): a load command produces NO AMS motion, LATCHES in firmware,
    and resurfaces at the operator's eventual resume as a bogus demand for the
    latched slot — 12 h later in the incident.

    Fails closed (False) on a malformed/absent state — a predicate that errors must
    never block an operator's load.
    """
    try:
        if (getattr(state, "state", None) or "") != "PAUSE":
            return False
        hms_list = getattr(state, "hms_errors", None) or []
        if current_runout_demand(hms_list) is not None:
            return True

        for e in hms_list:
            try:
                if hms_short_code(e.attr, e.code) in RUNOUT_HMS_CODES:
                    return True
            except Exception:  # noqa: BLE001 — a malformed HMS entry must not break the predicate
                continue
        return False
    except Exception:  # noqa: BLE001 — a gate predicate must never raise into a callback/route
        return False


def hms_error_payload(e) -> dict:
    """Serialize an HMSError to the API/WS wire dict.

    Enriches the raw firmware fields (code/attr/module/severity/actions/job_id/
    full_code) with the canonical ``short_code``, the human-readable
    ``description`` and the ``wiki_url``. The description prefers the lossless
    ``full_code`` against the vendored catalog and falls back to the legacy
    2-group table (None when neither has it — the frontend then renders an
    explicit "unknown code" fallback rather than dropping the error). The wiki
    URL is the vendored per-code deep link when available, else the HMS landing
    page. Used by BOTH the REST route and the WebSocket ``printer_state_to_dict``
    so the two payloads never drift.

    A per-slot runout fault additionally carries ``runout_slot`` (``{"ams_id",
    "tray_id"}``) decoded from the firmware attr — the single enrichment site that
    feeds the WS/REST per-slot "ran out" badge. Absent on every non-runout code, so
    the base payload shape is unchanged for those.
    """
    short_code = hms_short_code(e.attr, e.code)
    description = lookup_full_code(e.full_code) or get_error_description(short_code)
    wiki_path = lookup_wiki_path(e.full_code)
    wiki_url = (HMS_WIKI_URL_ORIGIN + wiki_path) if wiki_path else HMS_WIKI_URL
    payload = {
        "code": e.code,
        "attr": e.attr,
        "module": e.module,
        "severity": e.severity,
        "actions": e.actions,
        "job_id": e.job_id,
        "full_code": e.full_code,
        "short_code": short_code,
        "description": description,
        "wiki_url": wiki_url,
    }
    runout_slot = runout_slot_from_hms(int(e.attr or 0), _code_word(e.code))
    if runout_slot is not None:
        payload["runout_slot"] = {"ams_id": runout_slot[0], "tray_id": runout_slot[1]}
    return payload
