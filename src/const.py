import os
import tkinter as tk
from tkinter import ttk
from typing import Dict, List, Tuple


DEFAULT_PROCESS_NAME = "x86GP2_inline.exe"
DEFAULT_BASE_ANCHOR = "0xBF5AE0"        # anchor inside car0 record
DEFAULT_ANCHOR_STRUCT_OFFSET = "0x14"   # anchor is record_start + 0x14
DEFAULT_RECORD_SIZE = "0x330"
DEFAULT_CAR_COUNT = 26
DEFAULT_REFRESH_MS = 500

FOOTER_H=32

CONFIG_PATH = os.path.abspath("gp2mem_config.json")

#raw u32 @ struct+0x14 == 288 kph when raw==1103508588
SPEED_RAW_PER_KPH = 1103508588 / 288


# ----------------------------
# Windows memory constants
# ----------------------------
MEM_COMMIT = 0x1000

PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100


# ----------------------------
# Struct offsets (IDA offsets)
# ----------------------------
OFS = {
    "vAngle": 0x00,              # i16
    "timeAlarm004": 0x04,        # i32
    "splitNr": 0x08,             # u8
    "anaClutch": 0x09,           # u8
    "segPosX": 0x0A,             # i16
    "segDist2": 0x0C,            # i16
    "field_E": 0x0E,             # i16
    "curSeg": 0x10,              # i32

    # anchor is at 0x14
    "speed_raw_u32": 0x14,       # u32 (kph uses this)
    "speed_i16": 0x16,           # i16
    "field_18": 0x18,            # i16
    "failLap": 0x1A,             # u8
    "failSeg": 0x1B,             # u8
    "segDist": 0x1C,             # i16
    "segDistFactor": 0x1E,       # i16
    "field_20": 0x20,            # i16

    "lapNr": 0x22,               # u8
    "flags_23": 0x23,            # u8
    "gear": 0x24,                # u8
    "teamNr": 0x25,              # u8

    "xPos": 0x28,                # i32
    "yPos": 0x2C,                # i32
    "xSpeed": 0x30,              # i32
    "ySpeed": 0x34,              # i32

    "flagsFail2": 0x3C,          # u8
    "field_3D": 0x3D,            # u8
    "curbrel_3E": 0x3E,          # i16
    "timePrevBest": 0x40,        # i32

    "weight_ida": 0x44,          # i16

    "grip": 0x46,                # i16
    "convSteer": 0x48,           # i16
    "lockedWheelRel": 0x4C,      # u8
    "flagsGrass_4D": 0x4D,       # u8

    "timeLapStart": 0x54,        # i32

    "legacy_i16_at_58": 0x58,    # i16

    "speed2": 0x5C,              # i16
    "anaBrake": 0x5F,            # u8
    "revs": 0x62,                # i16
    "place_byte": 0x66,          # u8

    "advspeed_70": 0x70,         # i16
    "accel_72": 0x72,            # i16
    "anaSteer": 0x7A,            # i16
    "flags_7C": 0x7C,            # u8
    "digCtrlInput": 0x7D,        # u8
    "drvAids": 0x81,             # u8
    "usedDevices": 0x83,         # u8

    "timerVarSeq": 0x8C,         # i32
    "flags_90": 0x90,            # u8
    "flags_91": 0x91,            # u8

    "throttle": 0x92,            # i16
    "field_94": 0x94,            # u8
    "anaThrottle": 0x95,         # u8

    "CrashAnim098": 0x98,        # u8
    "flagsFail1": 0x99,          # u8

    "enginePower": 0xA2,         # i16
    "racePos": 0xA4,             # u8
    "carId": 0xA6,               # u8
    "csIndex": 0xA7,             # u8

    "failType": 0xAC,            # u8
    "flags_AD": 0xAD,            # u8

    "gripFactor": 0xB8,          # i16
    "prevGear": 0xBA,            # u8
    "littleCarDisp": 0xBB,       # u8

    "PlankStuff_D6": 0xD6,       # u8
    "numStopsDone": 0xD7,        # u8

    "display168": 0x168,         # u8
    "flags_169": 0x169,          # u8
    "flags_16A": 0x16A,          # u8

    "downforceRelRW": 0x16C,     # i32
    "downforceRelFW": 0x170,     # i16
    "rearwingRel": 0x174,        # i32

    "fuelUnit": 0x180,           # i16

    "damageRel": 0x228,          # i32
    "ledInfo": 0x232,            # u8

    "tirewear": 0x238,           # i32[4]
    "suspTravel": 0x258,         # i32[4]

    "brakes": 0x270,             # i16
    "failureType_": 0x272,       # u8
    "ComeInType": 0x273,         # u8
    "numPitStops": 0x274,        # u8
    "pitstop1": 0x275,           # u8
    "pitstop2": 0x276,           # u8
    "pitstop3": 0x277,           # u8

    "fuelLoad": 0x298,           # u32
    "pSeg2": 0x2C4,              # i32

    "timeLastSpl1": 0x2CC,       # i32
    "timeLastSpl2": 0x2D0,       # i32
    "timeLast": 0x2D4,           # i32
    "timeBestSpl1": 0x2D8,       # i32
    "timeBestSpl2": 0x2DC,       # i32
    "timeBest": 0x2E0,           # i32

    "gearRatios": 0x2E8,         # u8[6]
    "fuelLoadLaps": 0x306,       # i16
}

OFS_WHEELSPIN = 0x13C      # i32[4]
OFS_DAMAGEDPARTS = 0x14C   # i32[4]


BIT_MAPS: Dict[str, Dict[int, str]] = {
    "flags_23": {1: "checkpoint/end of lap?", 5: "in pits (guess)", 7: "steer into pit"},
    "field_3D": {5: "separate gear up/down devices", 6: "opp lock deactivated", 7: "steering help deactivated"},
    "flags_7C": {0: "player", 2: "cc", 4: "turning gear"},
    "drvAids": {
        0: "auto brake", 1: "auto shift", 2: "auto forward", 3: "indestructable",
        4: "f5 line", 5: "show gear", 6: "traction control", 7: "unknown",
    },
    "usedDevices": {
        0: "clutchMode", 1: "analog brkMode", 2: "analog accMode", 3: "analog steerMode",
        4: "analog clutchDevice", 5: "analog brkDevice in use", 6: "analog accDevice", 7: "analog steerDevice",
    },
    "flags_90": {5: "driver out of cockpit", 7: "car invisible"},
    "flags_91": {1: "engine sound on", 7: "marshall drawn at rear"},
    "flags_AD": {2: "pit in request", 5: "still accepts throttle? (guess)"},
    "littleCarDisp": {
        0: "plank rear", 1: "plank front", 2: "wheel RF", 3: "wheel LF",
        4: "wheel RR", 5: "wheel LR", 6: "rearwing", 7: "frontwing",
    },
    "PlankStuff_D6": {6: "plank rear red", 7: "plank front red"},
    "display168": {2: "show blackflag", 3: 'show "is in pits"', 5: "update lcd?"},
    "flags_169": {2: "will get failure", 3: "failure active", 5: "passed split/line"},
    "flags_16A": {1: "elevated in box", 3: "in pitlane", 7: "limp mode (guess)"},
    "ledInfo": {
        0: "display now", 1: "timer active", 2: "show gap", 3: "show split",
        5: "message from box", 6: "failure active (guess)", 7: "start logging",
    },
}


# ----------------------------
# Type info
# ----------------------------
TYPE_INFO = {
    "u8":  ("B", 1, 0, 0xFF),
    "i8":  ("b", 1, -128, 127),
    "u16": ("H", 2, 0, 0xFFFF),
    "i16": ("h", 2, -32768, 32767),
    "u32": ("I", 4, 0, 0xFFFFFFFF),
    "i32": ("i", 4, -2147483648, 2147483647),
    "f32": ("f", 4, None, None),
}

SpinboxWidget = getattr(ttk, "Spinbox", tk.Spinbox)


# ----------------------------
# Enums (easy dropdown editing)
# ----------------------------
FAIL_TYPE_ENUM: List[Tuple[int, str]] = [
    (0, "Suspension"),
    (1, "Loose wheel"),
    (2, "Puncture"),
    (3, "Engine problem"),
    (4, "Transmission problem"),
    (5, "Oil leak"),
    (6, "Throttle problem"),
    (7, "Electric problem"),
    (8, "Water leak"),
    (9, "Brake problem"),
]

COME_IN_ENUM: List[Tuple[int, str]] = [
    (0, "Come in now (0)"),
    (1, "Come in now (1)"),
    (2, "Come in now (2)"),
    (3, "Times disqualified"),
    (4, "Worn tires"),
]

SPLIT_ENUM: List[Tuple[int, str]] = [
    (0, "split1"),
    (1, "split2"),
    (2, "line"),
]

ENUM_FIELDS: Dict[str, List[Tuple[int, str]]] = {
    "splitNr": SPLIT_ENUM,
    "failType": FAIL_TYPE_ENUM,
    "failureType_": FAIL_TYPE_ENUM,
    "ComeInType": COME_IN_ENUM,
}


# ----------------------------
# (Type syntax: u8/i16/u32/i32, arrays like i32[4], u8[6])
# ----------------------------
STRUCT_LAYOUT_TEXT = r"""
0x000 vAngle i16
0x002 field_2 u8
0x003 field_3 u8
0x004 timeAlarm004 i32
0x008 splitNr u8
0x009 anaClutch u8
0x00A segPosX_0A i16
0x00C segDist2_ i16
0x00E field_E i16
0x010 curSeg i32
0x014 field_14 i16
0x016 speed i16
0x018 field_18 i16
0x01A failLap u8
0x01B failSeg u8
0x01C segDist i16
0x01E segDistFactor i16
0x020 field_20 i16
0x022 lapNr u8
0x023 flags_23 u8
0x024 gear u8
0x025 teamNr u8
0x026 field_26 i16
0x028 xPos i32
0x02C yPos i32
0x030 xSpeed i32
0x034 ySpeed i32
0x038 field_38 i32
0x03C flagsFail2 u8
0x03D field_3D u8
0x03E curbrel_3E i16
0x040 timePrevBest i32
0x044 weight i16
0x046 grip i16
0x048 convSteer i16
0x04A field_4A i16
0x04C lockedWheelRel u8
0x04D flagsGrass_4D u8
0x04E field_4E i16
0x050 field_50 i32
0x054 timeLapStart i32
0x058 rearWingRel2 i16
0x05A field_5A i16
0x05C speed2 i16
0x05E field_5E u8
0x05F anaBrake u8
0x060 field_60 i16
0x062 revs i16
0x064 field_64 i16
0x066 CpyFromRacePos_ u8
0x067 field_67 u8
0x068 field_68 i32
0x06C field_6C i16
0x06E field_6E i16
0x070 advspeed_70 i16
0x072 accel__72 i16
0x074 field_74 i16
0x076 field_76 i16
0x078 field_78 i16
0x07A anaSteer i16
0x07C flags_7C u8
0x07D digCtrlInput u8
0x07E field_7E i16
0x080 field_80 u8
0x081 drvAids u8
0x082 field_82 u8
0x083 usedDevices u8
0x084 field_84 i16
0x086 field_86 i16
0x088 field_88 i16
0x08A field_8A i16
0x08C timerVarSeq i32
0x090 flags_90 u8
0x091 flags_91 u8
0x092 throttle i16
0x094 field_94 u8
0x095 anaThrottle u8
0x096 field_96 i16
0x098 CrashAnim098 u8
0x099 flagsFail1 u8
0x09A timeAlarm09A i32
0x09E field_9E i32
0x0A2 enginePower i16
0x0A4 racePos u8
0x0A5 field_A5 u8
0x0A6 carId u8
0x0A7 csIndex u8
0x0A8 field_A8 i32
0x0AC failType u8
0x0AD flags_AD u8
0x0AE field_AE i16
0x0B0 field_B0 i16
0x0B2 gripFact2_ i16
0x0B4 field_B4 i16
0x0B6 field_B6 u8
0x0B7 field_B7 u8
0x0B8 gripFactor i16
0x0BA prevGear u8
0x0BB littleCarDisp u8
0x0BC pFirstCar u32
0x0C0 pCar0c0 u32
0x0C4 pCar0c4 u32
0x0C8 field_C8 i16
0x0CA field_CA i16
0x0CC field_CC u8
0x0CD field_CD u8
0x0CE field_CE i16
0x0D0 pCar0d0 u32
0x0D4 field_D4 i16
0x0D6 PlankStuff_D6 u8
0x0D7 numStopsDone u8
0x0D8 timeAlarm0d8 i32
0x0DC pCar0dc u32
0x0E0 pCar0e0 u32
0x0E4 pCar0e4 u32
0x0E8 field_E8 u8
0x0E9 field_E9 u8
0x0EA field_EA i16
0x0EC field_EC i16
0x0EE field_EE i16
0x0F0 field_F0 u8
0x0F1 field_F1 u8
0x0F2 field_F2 u8
0x0F3 field_F3 u8
0x0F4 field_F4 i16
0x0F6 field_F6 i16
0x0F8 segLenMinDist_ i16
0x0FA field_FA i16
0x0FC field_FC i32
0x100 field_100 i32
0x104 field_104 i32
0x108 field_108 i32
0x10C field_10C i32[4]
0x11C wheel_11C i32[4]
0x12C wheel_12C i32[4]
0x13C wheelSpin__13C i32[4]
0x14C damagedParts i32[4]
0x15C field_15C i32
0x160 field_160 i32
0x164 field_164 i32
0x168 display168 u8
0x169 flags_169 u8
0x16A flags_16A u8
0x16B field_16B u8
0x16C downforceRelRW i32
0x170 downforceRelFW i16
0x172 steerLowSens u8
0x173 accLowSens u8
0x174 rearwingRel i32
0x178 field_178 u8
0x179 field_179 u8
0x17A field_17A u8
0x17B field_17B u8
0x17C field_17C u8
0x17D field_17D u8
0x17E field_17E u8
0x17F field_17F u8
0x180 fuelUnit i16
0x182 field_182 u8
0x183 flags_183 u8
0x184 field_184 u8
0x185 field_185 u8
0x186 field_186 u8
0x187 field_187 u8
0x188 suSprings i32[4]
0x198 suRifeheights i32[4]
0x1A8 suSlowRebounds i32[4]
0x1B8 suFastRebounds i32[4]
0x1C8 suSlowBumps i32[4]
0x1D8 suFastBumps i32[4]
0x1E8 field_1E8 i32[4]
0x1F8 field_1F8 i32[4]
0x208 field_208 i32[4]
0x218 rideHeights i32[4]
0x228 damageRel i32
0x22C field_22C i32
0x230 field_230 i16
0x232 ledInfo u8
0x233 field_233 u8
0x234 field_234 i16
0x236 field_236 i16
0x238 tirewear i32[4]
0x248 calc_248 i32[4]
0x258 suspTravel_ i32[4]
0x268 rarb i32
0x26C farb i32
0x270 brakes i16
0x272 failureType_ u8
0x273 ComeInType u8
0x274 numPitStops u8
0x275 pitstop1_ u8
0x276 pitstop2_ u8
0x277 pitstop3_ u8
0x278 calc_278 i32[4]
0x288 notOnDamper_ i32[4]
0x298 fuelLoad u32
0x29C field_29C i32
0x2A0 field_2A0 i32
0x2A4 field_2A4 u8[4]
0x2A8 grip__2A8 i16
0x2AA field_2AA i16
0x2AC wheel_2AC i32[4]
0x2BC angleLaengs i32
0x2C0 angleQuer i32
0x2C4 pSeg2 i32
0x2C8 field_2C8 i16
0x2CA field_2CA u8
0x2CB field_2CB u8
0x2CC timeLastSpl1 i32
0x2D0 timeLastSpl2 i32
0x2D4 timeLast i32
0x2D8 timeBestSpl1 i32
0x2DC timeBestSpl2 i32
0x2E0 timeBest i32
0x2E4 field_2E4 i32
0x2E8 gearRatios u8[6]
0x2EE brakeLowSens u8
0x2EF clutchLowSens u8
0x2F0 packers i32[4]
0x300 field_300 i32
0x304 convSteerMaxLock u8
0x305 steerRedWCarSp u8
0x306 fuelLoadLaps i16
0x308 time308 i32
0x30C time_30C i32
0x310 timeAlarm310 i32
0x314 field_314 u8
0x315 SndsInPit315 u8
0x316 field_316 i16
0x318 timeAlarm318 i32
0x31C field_31C i16
0x31E field_31E u8
0x31F field_31F u8
0x320 timeAlarm320 i32
0x324 timeAlarm324 i32
0x328 timeAlarm328 i32
0x32C field_32C u8
0x32D field_32D u8
0x32E field_32E u8
0x32F field_32F u8
"""