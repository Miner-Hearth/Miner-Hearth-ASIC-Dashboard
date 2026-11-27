"""Miner Hearth v1.12 - miner fault flags, plain-English explainers, fault history,
log parsing, diagnostic redaction and HTTP Digest auth (stdlib only).

Only codes and fields that are documented by the firmware vendors (or their
reference guides) are mapped. Anything else is shown verbatim as "unmapped".
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from nk_fault_codes import BRAIINS_ERROR_CODES

# ---------------------------------------------------------------------------
# Guides (all checked to open in a browser; Bitmain Zendesk blocks scripts but loads in browsers)
# ---------------------------------------------------------------------------

GUIDES: dict[str, tuple[str, str]] = {
    "bitaxe_fault": ("bitaxe.de - Bitaxe hardware fault diagnosis", "https://bitaxe.de/en/bitaxe-hardware-fault-diagnosis"),
    "bitaxe_troubleshooter": ("bitaxe.de - Miner troubleshooter", "https://bitaxe.de/en/miner-troubleshooter"),
    "bitaxe_monitoring": ("bitaxe.de - Bitaxe monitoring (reset reasons)", "https://bitaxe.de/en/bitaxe-monitoring"),
    "axeos_explained": ("bitaxe.de - AxeOS explained", "https://bitaxe.de/en/axeos-explained"),
    "axeos_api": ("D-Central - AxeOS API reference", "https://d-central.tech/axeos-api-reference"),
    "osmu_faq": ("OSMU wiki - FAQ", "https://osmu.wiki/tips/faq/"),
    "nerd_repo": ("NerdQAxe+ firmware (docs/API.md)", "https://github.com/shufps/ESP-Miner-NerdQAxePlus"),
    "nerd_flasher": ("bitaxe.de - NerdOCTAXE flasher & notes", "https://flasher.bitaxe.de/en/nerdoctaxe"),
    "braiins_codes": ("Braiins Academy - Technical resources (error codes)", "https://academy.braiins.com/braiins-os/technical-resources"),
    "braiins_trouble": ("Braiins Academy - Troubleshooting", "https://academy.braiins.com/braiins-os/faqs/troubleshooting"),
    "braiins_features": ("Braiins Academy - Advanced features (overheat recovery)", "https://academy.braiins.com/braiins-os/advanced-features"),
    "bitmain_19": ("Bitmain Support - Common problems (19 series)", "https://support.bitmain.com/hc/en-us/articles/4406645619097-Common-Problems-and-Solutions-for-ANTMINER-19-series"),
    "bitmain_restart": ("Bitmain Support - Common miner restart scenarios", "https://support.bitmain.com/hc/en-us/articles/27245455307033-TIPS-for-Common-Scenario-of-Miners-Restart"),
    "dcentral_kernel": ("D-Central - Antminer S19 kernel log errors", "https://d-central.tech/asic-troubleshooting/antminer-s19-kernel-log-errors/"),
    "dcentral_init": ("D-Central - S19 hashboard init failure", "https://d-central.tech/asic-troubleshooting/antminer-s19-hashboard-init-failure/"),
}


def _e(title: str, meaning: str, cause: str, checks: list[str], guide: str, severity: str = "problem") -> dict[str, Any]:
    return {"title": title, "meaning": meaning, "cause": cause, "checks": checks, "guide": guide, "severity": severity}


CATALOG: dict[str, dict[str, Any]] = {
    # --- AxeOS (Bitaxe / ESP-Miner) -----------------------------------------
    "axeos:overheat_mode": _e(
        "Overheat safe mode",
        "AxeOS reports overheat_mode = 1 (the AxeOS screen says \"Device has overheated - See settings\"). The firmware lowered frequency "
        "and core voltage and set the fan to 100 % manual. It stays in this mode until the flag is cleared.",
        "Chip or voltage-regulator temperature went over the firmware limit: blocked airflow, a stopped fan, a loose heatsink, a hot room, or an overclock that is too high.",
        ["Make sure the fan spins and nothing blocks the intake or exhaust.", "Check the heatsink is seated (thermal pad/paste) and the VR area is not covered.",
         "Lower frequency/voltage if you had overclocked.", "Let it cool (chip under the cooldown limit) before you clear it, then set frequency and fan back to your normal values."],
        "bitaxe_fault"),
    "axeos:power_fault": _e(
        "Power regulator fault",
        "AxeOS reports a power_fault (the AxeOS screen shows it followed by \"Check your Power Supply.\"). The core-voltage regulator latched a fault; "
        "the firmware only clears it at boot, so it stays until the miner restarts.",
        "A weak or failing power supply, a loose or hot DC connector, input voltage sag, or a damaged regulator.",
        ["Use a known-good power supply with the rating your board's maker specifies.", "Reseat the DC plug and look for heat marks.",
         "Restart the miner. If the fault comes straight back, suspect the supply first, then the regulator."],
        "bitaxe_fault"),
    "axeos:hardware_fault": _e(
        "Hardware fault",
        "AxeOS reports a hardware_fault text (shown verbatim above).",
        "Depends on the text; see the guide.",
        ["Read the exact text above and look it up in the guide.", "Power the miner off and check cables, fan and heatsink.", "A restart can clear it if the cause is gone."],
        "bitaxe_fault"),
    "axeos:hardware_fault:fan": _e(
        "Fan control failed",
        "AxeOS reports hardware_fault \"Fan Control Failed\": the automatic fan controller could not read or drive the fan.",
        "Fan unplugged or failed, a broken tach wire, or a fan the board cannot control.",
        ["Check the fan is plugged in and spins freely.", "Try another fan of the same type.", "Restart after fixing; watch temperatures closely until the fan reads RPM again."],
        "bitaxe_fault"),
    "axeos:low_voltage": _e(
        "Low input voltage",
        "Input voltage is below 94.9 % of the board's nominal voltage (AxeOS shows \"Danger: Low Voltage\").",
        "Undersized power supply, long/thin DC cable, or a supply that sags under load.",
        ["Use a supply with enough current headroom.", "Use a shorter or thicker DC cable and a tight connector."],
        "bitaxe_fault", "warn"),
    "axeos:mining_paused": _e(
        "Mining paused",
        "AxeOS reports miningPaused = true: hashing is stopped until it is resumed.",
        "Someone paused it from AxeOS or the API, or the firmware paused it.",
        ["If you did not pause it on purpose, resume it (Clear/Reset button)."],
        "axeos_api", "warn"),
    # Reset reasons (shown as info; only abnormal ones are listed as findings)
    "reset:brownout": _e(
        "Last reboot: brownout",
        "The controller reset because its supply voltage dropped too low.",
        "Weak power supply, loose DC connector, or a voltage dip.",
        ["Check the power supply and connector.", "If it repeats, try another supply."], "bitaxe_monitoring", "info"),
    "reset:watchdog": _e(
        "Last reboot: watchdog",
        "The controller reset because a task or interrupt watchdog fired (the firmware got stuck).",
        "Firmware bug, Wi-Fi/pool trouble that blocked a task, or unstable hardware.",
        ["Update to the latest stable firmware.", "If it repeats, note the time and check pool/Wi-Fi stability."], "bitaxe_monitoring", "info"),
    "reset:panic": _e(
        "Last reboot: crash (panic)",
        "The controller reset after a software exception (panic).",
        "Firmware bug or memory corruption; sometimes unstable power.",
        ["Update to the latest stable firmware.", "Capture the log if it repeats."], "bitaxe_monitoring", "info"),
    "reset:glitch": _e(
        "Last reboot: power glitch",
        "The controller reset after a power glitch was detected.",
        "Unstable supply or connector.", ["Check the power supply and connector."], "bitaxe_monitoring", "info"),
    # --- NerdQAxe+ / NerdOCTAXE ----------------------------------------------
    "nerd:board_error:1": _e("Miner overheated", "NerdQAxe+ boardError 1 (TEMP_FAULT): the ASIC temperature passed the overheat limit, so the firmware shut mining off until a restart.",
                             "Poor airflow, failed fan, or a pump/radiator problem on water-cooled builds.",
                             ["Check airflow, heatsinks and fans (pump/radiator if water cooled).", "Let it cool, then restart."], "nerd_repo"),
    "nerd:board_error:2": _e("Voltage regulator overheated", "NerdQAxe+ boardError 2 (VREG_TEMP_FAULT): the voltage regulator passed its overheat limit; mining is off until a restart.",
                             "Missing VR heatsink or airflow over the regulator area.", ["Check the VR heatsink and airflow.", "Let it cool, then restart."], "nerd_repo"),
    "nerd:board_error:3": _e("Power supply fault", "NerdQAxe+ boardError 3 (PSU_FAULT): the power stage reported a supply fault; mining is off until a restart.",
                             "Power supply, XT60 connector or cable problem, or a supply that cannot deliver enough current.",
                             ["Check the supply, the XT60 connector and the cable.", "Use a supply rated for at least 20 A at 12 V as the vendor recommends.", "Restart."], "nerd_flasher"),
    "nerd:board_error:4": _e("Over-current protection", "NerdQAxe+ boardError 4 (IOUT_OC_FAULT): output over-current protection tripped; mining is off until a restart.",
                             "Frequency/voltage set too high for the board.", ["Lower frequency and/or core voltage.", "Restart."], "nerd_flasher"),
    "nerd:board_error:5": _e("Voltage protection", "NerdQAxe+ boardError 5 (VOUT_FAULT): output voltage protection tripped; mining is off until a restart.",
                             "Core voltage settings outside the safe range.", ["Return frequency/voltage to defaults.", "Restart."], "nerd_flasher"),
    "nerd:board_error:6": _e("Coinbase verify failed", "NerdQAxe+ boardError 6 (COINBASE_VERIFY_FAULT): the miner could not verify the pool's coinbase (payout) data, so it stopped.",
                             "Wrong Stratum host or user (payout address) for the pool.", ["Check the Stratum Host and Stratum User settings.", "Restart."], "nerd_flasher"),
    "nerd:shutdown": _e("Miner shut down", "NerdQAxe+ reports system.shutdown = true: mining is stopped until a restart.",
                        "A protection fault (see boardError) or a manual shutdown.", ["Check the board-error text if shown.", "Restart once the cause is fixed."], "nerd_repo"),
    # --- Stock Bitmain (Antminer) --------------------------------------------
    "bitmain:fan_lost": _e("Fan lost", "A fan reports 0 RPM while others spin (Bitmain logs this as ERROR_FAN_LOST and stops the miner).",
                           "Unplugged or damaged fan cable, a failed fan, or a bad fan socket on the control board.",
                           ["Check the fan connector and cable.", "Swap in a known-good fan.", "If it persists, the control board may need replacing."], "bitmain_19"),
    "bitmain:chain_missing": _e("Hashboard not detected / no chips", "A chain reports 0 chips (Bitmain log: \"Chain[X]: find 0 asic\").",
                                "Loose/failed data cable, power not reaching the board, or a failed hashboard.",
                                ["Power off and reseat that board's data ribbon and power connections.", "Swap boards between slots to see if the fault follows the board."], "bitmain_19"),
    "bitmain:chain_few_chips": _e("Missing chips on a hashboard", "One chain reports fewer chips than the others.",
                                  "Supply or grounding issues, or a damaged hashboard.",
                                  ["Bitmain suggests replacing the PSU and making sure the miner is grounded well.", "If it stays, the board needs repair."], "bitmain_19"),
    "bitmain:chain_bad_chips": _e("Chips reporting errors", "The chain status string (chain_acs) marks chips with 'x' (error) or '-' (missing).",
                                  "Failing chips or a hashboard problem.", ["Note which chain; if the count grows, plan a hashboard repair."], "dcentral_init", "warn"),
    "bitmain:log:fan_lost": _e("Log: ERROR_FAN_LOST", "The miner log reports a lost fan or fan speed too low.", "Fan cable, fan, or control-board socket.",
                               ["Check the fan connector and cable.", "Swap in a known-good fan."], "bitmain_19"),
    "bitmain:log:pic_temp": _e("Log: fail to read pic temp", "The PIC controller on a hashboard could not be read.", "Usually a control board or cable problem.",
                               ["Cross-check the cables.", "Update/re-flash firmware; if it stays, send for repair."], "bitmain_19"),
    "bitmain:log:pic_version": _e("Log: pic version mismatch", "A hashboard's PIC firmware version does not match what the miner firmware expects.",
                                  "Mixed boards or a firmware mismatch.", ["Update firmware; if it stays, send the board for repair."], "dcentral_kernel"),
    "bitmain:log:find_0_asic": _e("Log: find 0 asic", "A chain was powered but no ASIC chips answered.", "Data cable, power, or a failed hashboard.",
                                  ["Reseat the data ribbon and power for that chain.", "Swap boards between slots to isolate the fault."], "dcentral_init"),
    "bitmain:log:power_off_board": _e("Log: will power off hash board", "The firmware switched a hashboard off after a fault.", "Usually follows a chip-count, temperature or power problem on that board.",
                                      ["Read the lines just before this one in the captured log."], "bitmain_restart"),
    "bitmain:log:power_lost": _e("Log: ERROR_POWER_LOST", "The miner lost or could not talk to the power supply.", "PSU, copper-bar contact, power control wire, or a short.",
                                 ["Check the copper bars and the power control wire.", "Try another PSU."], "bitmain_19"),
    "bitmain:log:power_version": _e("Log: get power type version failed", "The miner could not identify the PSU.", "PSU control cable or the PSU itself.",
                                    ["Check the PSU control cable.", "Try another PSU."], "dcentral_kernel"),
    "bitmain:log:soc_init": _e("Log: ERROR_SOC_INIT", "Chip initialization failed during boot.", "Usually a failed hashboard or a short.",
                               ["Do not keep rebooting it; check for a short and plan a repair."], "bitmain_restart"),
    "bitmain:log:eeprom": _e("Log: ERROR_EEPROM_INFO", "A hashboard's EEPROM data could not be read or is invalid.", "Data cable or the board's EEPROM.",
                             ["Reseat the data cable; if it stays, the board needs repair."], "dcentral_kernel"),
    "bitmain:log:temp_high": _e("Log: ERROR_TEMP_TOO_HIGH", "Over-temperature protection stopped the miner.", "Hot intake air, dust, blocked exhaust.",
                                ["Clean the heatsinks.", "Check intake air temperature and exhaust path."], "bitmain_19"),
    "bitmain:log:read_temp": _e("Log: fail to read temp", "A chain's temperature sensor could not be read.", "Sensor, cable or board fault.",
                                ["Reseat the data cable; if only one chain fails, suspect that board."], "dcentral_kernel"),
    # --- AxeOS log lines ------------------------------------------------------
    "axeos:log:asic_not_found": _e("Log: ASIC not found", "AxeOS could not find the ASIC chip(s) at boot (\"ASIC 0 not found\" / \"0 chip(s) detected\").",
                                   "Power/regulator problem, damaged chip, or wrong board/firmware.", ["Check power first, then the board and firmware model."], "bitaxe_fault"),
    "axeos:log:chip_id": _e("Log: CHIP_ID response mismatch", "The ASIC answered with an unexpected chip ID.", "Wrong firmware for the board or a damaged chip.",
                            ["Make sure the firmware matches your board model."], "bitaxe_fault"),
    "axeos:log:high_temp": _e("Log: High temperature", "AxeOS logged \"Danger: High Temperature\".", "Cooling problem.",
                              ["Check fan and heatsink."], "bitaxe_fault", "warn"),
    # --- Braiins status --------------------------------------------------------
    "braiins:status:thermal_pause": _e("Thermal pause", "Braiins OS paused mining because temperatures reached the dangerous level.",
                                       "Cooling problem (fans, coolant flow, hot intake air).",
                                       ["Fix the cooling first.", "Braiins OS can resume by itself after cooldown if overheat recovery is enabled; otherwise resume manually once cool."],
                                       "braiins_features"),
    "braiins:status:paused": _e("Mining paused", "Braiins OS reports mining is paused.", "Paused by a user or by a protection.",
                                ["If you did not pause it, check the error list, then resume."], "braiins_trouble", "warn"),
    "braiins:status:dead_pools": _e("All pools unresponsive", "Braiins OS stopped mining because no configured pool is reachable.",
                                    "Internet outage, DNS failure, pool outage, or mistyped pool URL/port.",
                                    ["Check network/DNS and pool status.", "Add a working backup pool in Pool settings."], "braiins_trouble"),
    "braiins:status:missing_license": _e("Missing Braiins license", "Braiins OS stopped mining because no valid license is active.",
                                         "Trial/license expired or control board mismatch.",
                                         ["Check the Braiins OS License / System page in the miner web UI."], "braiins_trouble"),
    "braiins:status:hardware_error": _e("Braiins hardware error", "Braiins OS stopped mining due to a hardware error.",
                                        "Hashboard, power supply, sensor, or cooling fault.",
                                        ["Check the Braiins error details and kernel/bosminer log."], "braiins_trouble"),
    "braiins:status:tuner_error": _e("Braiins tuner error", "Braiins OS stopped mining because the autotuner encountered an error.",
                                     "Power target / PSU limit error or unstable hashboard tuning.",
                                     ["Check the Braiins tuner status and power supply."], "braiins_trouble"),
    "braiins:status:unsupported_hardware": _e("Unsupported hardware", "Braiins OS reported unsupported hardware during initialization.",
                                              "Hashboard EEPROM mismatch or wrong firmware image.",
                                              ["Check hashboard cables and Braiins hardware compatibility."], "braiins_trouble"),
    "braiins:status:application_unavailable": _e("BOSminer unavailable", "Braiins OS reports the bosminer application is unavailable.",
                                                 "BOSminer service crashed or is restarting.",
                                                 ["Capture logs and restart BOSminer if it stays stopped."], "braiins_trouble"),
    "braiins:status:dps_cooldown": _e("DPS cooldown", "Braiins OS Dynamic Performance Scaling is in cooldown.",
                                      "Power/thermal scaling triggered a temporary cooldown.",
                                      ["Wait for cooldown or lower the power target."], "braiins_features", "warn"),
    "braiins:status:starting_wait": _e("Starting / waiting", "Braiins OS is starting up (cooling down, warming up, or defrosting).",
                                       "Normal startup pre-check or temperature stabilization.",
                                       ["Wait a minute for startup to finish."], "braiins_trouble", "info"),
    "braiins:tuner:chain_error": _e("Hashboard tuner error/disabled", "Braiins CGMiner TUNERSTATUS reports a chain is in Error, Disabled, or Restricted state.",
                                    "Overheating, voltage glitch, or damaged hashboard.",
                                    ["Check per-board temps and error logs in the Braiins UI."], "braiins_trouble"),
    "braiins:cgminer:fan_lost": _e("Fan lost", "Braiins FANS reports 0 RPM on a fan while other fans are spinning.",
                                   "Unplugged, jammed, or failed fan.",
                                   ["Check fan connectors and blades; replace a stalled fan."], "braiins_trouble"),
    "braiins:cgminer:fan_slow": _e("Fan spinning slowly", "A fan is spinning far slower than sibling fans on the same miner.",
                                   "Worn fan bearings, dust buildup, or failing PWM/motor.",
                                   ["Inspect and clean or replace the slow fan before it stalls completely."], "braiins_trouble", "warn"),
    "braiins:cgminer:dev_unhealthy": _e("Hashboard Dead/Sick", "CGMiner DEVS reports a hashboard status is Dead or Sick.",
                                      "Hashboard stopped hashing or lost communication.",
                                      ["Check board temperatures, cables, and logs; restart if cool."], "braiins_trouble"),
    "braiins:cgminer:temp_high": _e("High temperature", "Braiins TEMPS reports dangerous chip or board temperature.",
                                    "Cooling failure, blocked airflow, or excessive power target.",
                                    ["Check fans, intake air, and heatsinks; lower power target."], "braiins_features"),
    "braiins:cgminer:temp_sensor_error": _e("Temperature sensor error", "Braiins TEMPS reports 0 °C on an active chain.",
                                            "Stuck I2C bus, loose data cable, or failed sensor.",
                                            ["Power-cycle the miner and reseat the hashboard data cable."], "braiins_codes"),
    "braiins:cgminer:pools_dead": _e("All pools dead", "CGMiner POOLS reports all configured mining pools are Dead.",
                                     "Network/DNS outage or unreachable pool endpoints.",
                                     ["Verify network connection and pool settings."], "braiins_trouble"),
    # --- Additional AxeOS alerts (UI banners & inline alerts) -----------------
    "axeos:high_temp": _e("Danger: High temperature", "ASIC temperature is >= 70 °C (AxeOS shows 'Danger: High Temperature' and throttles at 75 °C).",
                          "Blocked airflow, stalled fan, dry paste, or too high clock/voltage.",
                          ["Check fan RPM and heatsink contact.", "Lower frequency/voltage or improve cooling."], "bitaxe_fault"),
    "axeos:high_vr_temp": _e("Danger: High VR temperature", "Voltage regulator temperature is >= 105 °C (AxeOS shows 'Danger: High Temperature').",
                             "Overloaded regulator, blocked VR airflow, or excessive core voltage.",
                             ["Improve airflow over the voltage regulator.", "Lower core voltage and frequency."], "bitaxe_fault"),
    "axeos:vr_temp_warn": _e("High VR temperature", "Voltage regulator temperature is elevated (>= 94 °C).",
                             "High power draw or limited VR airflow.",
                             ["Check airflow across the VR heatsink; consider lowering core voltage."], "bitaxe_fault", "warn"),
    "axeos:fan_stalled": _e("Fan stalled (0 RPM)", "AxeOS reports 0 RPM while fan speed is > 0 %.",
                            "Unplugged fan, broken tach wire, or jammed/dead fan.",
                            ["Verify the fan is spinning and the 4-pin PWM/tach plug is seated."], "bitaxe_fault"),
    "axeos:temp_sensor_error": _e("Temperature sensor error", "AxeOS reports temperature <= 0 °C while the miner is active.",
                                  "Failed temp sensor, I2C glitch, or loose heatsink sensor.",
                                  ["Power-cycle the unit; check sensor connections if it persists."], "bitaxe_fault"),
    "axeos:pool_disconnected": _e("Stratum pool disconnected", "AxeOS reports poolConnectionInfo = 'Not Connected' (no active pool connection).",
                                  "Wi-Fi/internet loss, DNS failure, or unreachable pool URL/port.",
                                  ["Check primary and fallback stratum host, port, and worker format.", "Check router/firewall DNS."], "axeos_explained"),
    "axeos:fallback_stratum": _e("Mining on fallback pool", "AxeOS switched to the fallback stratum pool because the primary pool was unreachable.",
                                 "Primary pool outage, DNS glitch, or wrong primary pool config.",
                                 ["Check the primary pool settings and connectivity."], "axeos_explained", "warn"),
    "axeos:pool_diff_low": _e("Pool difficulty too low", "Pool difficulty is lower than ASIC minimum difficulty (AxeOS shows POOL_DIFFICULTY_LOW).",
                               "Pool starting difficulty is too low for this miner.",
                               ["Raise starting difficulty on the pool or use a port matching your hashrate."], "axeos_explained", "warn"),
    "axeos:coinbase_payout": _e("Payout address mismatch", "Coinbase outputs do not match your configured payout address (NO_MINING_REWARD / NOT_SOLO_MINING).",
                                "Pool is not paying your address or takes > 5% fee / non-solo payout.",
                                ["Verify your stratum user/payout address and pool type if solo mining."], "axeos_explained", "warn"),
    "axeos:frequency_low": _e("Frequency below minimum", "Operating frequency is below the board's normal minimum option (FREQUENCY_LOW).",
                              "Previous thermal throttle, manual setting, or safe-boot fallback.",
                              ["Check temperatures first, then set frequency back to normal."], "axeos_explained", "warn"),
    "axeos:wifi_disconnected": _e("Wi-Fi disconnected", "AxeOS reports Wi-Fi status is not connected.",
                                  "Weak signal, router reboot, or wrong Wi-Fi credentials.",
                                  ["Check Wi-Fi signal and router status."], "axeos_explained"),
    "axeos:wifi_weak": _e("Weak Wi-Fi signal", "Wi-Fi RSSI is <= -85 dBm, which can cause stale shares and pool drops.",
                          "Miner is too far from the access point or blocked by metal/walls.",
                          ["Move the miner closer to the router or add a closer access point."], "axeos_explained", "warn"),
    "axeos:zero_hashrate": _e("Zero hashrate", "Miner is online and not paused, but reports 0 GH/s after startup.",
                              "Pool disconnected, ASIC failed to initialize, or power/clock issue.",
                              ["Check pool connection, ASIC/VR temps, and logs; restart if cool."], "bitaxe_fault"),
    "axeos:low_hashrate": _e("Low hashrate", "Hashrate is less than half of expectedHashrate.",
                             "Dead ASIC chip/domain, thermal throttling, or severe undervoltage.",
                             ["Check hashrateMonitor/chips, temperatures, and input voltage."], "bitaxe_fault", "warn"),
    "axeos:dead_asic": _e("Dead ASIC chip", "One or more ASICs in hashrateMonitor report 0 GH/s while the miner is hashing.",
                          "Failed ASIC, solder joint, or insufficient core voltage.",
                          ["Check core voltage, chip temps, and logs; restart once cool."], "bitaxe_fault"),
    "axeos:high_error_pct": _e("High hardware error rate", "ASIC errorPercentage is >= 5 %.",
                               "Frequency too high for current core voltage, or overheating.",
                               ["Lower frequency or raise core voltage slightly; check cooling."], "bitaxe_fault", "warn"),
    # --- Additional NerdQAxe+ / NerdOCTAxe alerts ----------------------------
    "nerd:fan_stalled": _e("Fan stalled (0 RPM)", "NerdQAxe+ reports 0 RPM while fan speed >= 35 %.",
                           "Fan unplugged, blocked, or failed tach wire.",
                           ["Check the fan connector and make sure blades spin freely."], "nerd_repo"),
    "nerd:fan_low_rpm": _e("Fan duty too low for RPM", "NerdQAxe+ reports 0 RPM at fan speed < 35 % (FAN_LOW_RPM_HINT).",
                           "Fan speed duty cycle is below the fan's minimum spin threshold.",
                           ["Raise minimum fan speed or check fan curve."], "nerd_repo", "warn"),
    "nerd:stratum_disconnected": _e("Stratum not connected", "NerdQAxe+ stratum pool reports connected = false (red Stratum tile).",
                                    "Internet/DNS outage or unreachable pool URL/port.",
                                    ["Check pool host, port, and network connectivity."], "nerd_repo"),
    "nerd:verify_blocked": _e("Pool blocked by verification", "NerdQAxe+ blocked the pool because coinbase payout verification failed (red Stratum tile).",
                              "Missing payout address or pool fee exceeds firmware limit.",
                              ["Check your payout address and solo/pool settings."], "nerd_repo"),
    "nerd:pool_diff_err": _e("Pool share difficulty error", "NerdQAxe+ reports poolDiffErr = true (share for pool too small).",
                             "Pool minimum difficulty mismatch.",
                             ["Check pool port/difficulty settings."], "nerd_repo", "warn"),
    "nerd:voltage_out_of_range": _e("Input voltage out of range", "Input voltage is outside the board's [minVoltage, maxVoltage] bounds.",
                                    "Overloaded/failing PSU, thin cable, or wrong PSU voltage.",
                                    ["Measure PSU voltage under load and check DC/XT60 connectors."], "nerd_repo", "warn"),
    "nerd:current_high": _e("High input current", "Input current is >= 94 % of the board's maxCurrentA limit.",
                            "High clock/voltage power draw approaching PSU/fuse rating.",
                            ["Lower frequency or core voltage to stay safely below max current."], "nerd_repo", "warn"),
    "nerd:power_high": _e("High input power", "Input power is >= 95 % of the board's maxPower limit.",
                          "Power draw near board/PSU maximum.",
                          ["Lower frequency or core voltage."], "nerd_repo", "warn"),
    "nerd:vr_temp_high": _e("High VR temperature", "NerdQAxe+ voltage regulator temperature is in the warning/critical band.",
                            "Inadequate VR cooling or high power draw.",
                            ["Improve VR airflow and lower core voltage/frequency."], "nerd_repo", "warn"),
    "nerd:asic_temp_high": _e("High ASIC temperature", "NerdQAxe+ ASIC temperature is near or at the overheat limit.",
                              "Blocked airflow, stalled fan, or high clock/voltage.",
                              ["Check fans and cooling; lower frequency/voltage."], "nerd_repo", "warn"),
    "nerd:asic_sensor_dead": _e("ASIC temp sensor dead", "One or more ASIC temperature sensors report <= 0 °C while sibling sensors read normal.",
                                "Failed temperature sensor or communication issue on one chip.",
                                ["Restart the miner; watch remaining chip temps closely."], "nerd_repo"),
    # --- Additional Stock Bitmain / CGMiner alerts ---------------------------
    "bitmain:fan_slow": _e("Fan spinning slowly", "A fan is spinning far below sibling fans (< 35 % of median RPM).",
                           "Worn fan bearings, dust, or failing fan motor.",
                           ["Clean or replace the slow fan before it fails completely."], "bitmain_19", "warn"),
    "bitmain:zero_hashrate": _e("Zero hashrate", "Antminer has been online >= 3 min and reports 0 GH/s.",
                                "Dead pools, initialization failure, overheat protection, or PSU issue.",
                                ["Check pool connectivity, hashboard chip counts, and kernel log."], "bitmain_19"),
    "bitmain:low_hashrate": _e("Hashrate below nominal", "Hashrate is under 75 % of nominal/ideal rate after warmup.",
                               "Missing/disabled hashboard, dead chips, or thermal throttling.",
                               ["Check per-chain chip counts, temps, and kernel logs."], "bitmain_19", "warn"),
    "bitmain:temp_high": _e("High Antminer temperature", "Chip temp >= 85 °C or PCB temp >= 75 °C.",
                            "Hot intake air, dust-clogged heatsinks, or failing fan.",
                            ["Check fans, clean heatsinks, and improve intake cooling."], "bitmain_19"),
    "bitmain:temp_sensor_error": _e("Temperature sensor error", "A chain reports 0, negative, or >= 125 °C temperature while hashing.",
                                    "Disconnected/damaged sensor or loose ribbon cable.",
                                    ["Reseat the hashboard data cable and inspect the sensor."], "dcentral_kernel"),
    "bitmain:hw_errors_high": _e("High hardware error rate", "CGMiner Device Hardware% is >= 2.0 %.",
                                 "Overheating, bad chips, or unstable voltage/frequency.",
                                 ["Check chip temps, power supply voltage, and chain health."], "bitmain_19", "warn"),
    "bitmain:dev_unhealthy": _e("Hashboard Dead/Sick", "CGMiner DEVS reports a hashboard status is Dead or Sick.",
                                "Board stopped responding or failed during mining.",
                                ["Check logs and cables; reboot once cool if needed."], "bitmain_19"),
    "bitmain:pools_dead": _e("All pools dead", "CGMiner POOLS reports all configured pools are Dead.",
                             "Network/DNS outage or invalid pool endpoints.",
                             ["Check Ethernet, DNS, and pool configuration."], "bitmain_19"),
    "bitmain:cgminer_error": _e("CGMiner error status", "CGMiner API returned an error or warning status message.",
                                "Firmware-reported runtime fault.",
                                ["Read the message above and check the kernel log."], "bitmain_19"),
    "bitmain:web_warning": _e("Antminer web UI warning", "Bitmain web UI (/cgi-bin/warning.cgi, summary status, or port 6060) reported a warning.",
                              "Hardware, fan, voltage, temp, or pool fault reported by BMMiner.",
                              ["Read the exact code/message above and check Bitmain troubleshooting."], "bitmain_19"),
    # --- Repeated resets -----------------------------------------------------
    "axeos:chip_power_rail_off": _e(
        "Chip power rail off",
        "Core voltage actual is 0 V (while core voltage is set > 0) or ASIC power/current dropped near 0 W while input voltage is present and mining is not paused.",
        "VR/power-stage thermal shutdown or a latched VREG fault cut the ASIC core power rail (on NerdOCTAxe v1.0.37.1, shutdown and boardError may reset to false after a watchdog reboot).",
        ["Check Miner Hearth's remembered VR temperature history for a recent overheat spike before power dropped to 0 W.",
         "All-zero ASIC temps alongside ~0 W power confirms the ASIC core rail is off, not just a reporting glitch.",
         "Verify the 12 V / 5 V PSU input rail and VR heatsink airflow, then restart or power-cycle once cool."],
        "nerd_repo"),
    "tuner:sensor_suspect": _e(
        "Suspect temperature sensor reading",
        "Miner Hearth detected an implausible single-poll temperature jump (> 25 °C) or extreme VR/ASIC split (VR > 105 °C while ASIC < 45 °C).",
        "Sensor/ADC glitch, loose thermistor contact, or sudden thermal runaway; the autotuner stepped frequency down safely and blocked upward steps.",
        ["Check live ASIC and VR temperatures and verify fan operation.",
         "Confirm sensor readings stay plausible over several polls before raising frequency."],
        "bitaxe_fault", "warn"),
    "reset:loop": _e("Repeated reboot loop", "The miner restarted 3 or more times within 2 hours.",
                     "Power instability, thermal shutdowns, watchdog crashes, or pool/network disconnects triggering restarts.",
                     ["Inspect reset reasons, input voltage, and cooling before leaving unattended."], "bitaxe_monitoring", "warn"),
}

for _code, (_desc, _fix) in BRAIINS_ERROR_CODES.items():
    _sev = "problem"
    CATALOG[f"braiins:{_code}"] = _e(f"{_code}: {_desc}", _desc + ".", "See the Braiins troubleshooting text below.", [_fix], "braiins_codes", _sev)

SEVERITY_ORDER = {"problem": 0, "warn": 1, "info": 2}


def explain(key: str) -> Optional[dict[str, Any]]:
    e = CATALOG.get(key)
    if not e:
        return None
    g = GUIDES.get(e["guide"]) or ("", "")
    return dict(e, guide_label=g[0], guide_url=g[1])


def _fault(key: str, code: str, text: str, source: str, severity: Optional[str] = None, component: str = "") -> dict[str, Any]:
    ex = explain(key)
    out: dict[str, Any] = {"key": key, "code": code, "text": str(text)[:360], "source": source, "mapped": ex is not None}
    if component:
        out["component"] = str(component)[:180]
    if ex:
        out.update({k: ex[k] for k in ("title", "meaning", "cause", "checks", "guide_label", "guide_url")})
        out["severity"] = severity or ex["severity"]
    else:
        out.update({"title": f"Unmapped: {str(text)[:120]}", "meaning": "This code/text is not in Miner Hearth's documented list. It is shown exactly as the miner reported it.",
                    "cause": "", "checks": ["Search the exact text in your firmware's documentation or support forum."], "guide_label": "", "guide_url": ""})
        out["severity"] = severity or "warn"
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")[:60] or "unknown"


def classify_reset_reason(text: Any) -> Optional[str]:
    """Return a reset:* catalog key for abnormal reset reasons, else None (power-on / software reset are normal)."""
    t = str(text or "").lower()
    if not t:
        return None
    if "brownout" in t:
        return "reset:brownout"
    if "wdt" in t or "watchdog" in t:
        return "reset:watchdog"
    if "panic" in t or "exception" in t:
        return "reset:panic"
    if "glitch" in t:
        return "reset:glitch"
    return None


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return bool(v)


NERD_SCREEN = {1: "MINER OVERHEATED", 2: "VREG OVERHEATED", 3: "PSU FAULT", 4: "CURRENT PROTECTION", 5: "VOLTAGE PROTECTION", 6: "VERIFY FAILED"}


def _opt_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_falsy_flag(v: Any) -> bool:
    if v in (None, "", 0, False):
        return True
    if isinstance(v, str) and v.strip().lower() in {"0", "false", "no", "off", "none", ""}:
        return True
    return False


_6060_PREFIX_TEXT = {
    "R": "Fan lost or fan speed error",
    "N": "Hashrate / chain error",
    "V": "PSU / input voltage error",
    "P": "Temperature / overheat protection",
    "M": "Pool / network error",
    "J": "Hashboard / ASIC init error",
    "L": "Hashboard sensor / temp error",
}



def _fmt_ts_local(ts: Optional[float]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return ""


def _model_no_tach_hint(r: dict[str, Any]) -> bool:
    model = str(r.get("deviceModel") or r.get("model") or "").lower()
    if "601" in model:
        return True
    if "gamma" in model and "nerd" not in model and "octaxe" not in model:
        return True
    return False


def _vr_sensor_pick(r: dict[str, Any]) -> tuple[Optional[float], str]:
    ext = _opt_float(r.get("vrTemp"))
    inte = _opt_float(r.get("vrTempInt"))
    cands: list[tuple[float, str]] = []
    if ext is not None and ext > 0:
        cands.append((ext, "vrTemp"))
    if inte is not None and inte > 0:
        cands.append((inte, "vrTempInt"))
    if not cands:
        return None, ""
    cands.sort(key=lambda x: x[0], reverse=True)
    val, name = cands[0]
    return val, f"{name} ({val:.1f} °C)"


def mask_address(addr: str) -> str:
    """Return a short masked preview that never exposes full wallet prefixes or addresses."""
    a = str(addr or "").strip()
    if not a:
        return ""
    if ":" in a:
        pfx, rest = a.split(":", 1)
        tail = rest[-4:] if len(rest) >= 4 else rest
        return f"{pfx}:…{tail}"
    if len(a) <= 8:
        return "…" + a[-3:]
    return f"{a[:3]}…{a[-4:]}"


def collect_faults(raw: dict[str, Any], uptime_sec: Optional[float] = None) -> list[dict[str, Any]]:
    """Turn the raw fault fields published with a reading (Reading.fault_raw + probes) into explained faults."""
    r = raw if isinstance(raw, dict) else {}
    out: list[dict[str, Any]] = []

    is_nerd = bool(
        "boardError" in r
        or "shutdown" in r
        or r.get("is_nerd")
        or isinstance(r.get("nerd_fans"), list)
        or (isinstance(r.get("stratum_pools"), list) and len(r.get("stratum_pools")) > 0)
        or (r.get("minVoltage") is not None and r.get("maxVoltage") is not None)
    )
    is_braiins = bool(
        r.get("braiins_errors")
        or r.get("braiins_status")
        or r.get("is_braiins")
        or r.get("tuner_chains")
        or r.get("braiins_fans")
        or r.get("braiins_temps")
    )

    # --- AxeOS / ESP-Miner --------------------------------------------------
    overheat_active = not _is_falsy_flag(r.get("overheat_mode"))
    if overheat_active:
        out.append(_fault("axeos:overheat_mode", "overheat_mode", "overheat_mode = 1", "AxeOS /api/system/info"))
    pf = r.get("power_fault")
    power_fault_active = not _is_falsy_flag(pf)
    if power_fault_active:
        out.append(_fault("axeos:power_fault", "power_fault", str(pf), "AxeOS /api/system/info"))
    hf = r.get("hardware_fault")
    if not _is_falsy_flag(hf):
        key = "axeos:hardware_fault:fan" if "fan" in str(hf).lower() else "axeos:hardware_fault"
        out.append(_fault(key, "hardware_fault", str(hf), "AxeOS /api/system/info"))
    paused_active = _truthy(r.get("miningPaused"))
    if paused_active:
        out.append(_fault("axeos:mining_paused", "miningPaused", "miningPaused = true", "AxeOS /api/system/info"))

    # Voltage check: nominalVoltage (AxeOS), minVoltage/maxVoltage (NerdQAxe+), or heuristic rail sag
    v = _opt_float(r.get("voltage"))
    nom = _opt_float(r.get("nominalVoltage"))
    min_v = _opt_float(r.get("minVoltage"))
    max_v = _opt_float(r.get("maxVoltage"))
    if v is not None and v > 0:
        if min_v is not None and max_v is not None and min_v > 0 and max_v > min_v:
            v_v = v / 1000.0 if (v > 100 and max_v < 100) else v
            if v_v < min_v or v_v > max_v:
                out.append(_fault("nerd:voltage_out_of_range", "voltage",
                                  f"{v_v:.2f} V outside [{min_v:g}, {max_v:g}] V", "NerdQAxe+ power"))
        elif nom is not None and nom > 0:
            nom_cmp = nom * 1000.0 if (nom < 100 and v > 100) else nom
            if v < nom_cmp * 0.949:
                unit = "mV" if v > 100 else "V"
                out.append(_fault("axeos:low_voltage", "voltage", f"{v:.0f} {unit} vs nominal {nom_cmp:.0f} {unit}", "AxeOS /api/system/info"))
        else:
            v_v = v / 1000.0 if v > 100 else v
            if (2.5 < v_v < 4.6) or (8.5 <= v_v < 11.2):
                rail = 5.0 if v_v < 7.5 else 12.0
                out.append(_fault("axeos:low_voltage", "voltage", f"{v_v:.2f} V (expected ~{rail:.0f} V rail)", "AxeOS /api/system/info"))

    # NerdQAxe+ boardError & shutdown
    be = r.get("boardError")
    try:
        be_i = int(be) if be not in (None, "") else 0
    except (TypeError, ValueError):
        be_i = -1
    if be_i > 0 or be_i == -1:
        key = f"nerd:board_error:{be_i}"
        out.append(_fault(key, f"boardError {be if be_i == -1 else be_i}",
                          f"boardError = {be}" + (f' (screen: "{NERD_SCREEN[be_i]}")' if be_i in NERD_SCREEN else ""),
                          "NerdQAxe+ /api/v2/dashboard"))
    shutdown_active = _truthy(r.get("shutdown"))
    if shutdown_active and be_i == 0:
        out.append(_fault("nerd:shutdown", "shutdown", "system.shutdown = true", "NerdQAxe+ /api/v2/dashboard"))

    # NerdQAxe+ current / power limits
    cur_a = _opt_float(r.get("currentA"))
    max_cur_a = _opt_float(r.get("maxCurrentA"))
    if cur_a is not None and max_cur_a is not None and max_cur_a > 0:
        if cur_a >= max_cur_a * 0.99:
            out.append(_fault("nerd:current_high", "currentA", f"{cur_a:.1f} A >= {max_cur_a:g} A max", "NerdQAxe+ power", severity="problem"))
        elif cur_a >= max_cur_a * 0.94:
            out.append(_fault("nerd:current_high", "currentA", f"{cur_a:.1f} A (near {max_cur_a:g} A max)", "NerdQAxe+ power", severity="warn"))
    pwr_w = _opt_float(r.get("power"))
    max_pwr_w = _opt_float(r.get("maxPower"))
    if pwr_w is not None and max_pwr_w is not None and max_pwr_w > 0 and pwr_w >= max_pwr_w * 0.95:
        out.append(_fault("nerd:power_high", "power", f"{pwr_w:.1f} W (near {max_pwr_w:g} W max)", "NerdQAxe+ power"))

    # AxeOS & NerdQAxe+ temperatures (UI thresholds: chip >= 70, VR >= 105 on AxeOS; 94/115 or vrOverheatTemp on Nerd)
    t1 = _opt_float(r.get("temp"))
    t2 = _opt_float(r.get("temp2"))
    asic_t_max = _opt_float(r.get("asicTemp"))
    asic_temps_list = r.get("asicTemps") if isinstance(r.get("asicTemps"), list) else []
    hot_asic_slots: list[int] = []
    if asic_temps_list:
        pos_slots = [(i + 1, _opt_float(x)) for i, x in enumerate(asic_temps_list)]
        pos_vals = [(idx, val) for idx, val in pos_slots if val is not None and val > 0]
        if pos_vals:
            peak_slot_val = max(val for _, val in pos_vals)
            hot_asic_slots = [idx for idx, val in pos_vals if val >= peak_slot_val - 0.5]
    chip_comp = f"ASIC slot(s) {hot_asic_slots}" if hot_asic_slots else ("temp2" if (t2 is not None and (t1 is None or t2 > t1)) else "temp")
    nerd_oh = _opt_float(r.get("overheat_temp")) or _opt_float(r.get("overheatTemp"))
    hot_chip = max([x for x in (t1, t2, asic_t_max) if x is not None and x > 0], default=None)
    if is_nerd:
        oh_lim = nerd_oh if (nerd_oh and nerd_oh >= 50) else 72.0
        if hot_chip is not None:
            if hot_chip >= oh_lim:
                out.append(_fault("nerd:asic_temp_high", "asicTemp", f"{hot_chip:.1f} °C >= {oh_lim:g} °C ({chip_comp})", "NerdQAxe+ thermal", severity="problem", component=chip_comp))
            elif hot_chip >= oh_lim * 0.94 and hot_chip >= 65.0:
                out.append(_fault("nerd:asic_temp_high", "asicTemp", f"{hot_chip:.1f} °C (near {oh_lim:g} °C limit, {chip_comp})", "NerdQAxe+ thermal", severity="warn", component=chip_comp))
    else:
        if hot_chip is not None and hot_chip >= 70.0 and not overheat_active:
            out.append(_fault("axeos:high_temp", "temp", f"Chip {hot_chip:.1f} °C >= 70 °C ({chip_comp})", "AxeOS /api/system/info", component=chip_comp))

    vr_t, vr_comp = _vr_sensor_pick(r)
    if vr_t is not None:
        vr_code = "vrTempInt" if vr_comp.startswith("vrTempInt") else "vrTemp"
        if is_nerd:
            vr_lim = _opt_float(r.get("vrOverheatTemp"))
            crit_vr = max(105.0, vr_lim) if (vr_lim and vr_lim >= 85) else 105.0
            if vr_t >= crit_vr:
                out.append(_fault("nerd:vr_temp_high", vr_code, f"{vr_comp} >= {crit_vr:g} °C", "NerdQAxe+ thermal", severity="problem", component=vr_comp))
            elif vr_t >= 94.0:
                out.append(_fault("nerd:vr_temp_high", vr_code, f"{vr_comp} >= 94 °C", "NerdQAxe+ thermal", severity="warn", component=vr_comp))
        else:
            if vr_t >= 105.0 and not overheat_active:
                out.append(_fault("axeos:high_vr_temp", vr_code, f"{vr_comp} >= 105 °C", "AxeOS /api/system/info", component=vr_comp))
            elif vr_t >= 94.0 and not overheat_active:
                out.append(_fault("axeos:vr_temp_warn", vr_code, f"{vr_comp} >= 94 °C", "AxeOS /api/system/info", component=vr_comp))

    # Miner Hearth suspect-sensor guard fault (injected by tuner)
    if r.get("tuner_sensor_suspect"):
        out.append(_fault(
            "tuner:sensor_suspect",
            "sensor_plausibility",
            str(r["tuner_sensor_suspect"]),
            "Miner Hearth safety guard",
            component=str(r.get("tuner_sensor_component") or "temperature sensor"),
        ))

    # Temp sensor failure on AxeOS / NerdQAxe+
    hr = _opt_float(r.get("hashRate"))
    active_for_sensor = (
        not overheat_active
        and not power_fault_active
        and not paused_active
        and not shutdown_active
        and be_i == 0
        and ((hr is not None and hr > 0) or (pwr_w is not None and pwr_w > 5.0))
    )
    if "temp" in r and t1 is not None and t1 <= 0 and active_for_sensor:
        out.append(_fault("axeos:temp_sensor_error", "temp", f"temp = {t1:g} °C while active", "AxeOS /api/system/info"))
    asic_temps_raw = r.get("asicTemps") if isinstance(r.get("asicTemps"), list) else []
    if asic_temps_raw and active_for_sensor:
        vals = [_opt_float(x) for x in asic_temps_raw]
        pos = [x for x in vals if x is not None and x > 0]
        bad_idx = [i + 1 for i, x in enumerate(vals) if x is not None and x <= 0]
        if pos and bad_idx:
            comp = f"ASIC slot(s) {bad_idx}"
            out.append(_fault("nerd:asic_sensor_dead", "asicTemps",
                              f"ASIC sensor(s) {bad_idx} reading <= 0 °C while others read {max(pos):.1f} °C",
                              "NerdQAxe+ thermal", component=comp))

    # Fans on AxeOS / NerdQAxe+ (skip false stall when Bitaxe Gamma 601 has no RPM tach sensor)
    tach_seen = r.get("fan_tach_seen") if isinstance(r.get("fan_tach_seen"), dict) else None
    fan_no_tach = bool(r.get("fan_no_tach"))
    nerd_fans = r.get("nerd_fans") if isinstance(r.get("nerd_fans"), list) else []
    if nerd_fans:
        any_spinning = any((_opt_float(f.get("rpm")) or 0) > 0 for f in nerd_fans if isinstance(f, dict))
        for idx, f in enumerate(nerd_fans, start=1):
            if not isinstance(f, dict):
                continue
            rpm = _opt_float(f.get("rpm"))
            spd = _opt_float(f.get("speed"))
            lbl = f"fan{idx}"
            if rpm is not None and rpm <= 0:
                if (spd is not None and spd >= 35.0) or (spd is None and any_spinning):
                    out.append(_fault("nerd:fan_stalled", lbl,
                                      f"Fan {idx}: 0 RPM at {spd if spd is not None else '?'}% speed",
                                      "NerdQAxe+ thermal", component=f"Fan {idx}"))
                elif spd is not None and 0 < spd < 35.0:
                    out.append(_fault("nerd:fan_low_rpm", lbl,
                                      f"Fan {idx}: 0 RPM at low duty {spd:.0f}%",
                                      "NerdQAxe+ thermal", component=f"Fan {idx}"))
    elif "fanrpm" in r or "fan2rpm" in r:
        fspd = _opt_float(r.get("fanspeed"))
        for key, lbl in (("fanrpm", "fan1"), ("fan2rpm", "fan2")):
            if key not in r:
                continue
            frpm = _opt_float(r.get(key))
            if frpm is not None and frpm <= 0 and ((fspd is not None and fspd >= 20.0) or (key == "fan2rpm" and (_opt_float(r.get("fanrpm")) or 0) > 0)):
                if not is_nerd:
                    if fan_no_tach:
                        continue
                    if tach_seen is not None and not tach_seen.get(lbl):
                        continue
                    if tach_seen is None and _model_no_tach_hint(r):
                        continue
                k = "nerd:fan_stalled" if is_nerd else "axeos:fan_stalled"
                out.append(_fault(k, lbl, f"{lbl}: 0 RPM (fanspeed {fspd if fspd is not None else '?'}%)", "AxeOS /api/system/info", component=lbl))

    # Pools / Stratum on AxeOS & NerdQAxe+
    stratum_pools = r.get("stratum_pools") if isinstance(r.get("stratum_pools"), list) else []
    pool_conn_info = str(r.get("poolConnectionInfo") or "").strip()
    pool_disconnected = False
    if pool_conn_info.lower() in {"not connected", "disconnected", "none", "offline"}:
        pool_disconnected = True
        out.append(_fault("axeos:pool_disconnected", "poolConnectionInfo", f"poolConnectionInfo = {pool_conn_info}", "AxeOS /api/system/info"))
    if stratum_pools:
        active_p = [sp for sp in stratum_pools if isinstance(sp, dict) and sp.get("active")]
        check_p = active_p if active_p else [sp for sp in stratum_pools if isinstance(sp, dict)]
        if check_p and all(sp.get("connected") is False for sp in check_p):
            pool_disconnected = True
            out.append(_fault("nerd:stratum_disconnected", "stratum", "Stratum pool connected = false", "NerdQAxe+ stratum"))
        for idx, sp in enumerate(stratum_pools, start=1):
            if not isinstance(sp, dict):
                continue
            if sp.get("verifyBlocked"):
                out.append(_fault("nerd:verify_blocked", f"pool{idx}", f"Pool {idx} verifyBlocked: {sp.get('verifyBlocked')}", "NerdQAxe+ stratum"))
            if sp.get("poolDiffErr"):
                out.append(_fault("nerd:pool_diff_err", f"pool{idx}", f"Pool {idx} share difficulty error (poolDiffErr)", "NerdQAxe+ stratum"))

    if _truthy(r.get("isUsingFallbackStratum")):
        perr = str(r.get("primaryPoolError") or "").strip()
        txt = f"Using fallback stratum ({perr})" if perr else "Using fallback stratum (primary pool unreachable)"
        out.append(_fault("axeos:fallback_stratum", "isUsingFallbackStratum", txt, "AxeOS /api/system/info"))

    pdiff = _opt_float(r.get("poolDifficulty"))
    adiff = _opt_float(r.get("asicDifficulty"))
    if pdiff is not None and adiff is not None and pdiff > 0 and adiff > 0 and pdiff < adiff:
        out.append(_fault("axeos:pool_diff_low", "poolDifficulty", f"poolDifficulty {pdiff:g} < asicDifficulty {adiff:g}", "AxeOS /api/system/info"))

    cb_status = str(r.get("coinbasePayoutStatus") or "").strip().lower()
    if cb_status == "not_found" or _truthy(r.get("coinbaseMismatch")):
        out.append(_fault("axeos:coinbase_payout", "coinbasePayoutStatus",
                          f"coinbasePayoutStatus = {cb_status or 'mismatch'}", "AxeOS /api/system/info"))

    # Wi-Fi status & RSSI
    wstat = str(r.get("wifiStatus") or "").strip()
    if wstat:
        wl = wstat.lower()
        is_conn = (wl.startswith("connected") or wl == "ok") and not any(
            bad in wl for bad in ("fail", "disconnect", "not connect", "lost", "error", "reconnecting", "connecting")
        )
        if not is_conn:
            out.append(_fault("axeos:wifi_disconnected", "wifiStatus", f"wifiStatus = {wstat}", "AxeOS /api/system/info"))
    rssi = _opt_float(r.get("wifiRSSI"))
    if rssi is not None and -120 <= rssi <= -85:
        out.append(_fault("axeos:wifi_weak", "wifiRSSI", f"wifiRSSI = {rssi:.0f} dBm", "AxeOS /api/system/info"))

    # Frequency low (only when board explicitly gives minFrequency / frequencyOptions)
    freq = _opt_float(r.get("frequency"))
    min_freq_opt = _opt_float(r.get("minFrequencyOpt"))
    if freq is not None and freq > 0 and min_freq_opt is not None and freq < min_freq_opt and not overheat_active and not paused_active:
        out.append(_fault("axeos:frequency_low", "frequency", f"{freq:g} MHz < board min {min_freq_opt:g} MHz", "AxeOS /api/system/info"))

    # AxeOS / Nerd chip power rail off (v1.12 OCTAxe root-cause detection)
    up = uptime_sec if uptime_sec is not None else _opt_float(r.get("uptimeSeconds"))
    cv_set = _opt_float(r.get("coreVoltage"))
    cv_act = _opt_float(r.get("coreVoltageActual"))
    v_in = v / 1000.0 if (v is not None and v > 100) else v
    input_ok = (v_in is not None) and ((4.5 <= v_in <= 6.5) or (10.5 <= v_in <= 14.5))
    rail_off_cv = (cv_act is not None and cv_act <= 0 and cv_set is not None and cv_set > 0)
    rail_off_pwr = (pwr_w is not None and pwr_w <= 1.0 and (cur_a is None or cur_a <= 0.1) and (cv_set is None or cv_set > 0))
    last_vr_ctx = r.get("nk_last_vr_overheat") if isinstance(r.get("nk_last_vr_overheat"), dict) else None
    rail_off_active = False
    if (rail_off_cv or rail_off_pwr) and input_ok and not paused_active and (up is None or up >= 30):
        rail_off_active = True
        ev_parts: list[str] = []
        if rail_off_cv:
            ev_parts.append(f"coreVoltageActual = 0 mV (set {cv_set:g} mV)")
        if pwr_w is not None and pwr_w <= 1.0:
            ev_parts.append(f"power = {pwr_w:.2f} W" + (f", current = {cur_a:.2f} A" if cur_a is not None else ""))
        ev_parts.append(f"input = {v_in:.2f} V OK")
        if asic_temps_raw and all((_opt_float(x) or 0) <= 0 for x in asic_temps_raw):
            ev_parts.append("all ASIC temps 0 °C (ASIC rail unpowered)")
        if last_vr_ctx and last_vr_ctx.get("temp_c") is not None:
            vr_when = last_vr_ctx.get("iso") or _fmt_ts_local(last_vr_ctx.get("ts"))
            vr_s = last_vr_ctx.get("sensor") or "vrTemp"
            ev_parts.append(f"likely cause: NK recorded {vr_s} overheat {float(last_vr_ctx['temp_c']):.1f} °C" + (f" at {vr_when}" if vr_when else ""))
        f_rail = _fault("axeos:chip_power_rail_off", "coreVoltageActual/power", "; ".join(ev_parts),
                        "AxeOS /api/system/info", component="ASIC core VR / power stage")
        if last_vr_ctx and last_vr_ctx.get("temp_c") is not None:
            vr_when = last_vr_ctx.get("iso") or _fmt_ts_local(last_vr_ctx.get("ts"))
            f_rail["cause"] = (
                f"Miner Hearth recorded VR overheat ({float(last_vr_ctx['temp_c']):.1f} °C"
                + (f" at {vr_when}" if vr_when else "")
                + ") before the ASIC core rail shut off (latched VREG/power-stage fault; firmware shutdown/boardError flags may clear on watchdog reboot)."
            )
            f_rail["cause_ts"] = last_vr_ctx.get("ts")
        out.append(f_rail)

    # AxeOS / Nerd hashrate & ASIC health
    stopped_explained = overheat_active or power_fault_active or rail_off_active or (not _is_falsy_flag(hf)) or paused_active or shutdown_active or (be_i != 0) or pool_disconnected
    if "hashRate" in r and hr is not None and hr <= 0 and not stopped_explained and (up is None or up >= 120):
        out.append(_fault("axeos:zero_hashrate", "hashRate", "0 GH/s while online and not paused", "AxeOS /api/system/info", component="ASIC hashing core"))
    exp_hr = _opt_float(r.get("expectedHashrate"))
    if hr is not None and hr > 0 and exp_hr is not None and exp_hr > 0 and hr < exp_hr * 0.50 and not stopped_explained and (up is None or up >= 180):
        out.append(_fault("axeos:low_hashrate", "hashRate", f"{hr:.1f} GH/s vs expected {exp_hr:.1f} GH/s", "AxeOS /api/system/info", component="ASIC array"))
    dead_asics = r.get("dead_asics") if isinstance(r.get("dead_asics"), list) else []
    if dead_asics and hr is not None and hr > 0 and (up is None or up >= 180):
        slots = [int(i) + 1 for i in dead_asics]
        out.append(_fault("axeos:dead_asic", "hashrateMonitor", f"ASIC(s) {slots} reporting 0 GH/s", "AxeOS hashrateMonitor", component=f"ASIC slot(s) {slots}"))
    # v1.15.5: err_pct used to be read here before it was assigned, so any AxeOS miner reporting a non-zero
    # asic_error_counts raised UnboundLocalError and the fault list froze at its last good state.
    err_pct = _opt_float(r.get("errorPercentage"))
    if err_pct is not None and err_pct >= 5.0 and not paused_active:
        asic_errs = r.get("asic_error_counts") if isinstance(r.get("asic_error_counts"), dict) else {}
        err_comp = ""
        if asic_errs:
            hot_e = [f"ASIC #{int(k) + 1} ({int(v)} err)" for k, v in sorted(asic_errs.items(), key=lambda kv: int(kv[0])) if int(v or 0) > 0]
            if hot_e:
                err_comp = ", ".join(hot_e[:8])
        out.append(_fault("axeos:high_error_pct", "errorPercentage", f"errorPercentage = {err_pct:.2f}%" + (f" [{err_comp}]" if err_comp else ""), "AxeOS /api/system/info", component=err_comp))

    # --- Reset reason & reset loop ------------------------------------------
    rr = r.get("resetReason") or r.get("lastResetReason")
    rk = classify_reset_reason(rr)
    if rk and (uptime_sec is None or uptime_sec < 6 * 3600):
        out.append(_fault(rk, "resetReason", str(rr), "reset reason"))
    reboots_2h = _opt_float(r.get("recent_reboots_2h"))
    if reboots_2h is not None and reboots_2h >= 3:
        out.append(_fault("reset:loop", "reboots", f"{int(reboots_2h)} restarts in the last 2 hours", "Miner Hearth uptime tracker"))

    # --- Antminer / CGMiner (Stock Bitmain + Braiins CGMiner) ---------------
    chains = r.get("chains") if isinstance(r.get("chains"), list) else []
    counts = [c.get("acn") for c in chains if isinstance(c, dict) and isinstance(c.get("acn"), int)]
    top = max(counts) if counts else 0
    has_chain_missing = False
    for c in chains:
        if not isinstance(c, dict):
            continue
        if not c.get("present", True):
            continue
        idx, acn, acs = c.get("idx"), c.get("acn"), str(c.get("acs") or "")
        if acn == 0:
            has_chain_missing = True
            out.append(_fault("bitmain:chain_missing", f"chain{idx}", f"Chain {idx}: 0 chips", "CGMiner stats chain_acn", component=f"Hashboard chain {idx}"))
        elif isinstance(acn, int) and acn > 0 and top and acn < top:
            out.append(_fault("bitmain:chain_few_chips", f"chain{idx}", f"Chain {idx}: {acn} chips (others {top})", "CGMiner stats chain_acn", component=f"Hashboard chain {idx}"))
        bad = acs.count("x") + acs.count("-")
        if bad and acn:
            compact = acs.replace(" ", "")
            x_pos = [i + 1 for i, ch in enumerate(compact) if ch == "x"]
            dash_pos = [i + 1 for i, ch in enumerate(compact) if ch == "-"]
            pos_parts: list[str] = []
            if x_pos:
                pos_parts.append(f"'x' chip(s) #{', #'.join(str(n) for n in x_pos[:12])}" + (f" (+{len(x_pos) - 12})" if len(x_pos) > 12 else ""))
            if dash_pos:
                pos_parts.append(f"'-' chip(s) #{', #'.join(str(n) for n in dash_pos[:12])}" + (f" (+{len(dash_pos) - 12})" if len(dash_pos) > 12 else ""))
            pos_txt = "; ".join(pos_parts)
            comp = f"Chain {idx}: {pos_txt}" if pos_txt else f"Chain {idx}"
            out.append(_fault("bitmain:chain_bad_chips", f"chain{idx}", f"Chain {idx}: {acs.count('x')} 'x', {acs.count('-')} '-' in chain_acs ({pos_txt})", "CGMiner stats chain_acs", component=comp))
    if r.get("chain_count_dropped") and not has_chain_missing:
        has_chain_missing = True
        out.append(_fault("bitmain:chain_missing", "chains", str(r["chain_count_dropped"]), "CGMiner chain count"))

    # Fans (Stock Bitmain STATS fanN or Braiins FANS[])
    fans_raw = r.get("braiins_fans") if isinstance(r.get("braiins_fans"), list) else (r.get("fans") if isinstance(r.get("fans"), list) else [])
    fans_src = "Braiins CGMiner FANS" if isinstance(r.get("braiins_fans"), list) else "CGMiner stats fan"
    fan_lost_key = "braiins:cgminer:fan_lost" if is_braiins else "bitmain:fan_lost"
    fan_slow_key = "braiins:cgminer:fan_slow" if is_braiins else "bitmain:fan_slow"
    if fans_raw:
        pos_fans = [int(f) for f in fans_raw if isinstance(f, (int, float)) and f > 0]
        if pos_fans:
            for i, f in enumerate(fans_raw):
                if (f or 0) <= 0:
                    out.append(_fault(fan_lost_key, f"fan{i + 1}", f"Fan {i + 1}: 0 RPM", fans_src))
            if len(pos_fans) >= 2:
                s_pos = sorted(pos_fans)
                med = s_pos[len(s_pos) // 2]
                if med >= 2000:
                    for i, f in enumerate(fans_raw):
                        if isinstance(f, (int, float)) and 0 < f < med * 0.35:
                            out.append(_fault(fan_slow_key, f"fan{i + 1}", f"Fan {i + 1}: {int(f)} RPM (others ~{med} RPM)", fans_src))
    if r.get("fan_count_dropped"):
        out.append(_fault(fan_lost_key, "fans", str(r["fan_count_dropped"]), fans_src))

    # Antminer temps, hashrate, hw errors, DEVS, POOLS, CGMiner STATUS errors
    cg_ghs = _opt_float(r.get("cg_hashrate_ghs"))
    cg_ideal = _opt_float(r.get("cg_rate_ideal_ghs"))
    cg_elapsed = _opt_float(r.get("cg_elapsed"))
    cg_hw_pct = _opt_float(r.get("cg_hw_pct"))
    cg_devs = r.get("cg_devs") if isinstance(r.get("cg_devs"), list) else []
    cg_pools_dead = bool(r.get("cg_pools_all_dead"))

    if cg_pools_dead:
        out.append(_fault("braiins:cgminer:pools_dead" if is_braiins else "bitmain:pools_dead", "pools", "All configured pools report Dead", "CGMiner POOLS"))
    for dv in cg_devs:
        if not isinstance(dv, dict):
            continue
        dst = str(dv.get("status") or "").strip()
        if dst and dst.lower() not in {"alive", "ok"}:
            out.append(_fault("braiins:cgminer:dev_unhealthy" if is_braiins else "bitmain:dev_unhealthy",
                              f"dev{dv.get('id', 0)}", f"Chain/Dev {dv.get('id', 0)}: Status = {dst}", "CGMiner DEVS", component=f"DEVS[{dv.get('id', 0)}]"))

    # Per-chain chip/board temperatures on Antminer / Braiins
    cg_chip_temps = r.get("cg_chip_temps") if isinstance(r.get("cg_chip_temps"), list) else []
    cg_pcb_temps = r.get("cg_pcb_temps") if isinstance(r.get("cg_pcb_temps"), list) else []
    if cg_chip_temps:
        valid_c = [float(x) for x in cg_chip_temps if isinstance(x, (int, float)) and 0 < x < 125]
        hot_c = max(valid_c, default=None)
        if hot_c is not None and hot_c >= (95.0 if is_braiins else 85.0):
            hot_ch = [i + 1 for i, x in enumerate(cg_chip_temps) if isinstance(x, (int, float)) and abs(float(x) - hot_c) < 0.5]
            out.append(_fault("braiins:cgminer:temp_high" if is_braiins else "bitmain:temp_high",
                              "temp_chip", f"Chip temperature {hot_c:.1f} °C (chain {hot_ch})", "CGMiner temps", component=f"Chain {hot_ch} chip sensor"))
        if (valid_c or (cg_ghs is not None and cg_ghs > 0)) and any(isinstance(x, (int, float)) and (x <= 0 or x >= 125) for x in cg_chip_temps):
            bad_c = [i + 1 for i, x in enumerate(cg_chip_temps) if isinstance(x, (int, float)) and (x <= 0 or x >= 125)]
            out.append(_fault("braiins:cgminer:temp_sensor_error" if is_braiins else "bitmain:temp_sensor_error",
                              "temp_chip", f"Chain {bad_c} invalid chip temperature reading", "CGMiner temps", component=f"Chain {bad_c} chip sensor"))
    if cg_pcb_temps:
        valid_b = [float(x) for x in cg_pcb_temps if isinstance(x, (int, float)) and 0 < x < 125]
        hot_b = max(valid_b, default=None)
        if hot_b is not None and hot_b >= (80.0 if is_braiins else 75.0):
            out.append(_fault("braiins:cgminer:temp_high" if is_braiins else "bitmain:temp_high",
                              "temp_pcb", f"Board temperature {hot_b:.1f} °C", "CGMiner temps"))

    if cg_hw_pct is not None and cg_hw_pct >= 2.0 and not is_braiins:
        out.append(_fault("bitmain:hw_errors_high", "hw_pct", f"Device Hardware% = {cg_hw_pct:.2f}%", "CGMiner SUMMARY"))

    for cerr in r.get("cgminer_status_errors") or []:
        if not isinstance(cerr, dict):
            continue
        out.append(_fault("bitmain:cgminer_error", str(cerr.get("cmd") or "cgminer"),
                          f"{cerr.get('status')}: {cerr.get('msg') or 'error'}", "CGMiner API"))

    # Bitmain CGI / port 6060 warnings
    for bw in r.get("bitmain_warnings") or []:
        if isinstance(bw, dict):
            code = str(bw.get("code") or "warning")
            msg = str(bw.get("msg") or code)
            src_label = str(bw.get("source") or "Bitmain warning API")
        else:
            code = str(bw)
            msg = code
            src_label = "Bitmain warning API"
        prefix = code.split(":", 1)[0].strip().upper()[:1]
        hint = _6060_PREFIX_TEXT.get(prefix, "")
        txt = f"{msg} ({hint})" if hint and hint.lower() not in msg.lower() else msg
        out.append(_fault("bitmain:web_warning", code, txt, src_label))

    # --- Braiins REST / gRPC / TUNERSTATUS ----------------------------------
    for err in r.get("braiins_errors") or []:
        if not isinstance(err, dict):
            continue
        code = str(err.get("code") or "").strip().upper()
        msg = str(err.get("message") or err.get("name") or "").strip()
        if code and f"braiins:{code}" in CATALOG:
            out.append(_fault(f"braiins:{code}", code, msg or code, "Braiins OS errors"))
        else:
            out.append(_fault(f"braiins:unmapped:{_slug(code or msg)}", code or "-", msg or code or "unknown error", "Braiins OS errors"))

    st = str(r.get("braiins_status") or "")
    stn = st.lower().replace("_", "").replace(" ", "")
    braiins_stopped_explained = False
    if "thermalpause" in stn or "overheat" in stn:
        braiins_stopped_explained = True
        out.append(_fault("braiins:status:thermal_pause", st, st, "Braiins OS status"))
    elif "deadpools" in stn:
        braiins_stopped_explained = True
        out.append(_fault("braiins:status:dead_pools", st, st, "Braiins OS status"))
    elif "missinglicense" in stn:
        braiins_stopped_explained = True
        out.append(_fault("braiins:status:missing_license", st, st, "Braiins OS status"))
    elif "hardwareerror" in stn:
        braiins_stopped_explained = True
        out.append(_fault("braiins:status:hardware_error", st, st, "Braiins OS status"))
    elif "tunererror" in stn:
        braiins_stopped_explained = True
        out.append(_fault("braiins:status:tuner_error", st, st, "Braiins OS status"))
    elif "unsupportedhardware" in stn:
        braiins_stopped_explained = True
        out.append(_fault("braiins:status:unsupported_hardware", st, st, "Braiins OS status"))
    elif "applicationunavailable" in stn:
        braiins_stopped_explained = True
        out.append(_fault("braiins:status:application_unavailable", st, st, "Braiins OS status"))
    elif "dpscooldown" in stn:
        out.append(_fault("braiins:status:dps_cooldown", st, st, "Braiins OS status"))
    elif "paused" in stn or "userpause" in stn:
        braiins_stopped_explained = True
        out.append(_fault("braiins:status:paused", st, st, "Braiins OS status"))
    elif any(x in stn for x in ("coolingdown", "waitingwhilecold", "defrosting")):
        braiins_stopped_explained = True
        out.append(_fault("braiins:status:starting_wait", st, st, "Braiins OS status"))

    for tc in r.get("tuner_chains") or []:
        if not isinstance(tc, dict):
            continue
        tst = str(tc.get("status") or "").strip()
        if tst.lower() in {"error", "disabled", "restricted", "fault"}:
            out.append(_fault("braiins:tuner:chain_error", f"chain{tc.get('id', '?')}",
                              f"Chain {tc.get('id', '?')}: tuner {tst}", "Braiins TUNERSTATUS"))

    # Zero / low hashrate on Antminer (only after startup warmup and if not already explained by stopped/chain_missing/pools)
    if (
        cg_ghs is not None
        and cg_ghs <= 0
        and (cg_elapsed is None or cg_elapsed >= 180)
        and not braiins_stopped_explained
        and not cg_pools_dead
        and not r.get("braiins_errors")
    ):
        out.append(_fault("bitmain:zero_hashrate", "GHS 5s", "0 GH/s after boot warmup", "CGMiner SUMMARY"))
    elif (
        cg_ghs is not None
        and cg_ghs > 0
        and cg_ideal is not None
        and cg_ideal > 0
        and cg_ghs < cg_ideal * 0.75
        and (cg_elapsed is None or cg_elapsed >= 300)
        and not has_chain_missing
        and not braiins_stopped_explained
    ):
        out.append(_fault("bitmain:low_hashrate", "GHS 5s", f"{cg_ghs:.0f} GH/s vs ideal {cg_ideal:.0f} GH/s", "CGMiner SUMMARY"))

    # de-duplicate by key+code
    seen: set[str] = set()
    uniq = []
    for f in out:
        k = f["key"] + "|" + f["code"]
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    uniq.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 3))
    return uniq


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

LOG_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("bitmain:log:fan_lost", re.compile(r"ERROR_FAN_LOST", re.I)),
    ("bitmain:log:pic_temp", re.compile(r"fail to read pic temp", re.I)),
    ("bitmain:log:pic_version", re.compile(r"pic version mismatch", re.I)),
    ("bitmain:log:find_0_asic", re.compile(r"find 0 asic", re.I)),
    ("bitmain:log:power_off_board", re.compile(r"will power off hash ?board", re.I)),
    ("bitmain:log:power_lost", re.compile(r"ERROR_POWER_LOST", re.I)),
    ("bitmain:log:power_version", re.compile(r"get power type version failed", re.I)),
    ("bitmain:log:soc_init", re.compile(r"ERROR_SOC_INIT|soc init failed", re.I)),
    ("bitmain:log:eeprom", re.compile(r"ERROR_EEPROM_INFO", re.I)),
    ("bitmain:log:temp_high", re.compile(r"ERROR_TEMP_TOO_HIGH", re.I)),
    ("bitmain:log:read_temp", re.compile(r"fail to read temp(?!\w)", re.I)),
    ("axeos:log:asic_not_found", re.compile(r"ASIC \d+ not found|\b0 chip\(s\) detected", re.I)),
    ("axeos:log:chip_id", re.compile(r"CHIP_ID response mismatch", re.I)),
    ("axeos:log:high_temp", re.compile(r'Danger: High Temperature', re.I)),
    ("axeos:overheat_mode", re.compile(r"Device has overheated", re.I)),
    ("axeos:low_voltage", re.compile(r"Danger: Low Voltage", re.I)),
    ("axeos:hardware_fault:fan", re.compile(r"Fan Control Failed", re.I)),
]
_BRAIINS_CODE_RE = re.compile(r"\b(" + "|".join(sorted(BRAIINS_ERROR_CODES, key=len, reverse=True)) + r")\b[:\s-]")


def parse_log_lines(text: str, max_examples: int = 3) -> list[dict[str, Any]]:
    """Find known error lines in a captured miner log. Returns explained findings with counts and example lines."""
    found: dict[str, dict[str, Any]] = {}
    for line in str(text or "").splitlines()[-20000:]:
        s = line.strip()
        if not s:
            continue
        keys = [k for k, rx in LOG_PATTERNS if rx.search(s)]
        if not keys and ("error" in s.lower() or "err" in s.lower()):
            m = _BRAIINS_CODE_RE.search(s)
            if m and re.search(r"error code|\[(error|err)\]|error:", s, re.I):
                keys = [f"braiins:{m.group(1)}"]
        for k in keys:
            f = found.get(k)
            if f is None:
                f = _fault(k, k.rsplit(":", 1)[-1], s[:200], "captured log")
                f["count"] = 0
                f["examples"] = []
                found[k] = f
            f["count"] += 1
            if len(f["examples"]) < max_examples:
                f["examples"].append(scrub_text(s[:300]))
    out = list(found.values())
    out.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), -f["count"]))
    return out




def annotate_fault_causes(faults: list[dict[str, Any]], context: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Ensure every fault carries a timestamped cause (NK readings + matching firmware log line if available) and component."""
    ctx = context or {}
    now = float(ctx.get("now") or time.time())
    active_since = ctx.get("active_since") or {}
    last_vr = ctx.get("last_vr_overheat") if isinstance(ctx.get("last_vr_overheat"), dict) else None
    tuning_log = ctx.get("tuning_log") if isinstance(ctx.get("tuning_log"), list) else []
    fault_hist = ctx.get("fault_history") if isinstance(ctx.get("fault_history"), list) else []
    log_meta = ctx.get("log_meta") if isinstance(ctx.get("log_meta"), dict) else None
    log_lines = list(ctx.get("log_lines") or [])

    last_tune = next((t for t in tuning_log if isinstance(t, dict)), None)
    findings = (log_meta or {}).get("findings") or []

    for f in faults:
        if not isinstance(f, dict):
            continue
        fk = str(f.get("key") or "")
        fc = str(f.get("code") or "")
        uid = fk + "|" + fc
        if not f.get("component"):
            f["component"] = fc or str(f.get("source") or "device")
        first_ts = float(active_since.get(uid) or f.get("cause_ts") or now)
        f.setdefault("first_seen_ts", first_ts)
        f.setdefault("first_seen_iso", _fmt_ts_local(first_ts))

        cause_ts = first_ts
        parts: list[str] = []

        # Remembered VR overheat (critical for v1.0.37.1 OCTAxe watchdog reboot)
        if last_vr and last_vr.get("temp_c") is not None and any(tok in fk for tok in ("chip_power_rail_off", "vr_temp", "board_error:2", "shutdown", "watchdog", "zero_hashrate")):
            cts = float(last_vr.get("ts") or first_ts)
            cause_ts = cts
            parts.append(f"NK recorded {last_vr.get('sensor') or 'vrTemp'} {float(last_vr['temp_c']):.1f} °C at {_fmt_ts_local(cts)}")

        # Recent tuning action just before fault
        if last_tune and last_tune.get("ts"):
            tts = float(last_tune["ts"])
            if abs(first_ts - tts) <= 1800:
                act_s = f"{last_tune.get('field') or 'setting'} {last_tune.get('old')} -> {last_tune.get('new')}"
                parts.append(f"Last NK tuning: {act_s} at {_fmt_ts_local(tts)}")

        # Recent matching fault history event
        for he in fault_hist[:10]:
            if isinstance(he, dict) and he.get("key") == fk and he.get("ts"):
                hts = float(he["ts"])
                if not parts:
                    cause_ts = hts
                break

        # Matching firmware log finding or line (if captured)
        log_match = ""
        for fd in findings:
            if isinstance(fd, dict):
                ex_list = fd.get("examples") or []
                if fd.get("key") == fk or any(w in str(fd.get("title") or "").lower() for w in fk.replace(":", " ").split() if len(w) > 3):
                    log_match = str(ex_list[0]) if ex_list else str(fd.get("text") or "")
                    break
        if not log_match and log_lines:
            kws = [w for w in re.split(r"[^a-z0-9]+", (fk + " " + fc).lower()) if len(w) >= 3 and w not in {"axeos", "nerd", "bitmain", "braiins"}]
            for ln in reversed(log_lines[-200:]):
                low = ln.lower()
                if any(w in low for w in kws):
                    log_match = scrub_text(ln.strip()[:220])
                    break
        if log_match:
            f["log_line"] = log_match
            lts = float((log_meta or {}).get("ts") or cause_ts)
            parts.append(f"Firmware log ({_fmt_ts_local(lts)}): {log_match}")

        if not parts:
            parts.append(f"Detected at {_fmt_ts_local(cause_ts)}: {f.get('text') or f.get('title')}")
        f["cause_ts"] = cause_ts
        f["cause_iso"] = _fmt_ts_local(cause_ts)
        f["cause_summary"] = " | ".join(parts)
    return faults


# ---------------------------------------------------------------------------
# Fault history
# ---------------------------------------------------------------------------

class FaultHistory:
    """userdata/fault_history.json: appeared / cleared / user_reset / auto_restart events (capped)."""

    MAX = 2000

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self.events: list[dict[str, Any]] = []
        self.active: dict[str, dict[str, dict[str, Any]]] = {}
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.events = [e for e in raw.get("events") or [] if isinstance(e, dict)][-self.MAX:]
                    act = raw.get("active") or {}
                    self.active = {str(k): dict(v) for k, v in act.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                pass

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            data = {"schema": 1, "events": self.events[-self.MAX:], "active": self.active}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def add(self, device: str, event: str, now: float, save: bool = True, **fields: Any) -> dict[str, Any]:
        e = {"ts": now, "device": device, "event": event}
        e.update({k: v for k, v in fields.items() if v is not None})
        with self._lock:
            self.events.append(e)
            if len(self.events) > self.MAX:
                self.events = self.events[-self.MAX:]
        if save:
            self.save()
        return e

    def observe(self, device: str, faults: list[dict[str, Any]], now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Diff current faults against the stored active set. Returns (appeared, cleared)."""
        cur = {f["key"] + "|" + f["code"]: f for f in faults}
        appeared, cleared = [], []
        with self._lock:
            prev = self.active.get(device, {})
            for k, f in cur.items():
                if k not in prev:
                    appeared.append(f)
            for k, p in prev.items():
                if k not in cur:
                    cleared.append(dict(p, key=p.get("key") or k.split("|")[0]))
            self.active[device] = {k: {"key": f["key"], "code": f["code"], "text": f["text"], "title": f["title"], "severity": f["severity"],
                                       "since": (prev.get(k) or {}).get("since", now)} for k, f in cur.items()}
            if not self.active[device]:
                self.active.pop(device, None)
        for f in appeared:
            self.add(device, "appeared", now, save=False, key=f["key"], code=f["code"], text=f["text"], title=f["title"], severity=f["severity"])
        for p in cleared:
            self.add(device, "cleared", now, save=False, key=p.get("key"), code=p.get("code"), text=p.get("text"), title=p.get("title"),
                     severity=p.get("severity"), duration_sec=round(now - float(p.get("since") or now)))
        if appeared or cleared:
            self.save()
        return appeared, cleared

    def list(self, device: str = "", limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            ev = [e for e in self.events if not device or e.get("device") == device]
        return list(reversed(ev[-max(1, min(limit, self.MAX)):]))

    def actions_since(self, device: str, since: float) -> list[float]:
        with self._lock:
            return [float(e["ts"]) for e in self.events if e.get("device") == device and e.get("event") == "user_reset"
                    and not e.get("dry_run") and float(e.get("ts", 0)) >= since]

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            for e in self.events:
                if e.get("device") == old:
                    e["device"] = new
            if old in self.active:
                self.active[new] = self.active.pop(old)
        self.save()

    def remove(self, name: str) -> None:
        with self._lock:
            before = len(self.events)
            self.events = [e for e in self.events if e.get("device") != name]
            had_active = name in self.active
            self.active.pop(name, None)
        if len(self.events) != before or had_active:
            self.save()


# ---------------------------------------------------------------------------
# Clear / Reset planning (pure; the hub executes)
# ---------------------------------------------------------------------------

# Actions Miner Hearth may ever send. Factory reset / config reset are deliberately absent.
ALLOWED_ACTIONS = ("axe_clear_overheat", "axe_resume", "restart", "braiins_resume", "braiins_restart", "bitmain_reboot")
ACTION_LABELS = {
    "axe_clear_overheat": "Clear overheat flag",
    "axe_resume": "Resume mining",
    "restart": "Restart miner",
    "braiins_resume": "Resume mining (Braiins)",
    "braiins_restart": "Restart mining (Braiins)",
    "bitmain_reboot": "Reboot miner (Bitmain web)",
}
ACTION_WHAT = {
    "axe_clear_overheat": "PATCH /api/system {\"overheat_mode\": 0}",
    "axe_resume": "POST /api/system/resume",
    "restart": "POST /api/system/restart",
    "braiins_resume": "Braiins ResumeMining (gRPC, REST /api/v1/actions/resume fallback)",
    "braiins_restart": "Braiins Restart (gRPC, REST /api/v1/actions/restart fallback)",
    "bitmain_reboot": "HTTP Digest GET /cgi-bin/reboot.cgi",
}


def reset_plan(faults: list[dict[str, Any]], dev: dict[str, Any], opts: dict[str, Any], recent_actions: list[float], now: float) -> dict[str, Any]:
    """Pick the one safe Clear/Reset action for the current faults and say whether it is allowed right now.

    dev: {type, family ('axeos'|'nerd'|'braiins'|'bitmain'|'other'), online, asic_c, vr_c, asic_max_c, vr_max_c,
          has_web_password, has_bos_password}
    opts: faults settings {cool_asic_c, cool_vr_c, min_gap_min, max_per_day}
    """
    keys = {f["key"] for f in faults if f.get("severity") in {"problem", "warn"} and not f["key"].startswith("reset:")}
    fam = dev.get("family") or "other"
    action = ""
    needs_cool = False
    if not keys:
        return {"available": False, "action": "", "label": "", "enabled": False, "reason": "No active fault"}
    if fam == "axeos" or (fam == "nerd" and any(k.startswith("axeos:") for k in keys)):
        if "axeos:overheat_mode" in keys:
            action, needs_cool = "axe_clear_overheat", True
        elif "axeos:mining_paused" in keys and not ({"axeos:power_fault"} & keys):
            action, needs_cool = "axe_resume", True
        elif keys & {
            "axeos:power_fault", "axeos:hardware_fault", "axeos:hardware_fault:fan",
            "axeos:high_temp", "axeos:high_vr_temp", "axeos:temp_sensor_error",
            "axeos:zero_hashrate", "axeos:chip_power_rail_off", "axeos:fan_stalled", "axeos:dead_asic",
            "axeos:pool_disconnected", "axeos:wifi_disconnected",
        }:
            action = "restart"
            needs_cool = bool(keys & {"axeos:high_temp", "axeos:high_vr_temp"})
    if not action and fam in {"nerd", "axeos"} and any(k.startswith("nerd:") for k in keys):
        action = "restart"
        needs_cool = any(k in keys for k in ("nerd:board_error:1", "nerd:board_error:2", "nerd:asic_temp_high", "nerd:vr_temp_high"))
    if not action and fam == "braiins":
        if "braiins:status:thermal_pause" in keys or any(k in keys for k in ("braiins:T1", "braiins:T2", "braiins:T4", "braiins:cgminer:temp_high")):
            action, needs_cool = "braiins_resume", True
        elif "braiins:status:paused" in keys and len(keys) == 1:
            action, needs_cool = "braiins_resume", True
        elif any(k.startswith(("braiins:", "bitmain:")) for k in keys):
            action = "braiins_restart"
    if not action and fam == "bitmain" and any(k.startswith("bitmain:") for k in keys):
        action = "bitmain_reboot"
    if not action:
        return {"available": False, "action": "", "label": "", "enabled": False,
                "reason": "No remote reset is documented for this fault/firmware - follow the checks below"}
    assert action in ALLOWED_ACTIONS
    plan = {"available": True, "action": action, "label": ACTION_LABELS[action], "what": ACTION_WHAT[action], "enabled": True, "reason": "",
            "needs_cool": needs_cool}

    def block(msg: str) -> dict[str, Any]:
        plan["enabled"] = False
        plan["reason"] = msg
        return plan

    if not dev.get("online"):
        return block("Miner is offline")
    if action == "bitmain_reboot" and not dev.get("has_web_password"):
        return block("Save this miner's Bitmain web login (Details > Bitmain web login) to enable reboot")
    if action.startswith("braiins") and not dev.get("has_bos_password"):
        return block("Save the Braiins login on the card to enable this")
    asic, vr = dev.get("asic_c"), dev.get("vr_c")
    if needs_cool:
        ca, cv = float(opts.get("cool_asic_c", 45)), float(opts.get("cool_vr_c", 65))
        if asic is None and vr is None:
            return block("No temperature reading - cannot confirm it has cooled down")
        if asic is not None and asic >= ca:
            return block(f"Still hot: chip {asic:.0f} °C (wait until below {ca:.0f} °C)")
        if vr is not None and vr >= cv:
            return block(f"Still hot: VR {vr:.0f} °C (wait until below {cv:.0f} °C)")
    else:
        am, vm = dev.get("asic_max_c"), dev.get("vr_max_c")
        if asic is not None and am is not None and asic >= float(am):
            return block(f"Too hot to restart safely: chip {asic:.0f} °C (max {float(am):.0f} °C)")
        if vr is not None and vm is not None and vr >= float(vm):
            return block(f"Too hot to restart safely: VR {vr:.0f} °C (max {float(vm):.0f} °C)")
    gap = float(opts.get("min_gap_min", 10)) * 60.0
    day = [t for t in recent_actions if now - t < 86400]
    if day and now - max(day) < gap:
        return block(f"Rate limit: wait {int((gap - (now - max(day))) // 60) + 1} min before another reset")
    if len(day) >= int(opts.get("max_per_day", 4)):
        return block(f"Rate limit: {len(day)} resets in the last 24 h (max {int(opts.get('max_per_day', 4))})")
    return plan


# ---------------------------------------------------------------------------
# Redaction (diagnostic export, raw field viewer)
# ---------------------------------------------------------------------------

_PERSONAL_TOKENS = {"password", "passwd", "pass", "pwd", "token", "secret", "totp", "otp", "ssid", "wallet", "user", "username", "worker",
                    "email", "mail", "pin", "mac", "macaddr", "addr", "address", "hostname", "apikey", "cookie", "auth",
                    "authorization", "bssid", "payout"}
_SAFE_KEYS = {"keys", "keepalive"}


def _key_tokens(key: str) -> list[str]:
    k = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return [t for t in re.split(r"[^A-Za-z0-9]+", k.lower()) if t]


def is_personal_key(key: str) -> bool:
    toks = _key_tokens(key)
    if not toks or str(key).lower() in _SAFE_KEYS:
        return False
    if "has" in toks:  # has_password / web_has_password are booleans, not secrets
        return False
    joined = "".join(toks)
    if joined in {"macaddr", "macaddress", "stratumuser", "fallbackstratumuser", "pooluser", "hostip", "ipaddr", "ipaddress", "ip", "ipv4", "localip", "wifissid", "otpsession", "sessionid", "sessiontoken", "privatekey", "apikey"}:
        return True
    return any(t in _PERSONAL_TOKENS for t in toks)


_SCRUB = [
    (re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"), "[mac]"),
    (re.compile(r"\b(?:bc1|tb1|bcrt1)[02-9ac-hj-np-z]{11,87}\b", re.I), "[address]"),
    (re.compile(r"\b(?:bitcoincash:|ecash:|bchtest:)?[qp][02-9ac-hj-np-z]{40,}\b", re.I), "[address]"),
    (re.compile(r"\b[13mn2][a-km-zA-HJ-NP-Z1-9]{25,34}\b"), "[address]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
    (re.compile(r"(stratum\+\w+://)[^@\s/]+@"), r"\1[user]@"),
    (re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}\b"), r"\1.x"),
]


def scrub_text(s: str) -> str:
    out = str(s)
    for rx, rep in _SCRUB:
        out = rx.sub(rep, out)
    return out


def redact(obj: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "[...]"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if is_personal_key(str(k)) and not isinstance(v, (dict, list)):
                if v not in (None, "", [], {}):
                    out[k] = "[redacted]"
                else:
                    out[k] = v
            elif is_personal_key(str(k)):
                out[k] = "[redacted]"
            else:
                out[k] = redact(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, depth + 1) for v in obj]
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj


def flatten_fields(obj: Any, prefix: str = "", out: Optional[dict[str, Any]] = None, limit: int = 600, depth: int = 0) -> dict[str, Any]:
    """Flatten an API response into {path: value} for the Details viewer; personal keys dropped, strings scrubbed."""
    if out is None:
        out = {}
    if len(out) >= limit or depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if is_personal_key(str(k)):
                continue
            flatten_fields(v, f"{prefix}.{k}" if prefix else str(k), out, limit, depth + 1)
    elif isinstance(obj, list):
        if obj and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in obj) and len(obj) <= 64:
            out[prefix] = list(obj)
        else:
            for i, v in enumerate(obj[:64]):
                flatten_fields(v, f"{prefix}[{i}]", out, limit, depth + 1)
    elif isinstance(obj, bool) or obj is None:
        out[prefix] = obj
    elif isinstance(obj, (int, float)):
        out[prefix] = obj
    else:
        s = str(obj)
        if len(s) <= 160:
            out[prefix] = scrub_text(s)
    return out


# ---------------------------------------------------------------------------
# HTTP Digest auth (Bitmain stock web UI CGI)
# ---------------------------------------------------------------------------

def _parse_challenge(header: str) -> dict[str, str]:
    h = header.strip()
    if h.lower().startswith("digest "):
        h = h[7:]
    return {k.lower(): (v1 if v1 else v2) for k, v1, v2 in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', h)}


def _H(alg: str, data: str) -> str:
    if alg.upper().startswith("SHA-256"):
        return hashlib.sha256(data.encode("utf-8")).hexdigest()
    return hashlib.md5(data.encode("utf-8")).hexdigest()  # noqa: S324 - required by RFC 2617 Digest on the miner


def digest_authorization(method: str, uri: str, username: str, password: str, challenge: dict[str, str], nc: int = 1, cnonce: Optional[str] = None) -> str:
    realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
    alg = challenge.get("algorithm", "MD5") or "MD5"
    qop_opts = [q.strip() for q in (challenge.get("qop") or "").split(",") if q.strip()]
    cnonce = cnonce or secrets.token_hex(8)
    ha1 = _H(alg, f"{username}:{realm}:{password}")
    if alg.lower().endswith("-sess"):
        ha1 = _H(alg, f"{ha1}:{nonce}:{cnonce}")
    ha2 = _H(alg, f"{method}:{uri}")
    ncs = f"{nc:08x}"
    if "auth" in qop_opts:
        resp = _H(alg, f"{ha1}:{nonce}:{ncs}:{cnonce}:auth:{ha2}")
        parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"', f"algorithm={alg}",
                 f'response="{resp}"', "qop=auth", f"nc={ncs}", f'cnonce="{cnonce}"']
    else:
        resp = _H(alg, f"{ha1}:{nonce}:{ha2}")
        parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"', f"algorithm={alg}", f'response="{resp}"']
    if challenge.get("opaque"):
        parts.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(parts)


def digest_request(method: str, url: str, username: str, password: str, timeout: float = 8.0, max_bytes: int = 2_000_000) -> tuple[int, bytes]:
    """One request with HTTP Digest auth (MD5 / SHA-256, qop=auth). Returns (status, body). Raises ConnectionError on network failure."""
    u = urllib.parse.urlsplit(url)
    if u.scheme != "http":
        raise ValueError("only http:// miner URLs are supported")
    path = u.path or "/"
    if u.query:
        path += "?" + u.query

    def once(auth: Optional[str]) -> tuple[int, bytes, Any]:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        try:
            hdrs = {"User-Agent": "MinerHearth", "Connection": "close"}
            if auth:
                hdrs["Authorization"] = auth
            conn.request(method, path, headers=hdrs)
            resp = conn.getresponse()
            body = resp.read(max_bytes)
            return resp.status, body, resp.headers
        finally:
            conn.close()

    try:
        status, body, headers = once(None)
        if status != 401:
            return status, body
        chal_hdr = ""
        for k, v in headers.items():
            if k.lower() == "www-authenticate" and v.lower().startswith("digest"):
                if not chal_hdr or "sha-256" in v.lower():
                    chal_hdr = v
        if not chal_hdr:
            return status, body
        auth = digest_authorization(method, path, username, password, _parse_challenge(chal_hdr))
        status, body, _ = once(auth)
        return status, body
    except (OSError, http.client.HTTPException) as exc:
        raise ConnectionError(f"{method} {u.hostname}{u.path} failed: {exc}") from exc


def now_ts() -> float:
    return time.time()
