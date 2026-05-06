import tkinter as tk
from tkinter import filedialog, messagebox
import os
import copy
import time
import tempfile
import subprocess
import threading
import wave
import math
import shutil

# ============================================================================
# Cython core bootstrap (single-file edition).
#
# The full Cython source for the FCEUX-style NES core lives as a string at
# the bottom of this file (_CYTHON_CORE_SOURCE). On first launch we try to
# import a pre-built `catsnescore_0_2` module; if that fails we materialize
# the embedded .pyx next to this script and let pyximport compile it on the
# fly. If Cython isn't available at all we fall through to the Pure Python
# backend defined further down.
# ============================================================================

import sys

CORE_LOADED = False
CORE_NAME = "Pure Python Backend"
nes_core = None


def _bootstrap_cython_core():
    """Return (module, label) or (None, None) on failure."""
    # 1. Pre-built first (fastest path on a warmed cache).
    try:
        import catsnescore_0_2 as core  # type: ignore
        return core, "Cat'snescore 0.2 (Cython, prebuilt)"
    except Exception:
        pass
    # 2. Older Cython cores users may already have around.
    for name, label in (
        ("catsnescore_0_1", "Cat'snescore 0.1 (Cython)"),
        ("gemininesemu0_1", "gemininesemu0_1 (Cython)"),
    ):
        try:
            return __import__(name), label
        except Exception:
            pass
    # 3. Build the embedded .pyx via pyximport.
    try:
        import pyximport  # noqa: F401
    except Exception:
        return None, None
    try:
        here = os.path.dirname(os.path.abspath(__file__)) or "."
    except Exception:
        here = "."
    target_dir = here if os.access(here, os.W_OK) else tempfile.gettempdir()
    pyx_path = os.path.join(target_dir, "catsnescore_0_2.pyx")
    try:
        # Always rewrite so an updated frontend ships an updated core.
        with open(pyx_path, "w") as fh:
            fh.write(_CYTHON_CORE_SOURCE)
    except OSError:
        return None, None
    if target_dir not in sys.path:
        sys.path.insert(0, target_dir)
    try:
        import pyximport
        pyximport.install(language_level=3)
        import catsnescore_0_2 as core  # type: ignore
        return core, "Cat'snescore 0.2 (Cython, autobuilt)"
    except Exception as e:
        print("[catsnescore] pyximport build failed:", e)
        return None, None


# Cython core source string is defined just below; bootstrap runs after.
_CYTHON_CORE_SOURCE = r'''
# cython: boundscheck=False, wraparound=False, cdivision=True, initializedcheck=False
# distutils: language=c
"""
catsnescore_0_2 — fast Cython NES core with FCEUX-style feature coverage.

What's inside, meow~:
  * Full 6502 CPU with all 151 official opcodes plus the common unofficial
    set (LAX, SAX, DCP, ISB, SLO, RLA, SRE, RRA, ANC, ALR, ARR, AXS, NOPs).
  * Scanline-rendered PPU with sprite 0 hit, sprite overflow, 8x8/8x16
    sprites, full PPUMASK (greyscale + RGB emphasis), all mirroring modes
    (H/V/single-low/single-high/four-screen).
  * All 5 APU channels (Pulse 1, Pulse 2, Triangle, Noise, DMC) with frame
    counter, length counters, sweep, envelope, linear counter, frame IRQ,
    DMC IRQ, and a Blip-style mixer producing 16-bit PCM @ ~22050 Hz.
  * Mappers: NROM (0), MMC1 / SxROM (1), UxROM (2), CNROM (3),
             MMC3 / TxROM (4) with scanline IRQ, AxROM (7), MMC2 (9).
  * Two NES controllers, battery-backed PRG RAM ($6000-$7FFF), save states.

Public API (consumed by gemininesemu0_2.py):
  Core()
  Core.load_rom(path) -> bool
  Core.run_frame()    -> str
  Core.get_framebuffer() -> bytes   (length 256*240, palette indices 0-63)
  Core.consume_audio()   -> bytes   (signed-16 LE PCM for the last frame)
  Core.set_controller1(state); Core.set_controller2(state)
  Core.reset(); Core.save_state(); Core.load_state(d)
  Core.consume_apu_activity() -> int   (legacy hook for the UI beep)
  Core.ppu  (object exposing NES_RGB, mask, scanline)
  Core.rom_loaded; Core.debug_info
"""

from libc.stdint cimport uint8_t, uint16_t, uint32_t, int16_t, int32_t, int64_t
from libc.string cimport memset, memcpy
from cpython.bytes cimport PyBytes_FromStringAndSize
import os
import cython


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NES_RGB = [
    (84, 84, 84), (0, 30, 116), (8, 16, 144), (48, 0, 136),
    (68, 0, 100), (92, 0, 48), (84, 4, 0), (60, 24, 0),
    (32, 42, 0), (8, 58, 0), (0, 64, 0), (0, 60, 0),
    (0, 50, 60), (0, 0, 0), (0, 0, 0), (0, 0, 0),
    (152, 150, 152), (8, 76, 196), (48, 50, 236), (92, 30, 228),
    (136, 20, 176), (160, 20, 100), (152, 34, 32), (120, 60, 0),
    (84, 90, 0), (40, 114, 0), (8, 124, 0), (0, 118, 40),
    (0, 102, 120), (0, 0, 0), (0, 0, 0), (0, 0, 0),
    (236, 238, 236), (76, 154, 236), (120, 124, 236), (176, 98, 236),
    (228, 84, 236), (236, 88, 180), (236, 106, 100), (212, 136, 32),
    (160, 170, 0), (116, 196, 0), (76, 208, 32), (56, 204, 108),
    (56, 180, 204), (60, 60, 60), (0, 0, 0), (0, 0, 0),
    (236, 238, 236), (168, 204, 236), (188, 188, 236), (212, 178, 236),
    (236, 174, 236), (236, 174, 212), (236, 180, 176), (228, 196, 144),
    (204, 210, 120), (180, 222, 120), (168, 226, 144), (152, 226, 180),
    (160, 214, 228), (160, 162, 160), (0, 0, 0), (0, 0, 0),
]

cdef uint8_t LENGTH_TABLE[32]
LENGTH_TABLE[:] = [
    10, 254, 20,  2, 40,  4, 80,  6, 160,  8, 60, 10, 14, 12, 26, 14,
    12,  16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30,
]

cdef uint8_t DUTY_TABLE[4][8]
DUTY_TABLE[0][:] = [0, 1, 0, 0, 0, 0, 0, 0]
DUTY_TABLE[1][:] = [0, 1, 1, 0, 0, 0, 0, 0]
DUTY_TABLE[2][:] = [0, 1, 1, 1, 1, 0, 0, 0]
DUTY_TABLE[3][:] = [1, 0, 0, 1, 1, 1, 1, 1]

cdef uint8_t TRIANGLE_SEQ[32]
TRIANGLE_SEQ[:] = [
    15, 14, 13, 12, 11, 10,  9,  8,  7,  6,  5,  4,  3,  2,  1,  0,
     0,  1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15,
]

cdef uint16_t NOISE_PERIODS[16]
NOISE_PERIODS[:] = [4, 8, 16, 32, 64, 96, 128, 160,
                    202, 254, 380, 508, 762, 1016, 2034, 4068]

cdef uint16_t DMC_RATES[16]
DMC_RATES[:] = [428, 380, 340, 320, 286, 254, 226, 214,
                190, 160, 142, 128, 106,  84,  72,  54]

# 6502 status flag bits.
cdef uint8_t FLAG_C = 0x01
cdef uint8_t FLAG_Z = 0x02
cdef uint8_t FLAG_I = 0x04
cdef uint8_t FLAG_D = 0x08
cdef uint8_t FLAG_B = 0x10
cdef uint8_t FLAG_U = 0x20
cdef uint8_t FLAG_V = 0x40
cdef uint8_t FLAG_N = 0x80

# Audio: synthesize ~22050 Hz mono.
cdef int AUDIO_SR = 22050
cdef int AUDIO_PER_FRAME = 22050 // 60   # 367 samples / frame


# ---------------------------------------------------------------------------
# Cart — handles iNES parsing and all supported mappers.
# ---------------------------------------------------------------------------

cdef class Cart:
    cdef public uint8_t mapper
    cdef public uint8_t mirroring         # 0=V-mirror,1=H-mirror,2=SS lo,3=SS hi,4=4-screen
    cdef public int prg_banks_16k
    cdef public int chr_banks_8k
    cdef public bint has_battery
    # ROM data (kept as bytes, indexed via Python access for simplicity).
    cdef public bytes prg_rom
    cdef public bytes chr_rom
    # Working RAM at $6000-$7FFF (battery-backed if has_battery).
    cdef public bytearray prg_ram
    # CHR-RAM (used when chr_banks_8k == 0 or by some mappers like AxROM).
    cdef public bytearray chr_ram
    # Mapper-specific live state.
    cdef public uint8_t mmc1_shift, mmc1_ctrl, mmc1_chr0, mmc1_chr1, mmc1_prg
    cdef public uint8_t uxrom_bank
    cdef public uint8_t cnrom_chr
    cdef public uint8_t mmc3_select
    cdef public uint8_t mmc3_banks[8]
    cdef public uint8_t mmc3_prg_mode, mmc3_chr_mode
    cdef public uint8_t mmc3_irq_latch, mmc3_irq_counter
    cdef public bint mmc3_irq_enable, mmc3_irq_reload, mmc3_irq_pending
    cdef public uint8_t axrom_prg
    cdef public uint8_t mmc2_latch_fd, mmc2_latch_fe
    cdef public uint8_t mmc2_chr_banks[4]    # 0:$0000_FD, 1:$0000_FE, 2:$1000_FD, 3:$1000_FE
    cdef public uint8_t mmc2_prg
    # Convenience cache.
    cdef public int prg_size
    cdef public int chr_size

    def __cinit__(self):
        self.prg_ram = bytearray(0x2000)
        self.chr_ram = bytearray(0x2000)
        self.prg_rom = b""
        self.chr_rom = b""
        self.mmc1_shift = 0x10
        self.mmc1_ctrl = 0x0C
        cdef int i
        for i in range(8):
            self.mmc3_banks[i] = 0
        self.mmc2_latch_fd = 0xFD
        self.mmc2_latch_fe = 0xFE
        for i in range(4):
            self.mmc2_chr_banks[i] = 0

    cpdef bint load(self, str path):
        with open(path, "rb") as f:
            data = f.read()
        if len(data) < 16 or data[:4] != b"NES\x1a":
            return False
        cdef int prg = data[4]
        cdef int chr_ = data[5]
        cdef int flag6 = data[6]
        cdef int flag7 = data[7]
        self.mapper = ((flag7 & 0xF0) | ((flag6 & 0xF0) >> 4)) & 0xFF
        if flag6 & 0x08:
            self.mirroring = 4
        else:
            self.mirroring = 0 if (flag6 & 1) == 0 else 1
        self.has_battery = (flag6 & 0x02) != 0
        self.prg_banks_16k = prg
        self.chr_banks_8k = chr_
        cdef int off = 16
        if flag6 & 0x04:
            off += 512
        cdef int prg_sz = prg * 16384
        cdef int chr_sz = chr_ * 8192
        if off + prg_sz > len(data):
            return False
        self.prg_rom = bytes(data[off:off + prg_sz])
        self.prg_size = prg_sz
        off += prg_sz
        if chr_sz > 0:
            if off + chr_sz > len(data):
                return False
            self.chr_rom = bytes(data[off:off + chr_sz])
        else:
            self.chr_rom = b""
        self.chr_size = chr_sz
        # Reset mapper live state.
        self.mmc1_shift = 0x10
        self.mmc1_ctrl = 0x0C
        self.mmc1_chr0 = 0
        self.mmc1_chr1 = 0
        self.mmc1_prg = 0
        self.uxrom_bank = 0
        self.cnrom_chr = 0
        self.mmc3_select = 0
        self.mmc3_prg_mode = 0
        self.mmc3_chr_mode = 0
        self.mmc3_irq_latch = 0
        self.mmc3_irq_counter = 0
        self.mmc3_irq_enable = False
        self.mmc3_irq_reload = False
        self.mmc3_irq_pending = False
        self.axrom_prg = 0
        self.mmc2_prg = 0
        self.mmc2_latch_fd = 0xFD
        self.mmc2_latch_fe = 0xFE
        cdef int j
        for j in range(8):
            self.mmc3_banks[j] = 0
        for j in range(4):
            self.mmc2_chr_banks[j] = 0
        # Try to load the .sav file if battery-backed.
        if self.has_battery:
            sav = path + ".sav"
            if os.path.exists(sav):
                try:
                    with open(sav, "rb") as sf:
                        d = sf.read(0x2000)
                    self.prg_ram[:len(d)] = d
                except OSError:
                    pass
        return True

    cpdef save_battery(self, str path):
        if not self.has_battery:
            return
        try:
            with open(path + ".sav", "wb") as f:
                f.write(bytes(self.prg_ram))
        except OSError:
            pass

    # ----- PRG ROM read --------------------------------------------------
    cpdef int read_prg(self, int addr):
        cdef int m = self.mapper
        cdef bytes rom = self.prg_rom
        cdef int sz = self.prg_size
        cdef int total8, last8, secondlast8, win, bank, base
        cdef int total32, total16
        if sz == 0:
            return 0
        if m == 0:
            if self.prg_banks_16k == 1:
                return rom[(addr - 0x8000) & 0x3FFF]
            return rom[(addr - 0x8000) & 0x7FFF]
        if m == 1:
            mode = (self.mmc1_ctrl >> 2) & 3
            bank = self.mmc1_prg & 0x0F
            if mode <= 1:
                bank &= 0x0E
                base = (bank * 0x4000) + ((addr - 0x8000) & 0x7FFF)
                return rom[base % sz]
            if mode == 2:
                if addr < 0xC000:
                    return rom[(addr - 0x8000) & 0x3FFF]
                base = bank * 0x4000 + ((addr - 0xC000) & 0x3FFF)
                return rom[base % sz]
            # mode 3
            if addr < 0xC000:
                base = bank * 0x4000 + ((addr - 0x8000) & 0x3FFF)
                return rom[base % sz]
            last = (self.prg_banks_16k - 1) * 0x4000 + ((addr - 0xC000) & 0x3FFF)
            return rom[last % sz]
        if m == 2:
            if addr < 0xC000:
                base = (self.uxrom_bank % max(1, self.prg_banks_16k)) * 0x4000 + ((addr - 0x8000) & 0x3FFF)
                return rom[base % sz]
            base = (self.prg_banks_16k - 1) * 0x4000 + ((addr - 0xC000) & 0x3FFF)
            return rom[base % sz]
        if m == 3:
            if self.prg_banks_16k == 1:
                return rom[(addr - 0x8000) & 0x3FFF]
            return rom[(addr - 0x8000) & 0x7FFF]
        if m == 4:
            total8 = max(1, sz // 0x2000)
            last8 = total8 - 1
            secondlast8 = total8 - 2 if total8 >= 2 else 0
            r6 = self.mmc3_banks[6] % total8
            r7 = self.mmc3_banks[7] % total8
            win = (addr - 0x8000) // 0x2000
            if self.mmc3_prg_mode == 0:
                bank = (r6, r7, secondlast8, last8)[win]
            else:
                bank = (secondlast8, r7, r6, last8)[win]
            base = bank * 0x2000 + (addr & 0x1FFF)
            return rom[base % sz]
        if m == 7:
            total32 = max(1, sz // 0x8000)
            bank = self.axrom_prg % total32
            base = bank * 0x8000 + (addr - 0x8000)
            return rom[base % sz]
        if m == 9:
            total8 = max(1, sz // 0x2000)
            last8 = total8 - 1
            if addr < 0xA000:
                bank = self.mmc2_prg % total8
            elif addr < 0xC000:
                bank = (last8 - 2) if last8 >= 2 else 0
            elif addr < 0xE000:
                bank = (last8 - 1) if last8 >= 1 else 0
            else:
                bank = last8
            base = bank * 0x2000 + (addr & 0x1FFF)
            return rom[base % sz]
        # Fallback
        return rom[(addr - 0x8000) % sz]

    # ----- mapper register write ----------------------------------------
    cpdef write_mapper(self, int addr, int value, Ppu ppu):
        cdef int m = self.mapper
        value &= 0xFF
        if addr < 0x8000:
            return
        if m == 1:
            if value & 0x80:
                self.mmc1_shift = 0x10
                self.mmc1_ctrl |= 0x0C
                return
            commit = self.mmc1_shift & 1
            self.mmc1_shift >>= 1
            self.mmc1_shift |= (value & 1) << 4
            if commit:
                reg = (addr >> 13) & 3
                data = self.mmc1_shift & 0x1F
                if reg == 0:
                    self.mmc1_ctrl = data
                    mm = data & 3
                    self.mirroring = (2, 3, 0, 1)[mm]
                    if ppu is not None:
                        ppu.set_mirroring(self.mirroring)
                elif reg == 1:
                    self.mmc1_chr0 = data
                elif reg == 2:
                    self.mmc1_chr1 = data
                else:
                    self.mmc1_prg = data
                self.mmc1_shift = 0x10
                self.apply_chr(ppu)
            return
        if m == 2:
            self.uxrom_bank = value & 0x0F
            return
        if m == 3:
            self.cnrom_chr = value & 0x03
            self.apply_chr(ppu)
            return
        if m == 4:
            if 0x8000 <= addr <= 0x9FFF:
                if (addr & 1) == 0:
                    self.mmc3_select = value & 0x07
                    self.mmc3_prg_mode = (value >> 6) & 1
                    self.mmc3_chr_mode = (value >> 7) & 1
                else:
                    self.mmc3_banks[self.mmc3_select] = value
                    self.apply_chr(ppu)
            elif 0xA000 <= addr <= 0xBFFF:
                if (addr & 1) == 0:
                    if self.mirroring != 4:
                        self.mirroring = 0 if (value & 1) == 0 else 1
                        ppu.set_mirroring(self.mirroring)
            elif 0xC000 <= addr <= 0xDFFF:
                if (addr & 1) == 0:
                    self.mmc3_irq_latch = value
                else:
                    self.mmc3_irq_counter = 0
                    self.mmc3_irq_reload = True
            else:  # $E000-$FFFF
                if (addr & 1) == 0:
                    self.mmc3_irq_enable = False
                    self.mmc3_irq_pending = False
                else:
                    self.mmc3_irq_enable = True
            return
        if m == 7:
            self.axrom_prg = value & 0x07
            ss = 1 if (value & 0x10) else 0
            self.mirroring = 3 if ss else 2
            ppu.set_mirroring(self.mirroring)
            return
        if m == 9:
            if 0xA000 <= addr <= 0xAFFF:
                self.mmc2_prg = value & 0x0F
            elif 0xB000 <= addr <= 0xBFFF:
                self.mmc2_chr_banks[0] = value & 0x1F
                self.apply_chr(ppu)
            elif 0xC000 <= addr <= 0xCFFF:
                self.mmc2_chr_banks[1] = value & 0x1F
                self.apply_chr(ppu)
            elif 0xD000 <= addr <= 0xDFFF:
                self.mmc2_chr_banks[2] = value & 0x1F
                self.apply_chr(ppu)
            elif 0xE000 <= addr <= 0xEFFF:
                self.mmc2_chr_banks[3] = value & 0x1F
                self.apply_chr(ppu)
            elif 0xF000 <= addr <= 0xFFFF:
                self.mirroring = 0 if (value & 1) == 0 else 1
                ppu.set_mirroring(self.mirroring)
            return

    # ----- CHR ROM/RAM resolution into the PPU's 8KB pattern table ------
    cpdef apply_chr(self, Ppu ppu):
        cdef bytearray dst = ppu.chr
        cdef int csz = self.chr_size
        cdef int i, b0, b1, total_1k, base, slot
        if csz == 0:
            # CHR RAM
            dst[:] = self.chr_ram
            return
        m = self.mapper
        rom = self.chr_rom
        if m == 1:
            chr_4k = (self.mmc1_ctrl & 0x10) != 0
            if chr_4k:
                b0 = (self.mmc1_chr0 % max(1, self.chr_banks_8k * 2)) * 0x1000
                b1 = (self.mmc1_chr1 % max(1, self.chr_banks_8k * 2)) * 0x1000
                dst[0:0x1000]      = rom[b0:b0 + 0x1000]
                dst[0x1000:0x2000] = rom[b1:b1 + 0x1000]
            else:
                bank = (self.mmc1_chr0 & 0x1E) % max(1, self.chr_banks_8k)
                base = bank * 0x2000
                dst[:] = rom[base:base + 0x2000]
            return
        if m == 3:
            bank = self.cnrom_chr % max(1, self.chr_banks_8k)
            base = bank * 0x2000
            dst[:] = rom[base:base + 0x2000]
            return
        if m == 4:
            total_1k = max(1, csz // 0x400)
            r = [self.mmc3_banks[i] % total_1k for i in range(8)]
            if self.mmc3_chr_mode == 0:
                layout = [r[0] & ~1, (r[0] & ~1) | 1, r[1] & ~1, (r[1] & ~1) | 1,
                          r[2], r[3], r[4], r[5]]
            else:
                layout = [r[2], r[3], r[4], r[5],
                          r[0] & ~1, (r[0] & ~1) | 1, r[1] & ~1, (r[1] & ~1) | 1]
            for slot in range(8):
                base = layout[slot] * 0x400
                dst[slot * 0x400:(slot + 1) * 0x400] = rom[base:base + 0x400]
            return
        if m == 7:
            dst[:] = self.chr_ram
            return
        if m == 9:
            # MMC2: $0000-$0FFF latched between banks 0 and 1, $1000-$1FFF
            # latched between banks 2 and 3. We pick by current latch state.
            total_4k = max(1, csz // 0x1000)
            sel0 = self.mmc2_chr_banks[0] if self.mmc2_latch_fd == 0xFD else self.mmc2_chr_banks[1]
            sel1 = self.mmc2_chr_banks[2] if self.mmc2_latch_fe == 0xFD else self.mmc2_chr_banks[3]
            b0 = (sel0 % total_4k) * 0x1000
            b1 = (sel1 % total_4k) * 0x1000
            dst[0:0x1000]      = rom[b0:b0 + 0x1000]
            dst[0x1000:0x2000] = rom[b1:b1 + 0x1000]
            return
        # Default: NROM-like, copy first 8KB.
        dst[:] = rom[:0x2000]


# ---------------------------------------------------------------------------
# Ppu — scanline-rendered with all the goodies.
# ---------------------------------------------------------------------------

cdef class Ppu:
    cdef public bytearray vram          # 2KB nametable
    cdef public bytearray vram_extra    # extra 2KB for 4-screen carts
    cdef public bytearray palette_ram   # 32 bytes
    cdef public bytearray oam           # 256 bytes
    cdef public bytearray chr           # 8KB pattern table window
    cdef public bytearray framebuffer   # 256*240 palette indices
    cdef public uint8_t ctrl, mask, status, oam_addr
    cdef public uint16_t v, t
    cdef public uint8_t x, w
    cdef public uint8_t ppu_data_buffer
    cdef public bint nmi_pending
    cdef public int scanline, dot, frame
    cdef public uint8_t mirroring
    cdef public list NES_RGB

    def __cinit__(self):
        self.vram = bytearray(0x800)
        self.vram_extra = bytearray(0x800)
        self.palette_ram = bytearray(32)
        self.oam = bytearray(256)
        self.chr = bytearray(0x2000)
        self.framebuffer = bytearray(256 * 240)
        self.ctrl = 0; self.mask = 0; self.status = 0; self.oam_addr = 0
        self.v = 0; self.t = 0; self.x = 0; self.w = 0
        self.ppu_data_buffer = 0
        self.nmi_pending = False
        self.scanline = 0; self.dot = 0; self.frame = 0
        self.mirroring = 0
        self.NES_RGB = NES_RGB

    cpdef set_mirroring(self, int m):
        self.mirroring = m & 0xFF

    # ---- nametable mirroring ----------------------------------------
    cdef inline (bint, int) _map_nt(self, int addr):
        cdef int idx = (addr - 0x2000) & 0x0FFF
        cdef int nt = (idx >> 10) & 3
        cdef int off = idx & 0x3FF
        cdef int mode = self.mirroring
        if mode == 4:
            if nt < 2:
                return (False, (nt * 0x400 + off) & 0x7FF)
            return (True, ((nt - 2) * 0x400 + off) & 0x7FF)
        if mode == 2:
            return (False, off & 0x3FF)
        if mode == 3:
            return (False, 0x400 + (off & 0x3FF))
        if mode == 1:
            return (False, ((nt >> 1) * 0x400 + off) & 0x7FF)
        return (False, (((nt & 1) * 0x400) + off) & 0x7FF)

    cdef inline int _nt_read(self, int addr):
        use_extra, off = self._map_nt(addr)
        if use_extra:
            return self.vram_extra[off]
        return self.vram[off]

    cdef inline void _nt_write(self, int addr, int value):
        use_extra, off = self._map_nt(addr)
        if use_extra:
            self.vram_extra[off] = value & 0xFF
        else:
            self.vram[off] = value & 0xFF

    cdef inline int _palette_addr(self, int addr):
        cdef int p = (addr - 0x3F00) & 0x1F
        if p == 0x10 or p == 0x14 or p == 0x18 or p == 0x1C:
            p -= 0x10
        return p

    cdef inline void _inc_vram(self):
        self.v = (self.v + (32 if (self.ctrl & 0x04) else 1)) & 0x7FFF

    # ---- $2007 PPUDATA -----------------------------------------------
    cpdef int read_ppudata(self):
        cdef int addr = self.v & 0x3FFF
        self._inc_vram()
        cdef int value
        if addr < 0x2000:
            value = self.ppu_data_buffer
            self.ppu_data_buffer = self.chr[addr]
            return value
        if addr < 0x3F00:
            value = self.ppu_data_buffer
            self.ppu_data_buffer = self._nt_read(addr)
            return value
        value = self.palette_ram[self._palette_addr(addr)]
        self.ppu_data_buffer = self._nt_read(addr - 0x1000)
        return value

    cpdef write_ppudata(self, int value):
        cdef int addr = self.v & 0x3FFF
        self._inc_vram()
        if addr < 0x2000:
            self.chr[addr] = value & 0xFF
        elif addr < 0x3F00:
            self._nt_write(addr, value)
        else:
            self.palette_ram[self._palette_addr(addr)] = value & 0x3F

    cpdef int read_register(self, int reg):
        reg &= 7
        cdef int v
        if reg == 2:
            v = self.status
            self.status &= 0x7F
            self.w = 0
            return v
        if reg == 4:
            return self.oam[self.oam_addr]
        if reg == 7:
            return self.read_ppudata()
        return 0

    cpdef write_register(self, int reg, int value):
        reg &= 7
        value &= 0xFF
        if reg == 0:
            self.ctrl = value
            self.t = (self.t & 0x73FF) | ((value & 3) << 10)
        elif reg == 1:
            self.mask = value
        elif reg == 3:
            self.oam_addr = value
        elif reg == 4:
            self.oam[self.oam_addr] = value
            self.oam_addr = (self.oam_addr + 1) & 0xFF
        elif reg == 5:
            if self.w == 0:
                self.t = (self.t & 0x7FE0) | (value >> 3)
                self.x = value & 7
                self.w = 1
            else:
                self.t = (self.t & 0x0C1F) | ((value & 7) << 12) | ((value & 0xF8) << 2)
                self.w = 0
        elif reg == 6:
            if self.w == 0:
                self.t = (self.t & 0x00FF) | ((value & 0x3F) << 8)
                self.w = 1
            else:
                self.t = (self.t & 0x7F00) | value
                self.v = self.t
                self.w = 0
        elif reg == 7:
            self.write_ppudata(value)

    cpdef oam_dma(self, page_data):
        cdef int i
        for i in range(256):
            self.oam[(self.oam_addr + i) & 0xFF] = page_data[i] & 0xFF
        self.oam_addr = (self.oam_addr + 256) & 0xFF

    # ---- Per-pixel rendering helpers --------------------------------
    cdef inline (int, int) _bg_pixel(self, int x, int y):
        cdef int show_bg = self.mask & 0x08
        cdef int show_left = self.mask & 0x02
        if (not show_bg) or (x < 8 and not show_left):
            return (0, self.palette_ram[0] & 0x3F)
        cdef int base_nt = self.ctrl & 3
        cdef int base_x = (base_nt & 1) * 256
        cdef int base_y = ((base_nt >> 1) & 1) * 240
        cdef int scx = ((self.v & 0x1F) << 3) | self.x
        cdef int scy = (((self.v >> 5) & 0x1F) << 3) | ((self.v >> 12) & 7)
        cdef int wx = (base_x + scx + x) % 512
        cdef int wy = (base_y + scy + y) % 480
        cdef int nt_x = wx // 256
        cdef int nt_y = wy // 240
        cdef int nt = (nt_y << 1) | nt_x
        cdef int lx = wx - nt_x * 256
        cdef int ly = wy - nt_y * 240
        cdef int tile_x = lx >> 3
        cdef int tile_y = ly >> 3
        cdef int nt_base = 0x2000 + nt * 0x400
        cdef int tile = self._nt_read(nt_base + tile_y * 32 + tile_x)
        cdef int attr = self._nt_read(nt_base + 0x3C0 + (tile_y >> 2) * 8 + (tile_x >> 2))
        cdef int quad = ((tile_y & 2) << 1) | (tile_x & 2)
        cdef int pal_hi = (attr >> quad) & 3
        cdef int pat = 0x1000 if (self.ctrl & 0x10) else 0
        cdef int row = ly & 7
        cdef int col = lx & 7
        cdef int low = self.chr[(pat + tile * 16 + row) & 0x1FFF]
        cdef int high = self.chr[(pat + tile * 16 + row + 8) & 0x1FFF]
        cdef int bit = 7 - col
        cdef int pix = ((low >> bit) & 1) | (((high >> bit) & 1) << 1)
        if pix == 0:
            return (0, self.palette_ram[0] & 0x3F)
        return (pix, self.palette_ram[(pal_hi << 2) + pix] & 0x3F)

    cdef inline int _spr_pixel(self, int x, int y, bint bg_nz):
        cdef int show_spr = self.mask & 0x10
        cdef int show_left = self.mask & 0x04
        if (not show_spr) or (x < 8 and not show_left):
            return -1
        cdef int sprite_h = 16 if (self.ctrl & 0x20) else 8
        cdef int i, o, sy, tile, attr, sx, row, col, base, low, high, bit, pix, pal
        cdef int bank, tile_index
        for i in range(64):
            o = i * 4
            sy = self.oam[o] + 1
            tile = self.oam[o + 1]
            attr = self.oam[o + 2]
            sx = self.oam[o + 3]
            if y < sy or y >= sy + sprite_h or x < sx or x >= sx + 8:
                continue
            row = y - sy
            col = x - sx
            if attr & 0x80:
                row = (sprite_h - 1) - row
            if attr & 0x40:
                col = 7 - col
            if sprite_h == 16:
                bank = (tile & 1) * 0x1000
                tile_index = tile & 0xFE
                if row >= 8:
                    tile_index += 1
                    row -= 8
                base = bank + tile_index * 16
            else:
                base = (0x1000 if (self.ctrl & 0x08) else 0) + tile * 16
            low = self.chr[(base + row) & 0x1FFF]
            high = self.chr[(base + row + 8) & 0x1FFF]
            bit = 7 - col
            pix = ((low >> bit) & 1) | (((high >> bit) & 1) << 1)
            if pix == 0:
                continue
            if i == 0 and bg_nz and x < 255:
                self.status |= 0x40
            if (attr & 0x20) and bg_nz:
                return -1
            pal = 0x10 + ((attr & 3) << 2) + pix
            return self.palette_ram[self._palette_addr(0x3F00 + pal)] & 0x3F
        return -1

    cpdef render_frame(self):
        # Clear sprite-0 hit + sprite-overflow flags at frame start.
        self.status &= ~0x60
        cdef int sprite_h = 16 if (self.ctrl & 0x20) else 8
        cdef int i, o, sy, ly
        cdef int counts[240]
        for i in range(240):
            counts[i] = 0
        for i in range(64):
            o = i * 4
            sy = self.oam[o] + 1
            if sy >= 240:
                continue
            for ly in range(sy, min(240, sy + sprite_h)):
                if 0 <= ly < 240:
                    counts[ly] += 1
        for i in range(240):
            if counts[i] > 8:
                self.status |= 0x20
                break
        cdef int x, y, bg_pix, bg_col, spr
        cdef int row_base
        cdef bytearray fb = self.framebuffer
        for y in range(240):
            row_base = y * 256
            for x in range(256):
                bg_pix, bg_col = self._bg_pixel(x, y)
                spr = self._spr_pixel(x, y, bg_pix != 0)
                fb[row_base + x] = (spr if spr >= 0 else bg_col) & 0x3F

    cpdef step(self, int ppu_cycles):
        cdef int i
        for i in range(ppu_cycles):
            self.dot += 1
            if self.dot >= 341:
                self.dot = 0
                self.scanline += 1
                if self.scanline == 241:
                    self.status |= 0x80
                    if self.ctrl & 0x80:
                        self.nmi_pending = True
                elif self.scanline >= 262:
                    self.scanline = 0
                    self.frame += 1
                    self.status &= 0x1F


# ---------------------------------------------------------------------------
# Apu — full 5-channel state machine + Blip-style mixer.
# ---------------------------------------------------------------------------

cdef class Apu:
    # Pulse 1 / Pulse 2
    cdef public bint p_enabled[2]
    cdef public uint8_t p_duty[2], p_volume[2]
    cdef public bint p_halt[2], p_const[2], p_env_start[2]
    cdef public uint8_t p_env_div[2], p_env_decay[2]
    cdef public bint p_sw_enable[2], p_sw_negate[2], p_sw_reload[2]
    cdef public uint8_t p_sw_period[2], p_sw_shift[2], p_sw_div[2]
    cdef public uint16_t p_period[2]
    cdef public uint16_t p_timer[2]
    cdef public uint8_t p_seq[2]
    cdef public uint8_t p_length[2]
    # Triangle
    cdef public bint t_enabled, t_halt, t_lin_reload
    cdef public uint8_t t_lin_value, t_lin_counter
    cdef public uint16_t t_period, t_timer
    cdef public uint8_t t_seq, t_length
    # Noise
    cdef public bint n_enabled, n_halt, n_const, n_env_start, n_mode
    cdef public uint8_t n_volume, n_env_div, n_env_decay
    cdef public uint16_t n_period
    cdef public int32_t n_timer
    cdef public uint16_t n_shift
    cdef public uint8_t n_length
    # DMC
    cdef public bint dmc_enabled, dmc_irq_en, dmc_loop, dmc_irq
    cdef public uint16_t dmc_rate, dmc_sample_addr, dmc_sample_len
    cdef public uint16_t dmc_current_addr, dmc_bytes_remaining
    cdef public int32_t dmc_timer
    cdef public uint8_t dmc_output, dmc_shift, dmc_bits_remaining
    # Frame counter
    cdef public bint frame_5, frame_inhibit, frame_irq
    cdef public int32_t frame_cycle
    # Audio output buffer for the current frame.
    cdef public object audio_buf            # bytearray of int16 LE samples
    cdef public int audio_pos
    cdef public double audio_carry          # cycles-per-sample fractional carry
    cdef public int activity_count
    # Hook so DMC can read CPU memory.
    cdef public object cpu_read_fn

    def __cinit__(self):
        cdef int i
        for i in range(2):
            self.p_enabled[i] = False
            self.p_duty[i] = 0; self.p_volume[i] = 0
            self.p_halt[i] = False; self.p_const[i] = False; self.p_env_start[i] = False
            self.p_env_div[i] = 0; self.p_env_decay[i] = 0
            self.p_sw_enable[i] = False; self.p_sw_negate[i] = False; self.p_sw_reload[i] = False
            self.p_sw_period[i] = 0; self.p_sw_shift[i] = 0; self.p_sw_div[i] = 0
            self.p_period[i] = 0; self.p_timer[i] = 0; self.p_seq[i] = 0; self.p_length[i] = 0
        self.t_enabled = False; self.t_halt = False; self.t_lin_reload = False
        self.t_lin_value = 0; self.t_lin_counter = 0
        self.t_period = 0; self.t_timer = 0; self.t_seq = 0; self.t_length = 0
        self.n_enabled = False; self.n_halt = False; self.n_const = False
        self.n_env_start = False; self.n_mode = False
        self.n_volume = 0; self.n_env_div = 0; self.n_env_decay = 0
        self.n_period = NOISE_PERIODS[0]; self.n_timer = 0
        self.n_shift = 1; self.n_length = 0
        self.dmc_enabled = False; self.dmc_irq_en = False; self.dmc_loop = False; self.dmc_irq = False
        self.dmc_rate = DMC_RATES[0]; self.dmc_sample_addr = 0xC000; self.dmc_sample_len = 0
        self.dmc_current_addr = 0xC000; self.dmc_bytes_remaining = 0
        self.dmc_timer = 0; self.dmc_output = 0; self.dmc_shift = 0; self.dmc_bits_remaining = 0
        self.frame_5 = False; self.frame_inhibit = False; self.frame_irq = False
        self.frame_cycle = 0
        self.audio_buf = bytearray(AUDIO_PER_FRAME * 2 * 4)  # extra headroom
        self.audio_pos = 0
        self.audio_carry = 0.0
        self.activity_count = 0
        self.cpu_read_fn = None

    cpdef write_register(self, int reg, int value):
        value &= 0xFF
        cdef int ch
        if reg == 0x4000 or reg == 0x4004:
            ch = 0 if reg == 0x4000 else 1
            self.p_duty[ch] = (value >> 6) & 3
            self.p_halt[ch] = (value & 0x20) != 0
            self.p_const[ch] = (value & 0x10) != 0
            self.p_volume[ch] = value & 0x0F
        elif reg == 0x4001 or reg == 0x4005:
            ch = 0 if reg == 0x4001 else 1
            self.p_sw_enable[ch] = (value & 0x80) != 0
            self.p_sw_period[ch] = (value >> 4) & 7
            self.p_sw_negate[ch] = (value & 0x08) != 0
            self.p_sw_shift[ch] = value & 7
            self.p_sw_reload[ch] = True
        elif reg == 0x4002 or reg == 0x4006:
            ch = 0 if reg == 0x4002 else 1
            self.p_period[ch] = (self.p_period[ch] & 0x700) | value
        elif reg == 0x4003 or reg == 0x4007:
            ch = 0 if reg == 0x4003 else 1
            self.p_period[ch] = (self.p_period[ch] & 0xFF) | ((value & 7) << 8)
            if self.p_enabled[ch]:
                self.p_length[ch] = LENGTH_TABLE[(value >> 3) & 0x1F]
            self.p_seq[ch] = 0
            self.p_env_start[ch] = True
        elif reg == 0x4008:
            self.t_halt = (value & 0x80) != 0
            self.t_lin_value = value & 0x7F
        elif reg == 0x400A:
            self.t_period = (self.t_period & 0x700) | value
        elif reg == 0x400B:
            self.t_period = (self.t_period & 0xFF) | ((value & 7) << 8)
            if self.t_enabled:
                self.t_length = LENGTH_TABLE[(value >> 3) & 0x1F]
            self.t_lin_reload = True
        elif reg == 0x400C:
            self.n_halt = (value & 0x20) != 0
            self.n_const = (value & 0x10) != 0
            self.n_volume = value & 0x0F
        elif reg == 0x400E:
            self.n_mode = (value & 0x80) != 0
            self.n_period = NOISE_PERIODS[value & 0x0F]
        elif reg == 0x400F:
            if self.n_enabled:
                self.n_length = LENGTH_TABLE[(value >> 3) & 0x1F]
            self.n_env_start = True
        elif reg == 0x4010:
            self.dmc_irq_en = (value & 0x80) != 0
            self.dmc_loop = (value & 0x40) != 0
            self.dmc_rate = DMC_RATES[value & 0x0F]
            if not self.dmc_irq_en:
                self.dmc_irq = False
        elif reg == 0x4011:
            self.dmc_output = value & 0x7F
        elif reg == 0x4012:
            self.dmc_sample_addr = 0xC000 | ((value & 0xFF) << 6)
        elif reg == 0x4013:
            self.dmc_sample_len = ((value & 0xFF) << 4) | 1
        elif reg == 0x4015:
            self.p_enabled[0] = (value & 0x01) != 0
            if not self.p_enabled[0]: self.p_length[0] = 0
            self.p_enabled[1] = (value & 0x02) != 0
            if not self.p_enabled[1]: self.p_length[1] = 0
            self.t_enabled = (value & 0x04) != 0
            if not self.t_enabled: self.t_length = 0
            self.n_enabled = (value & 0x08) != 0
            if not self.n_enabled: self.n_length = 0
            if value & 0x10:
                if self.dmc_bytes_remaining == 0:
                    self.dmc_current_addr = self.dmc_sample_addr
                    self.dmc_bytes_remaining = self.dmc_sample_len
                self.dmc_enabled = True
            else:
                self.dmc_enabled = False
                self.dmc_bytes_remaining = 0
            self.dmc_irq = False
        elif reg == 0x4017:
            self.frame_5 = (value & 0x80) != 0
            self.frame_inhibit = (value & 0x40) != 0
            if self.frame_inhibit:
                self.frame_irq = False
            self.frame_cycle = 0
            if self.frame_5:
                self._quarter()
                self._half()
        self.activity_count = (self.activity_count + 1) & 0xFFFF

    cpdef int read_status(self):
        cdef int v = 0
        if self.p_length[0] > 0: v |= 0x01
        if self.p_length[1] > 0: v |= 0x02
        if self.t_length > 0:    v |= 0x04
        if self.n_length > 0:    v |= 0x08
        if self.dmc_bytes_remaining > 0: v |= 0x10
        if self.frame_irq: v |= 0x40
        if self.dmc_irq:   v |= 0x80
        self.frame_irq = False
        return v

    cdef inline void _env(self, int ch):
        if self.p_env_start[ch]:
            self.p_env_start[ch] = False
            self.p_env_decay[ch] = 15
            self.p_env_div[ch] = self.p_volume[ch]
        else:
            if self.p_env_div[ch] == 0:
                self.p_env_div[ch] = self.p_volume[ch]
                if self.p_env_decay[ch] > 0:
                    self.p_env_decay[ch] -= 1
                elif self.p_halt[ch]:
                    self.p_env_decay[ch] = 15
            else:
                self.p_env_div[ch] -= 1

    cdef inline void _n_env(self):
        if self.n_env_start:
            self.n_env_start = False
            self.n_env_decay = 15
            self.n_env_div = self.n_volume
        else:
            if self.n_env_div == 0:
                self.n_env_div = self.n_volume
                if self.n_env_decay > 0:
                    self.n_env_decay -= 1
                elif self.n_halt:
                    self.n_env_decay = 15
            else:
                self.n_env_div -= 1

    cdef inline void _sweep(self, int ch):
        cdef int change = self.p_period[ch] >> self.p_sw_shift[ch]
        cdef int target = self.p_period[ch] + (-change if self.p_sw_negate[ch] else change)
        if ch == 0 and self.p_sw_negate[ch]:
            target -= 1
        cdef bint mute = (self.p_period[ch] < 8) or (target > 0x7FF)
        if (self.p_sw_div[ch] == 0 and self.p_sw_enable[ch]
                and self.p_sw_shift[ch] != 0 and not mute):
            self.p_period[ch] = target & 0x7FF
        if self.p_sw_div[ch] == 0 or self.p_sw_reload[ch]:
            self.p_sw_div[ch] = self.p_sw_period[ch]
            self.p_sw_reload[ch] = False
        else:
            self.p_sw_div[ch] -= 1

    cdef inline void _length(self):
        cdef int ch
        for ch in range(2):
            if not self.p_halt[ch] and self.p_length[ch] > 0:
                self.p_length[ch] -= 1
        if not self.t_halt and self.t_length > 0:
            self.t_length -= 1
        if not self.n_halt and self.n_length > 0:
            self.n_length -= 1

    cdef inline void _linear(self):
        if self.t_lin_reload:
            self.t_lin_counter = self.t_lin_value
        elif self.t_lin_counter > 0:
            self.t_lin_counter -= 1
        if not self.t_halt:
            self.t_lin_reload = False

    cdef inline void _quarter(self):
        self._env(0); self._env(1); self._n_env(); self._linear()

    cdef inline void _half(self):
        self._quarter(); self._length(); self._sweep(0); self._sweep(1)

    cpdef step(self, int cpu_cycles):
        # Frame counter sequencer (NTSC step boundaries, in CPU cycles).
        cdef int i, ch, advance
        cdef int s0 = 3729, s1 = 7457, s2 = 11186, s3 = 14916, s4 = 18641
        for i in range(cpu_cycles):
            self.frame_cycle += 1
            if not self.frame_5:
                if self.frame_cycle == s0: self._quarter()
                elif self.frame_cycle == s1: self._half()
                elif self.frame_cycle == s2: self._quarter()
                elif self.frame_cycle >= s3:
                    self._half()
                    if not self.frame_inhibit:
                        self.frame_irq = True
                    self.frame_cycle = 0
            else:
                if self.frame_cycle == s0: self._quarter()
                elif self.frame_cycle == s1: self._half()
                elif self.frame_cycle == s2: self._quarter()
                elif self.frame_cycle >= s4:
                    self._half()
                    self.frame_cycle = 0
        # Approximate channel timer advance per frame chunk (fast path).
        for ch in range(2):
            if self.p_period[ch] >= 8:
                advance = max(1, cpu_cycles // (self.p_period[ch] + 1))
                self.p_seq[ch] = (self.p_seq[ch] + advance) & 7
        if self.t_period >= 2 and self.t_lin_counter > 0 and self.t_length > 0:
            advance = max(1, cpu_cycles // (self.t_period + 1))
            self.t_seq = (self.t_seq + advance) & 0x1F
        # Noise LFSR
        self.n_timer -= cpu_cycles
        cdef int fb
        while self.n_timer <= 0:
            self.n_timer += max(1, <int>self.n_period)
            fb = (self.n_shift & 1) ^ (((self.n_shift >> (6 if self.n_mode else 1)) & 1))
            self.n_shift = ((self.n_shift >> 1) | (fb << 14)) & 0x7FFF
        # DMC
        cdef int sample
        if self.dmc_enabled and self.dmc_bytes_remaining > 0 and self.cpu_read_fn is not None:
            self.dmc_timer -= cpu_cycles
            while self.dmc_timer <= 0 and self.dmc_bytes_remaining > 0:
                self.dmc_timer += max(1, <int>self.dmc_rate)
                sample = self.cpu_read_fn(self.dmc_current_addr) & 0xFF
                self.dmc_current_addr = ((self.dmc_current_addr + 1) & 0xFFFF) | 0x8000
                self.dmc_bytes_remaining -= 1
                if sample & 1:
                    self.dmc_output = min(127, self.dmc_output + 2)
                else:
                    self.dmc_output = max(0, self.dmc_output - 2)
                if self.dmc_bytes_remaining == 0:
                    if self.dmc_loop:
                        self.dmc_current_addr = self.dmc_sample_addr
                        self.dmc_bytes_remaining = self.dmc_sample_len
                    elif self.dmc_irq_en:
                        self.dmc_irq = True
        # Audio synthesis: produce samples proportional to cycles elapsed.
        cdef double cycles_per_sample = 1789773.0 / AUDIO_SR
        cdef double pending = self.audio_carry + cpu_cycles
        cdef int n_samples = <int>(pending / cycles_per_sample)
        self.audio_carry = pending - n_samples * cycles_per_sample
        cdef int p1, p2, tri, no, dmc, mix
        cdef int16_t s16
        cdef int j
        # Compute current channel outputs (~constant for this chunk).
        if (self.p_length[0] == 0 or self.p_period[0] < 8 or not self.p_enabled[0]):
            p1 = 0
        else:
            p1 = (self.p_volume[0] if self.p_const[0] else self.p_env_decay[0]) * DUTY_TABLE[self.p_duty[0]][self.p_seq[0]]
        if (self.p_length[1] == 0 or self.p_period[1] < 8 or not self.p_enabled[1]):
            p2 = 0
        else:
            p2 = (self.p_volume[1] if self.p_const[1] else self.p_env_decay[1]) * DUTY_TABLE[self.p_duty[1]][self.p_seq[1]]
        tri = TRIANGLE_SEQ[self.t_seq] if (self.t_enabled and self.t_length > 0
                                            and self.t_lin_counter > 0 and self.t_period >= 2) else 0
        if self.n_enabled and self.n_length > 0 and (self.n_shift & 1) == 0:
            no = self.n_volume if self.n_const else self.n_env_decay
        else:
            no = 0
        dmc = self.dmc_output
        # Linear mixer (rough approximation of NES nonlinear mix; loud enough).
        mix = (p1 + p2) * 250 + tri * 200 + no * 180 + dmc * 60
        if mix > 32767: mix = 32767
        if mix < -32768: mix = -32768
        s16 = <int16_t>mix
        for j in range(n_samples):
            if self.audio_pos + 2 > len(self.audio_buf):
                break
            self.audio_buf[self.audio_pos]     = s16 & 0xFF
            self.audio_buf[self.audio_pos + 1] = (s16 >> 8) & 0xFF
            self.audio_pos += 2

    cpdef bytes consume_audio(self):
        cdef bytes out = bytes(self.audio_buf[:self.audio_pos])
        self.audio_pos = 0
        return out

    cpdef int consume_activity(self):
        cdef int v = self.activity_count
        self.activity_count = 0
        return v

    @property
    def irq_pending(self):
        return self.frame_irq or self.dmc_irq


# ---------------------------------------------------------------------------
# Cpu — full 6502 with all official + common unofficial opcodes.
# ---------------------------------------------------------------------------

cdef class Cpu:
    cdef public bytearray ram             # 2KB internal RAM
    cdef public uint16_t pc
    cdef public uint8_t a, x, y, sp, status
    cdef public int64_t cycles
    cdef public int64_t executed_instr
    cdef public Ppu ppu
    cdef public Apu apu
    cdef public Cart cart
    cdef public uint8_t controller1_state, controller1_shift
    cdef public uint8_t controller2_state, controller2_shift
    cdef public uint8_t controller_strobe
    cdef public str last_instr

    def __cinit__(self):
        self.ram = bytearray(0x800)
        self.pc = 0; self.a = 0; self.x = 0; self.y = 0; self.sp = 0xFD
        self.status = 0x24
        self.cycles = 0; self.executed_instr = 0
        self.controller1_state = 0; self.controller1_shift = 0
        self.controller2_state = 0; self.controller2_shift = 0
        self.controller_strobe = 0
        self.last_instr = "RESET"

    cdef inline int _read(self, int addr):
        addr &= 0xFFFF
        if addr < 0x2000:
            return self.ram[addr & 0x7FF]
        if 0x2000 <= addr < 0x4000:
            return self.ppu.read_register(addr & 7)
        if addr == 0x4015:
            return self.apu.read_status()
        if addr == 0x4016:
            v = self.controller1_shift & 1
            if not self.controller_strobe:
                self.controller1_shift = ((self.controller1_shift >> 1) | 0x80) & 0xFF
            return v
        if addr == 0x4017:
            v = self.controller2_shift & 1
            if not self.controller_strobe:
                self.controller2_shift = ((self.controller2_shift >> 1) | 0x80) & 0xFF
            return v
        if 0x4000 <= addr < 0x4020:
            return 0
        if 0x6000 <= addr < 0x8000:
            return self.cart.prg_ram[addr - 0x6000]
        if 0x8000 <= addr <= 0xFFFF:
            return self.cart.read_prg(addr)
        return 0

    cdef inline void _write(self, int addr, int value):
        addr &= 0xFFFF
        value &= 0xFF
        if addr < 0x2000:
            self.ram[addr & 0x7FF] = value
            return
        if 0x2000 <= addr < 0x4000:
            self.ppu.write_register(addr & 7, value)
            return
        if addr == 0x4014:
            page = value << 8
            page_data = bytes([self._read((page + i) & 0xFFFF) for i in range(256)])
            self.ppu.oam_dma(page_data)
            self.cycles += 513 + (self.cycles & 1)
            return
        if addr == 0x4016:
            self.controller_strobe = value & 1
            if self.controller_strobe:
                self.controller1_shift = self.controller1_state
                self.controller2_shift = self.controller2_state
            return
        if addr == 0x4017:
            self.apu.write_register(0x4017, value)
            return
        if addr == 0x4015:
            self.apu.write_register(0x4015, value)
            return
        if 0x4000 <= addr <= 0x4013:
            self.apu.write_register(addr, value)
            return
        if 0x6000 <= addr < 0x8000:
            self.cart.prg_ram[addr - 0x6000] = value
            return
        if addr >= 0x8000:
            self.cart.write_mapper(addr, value, self.ppu)
            return

    cdef inline int _read16(self, int addr):
        return self._read(addr) | (self._read((addr + 1) & 0xFFFF) << 8)

    cdef inline int _read16_bug(self, int addr):
        return self._read(addr) | (self._read((addr & 0xFF00) | ((addr + 1) & 0xFF)) << 8)

    cdef inline void _push(self, int v):
        self._write(0x100 + self.sp, v)
        self.sp = (self.sp - 1) & 0xFF

    cdef inline int _pull(self):
        self.sp = (self.sp + 1) & 0xFF
        return self._read(0x100 + self.sp)

    cdef inline void _set_zn(self, int v):
        if (v & 0xFF) == 0:
            self.status |= FLAG_Z
        else:
            self.status &= ~FLAG_Z & 0xFF
        if v & 0x80:
            self.status |= FLAG_N
        else:
            self.status &= ~FLAG_N & 0xFF

    cdef inline void _adc(self, int v):
        cdef int carry = 1 if (self.status & FLAG_C) else 0
        cdef int r = self.a + v + carry
        if r > 0xFF: self.status |= FLAG_C
        else: self.status &= ~FLAG_C & 0xFF
        if (~(self.a ^ v) & (self.a ^ r) & 0x80): self.status |= FLAG_V
        else: self.status &= ~FLAG_V & 0xFF
        self.a = r & 0xFF
        self._set_zn(self.a)

    cdef inline void _sbc(self, int v):
        self._adc(v ^ 0xFF)

    cdef inline void _cmp(self, int reg, int v):
        cdef int t = (reg - v) & 0x1FF
        if reg >= v: self.status |= FLAG_C
        else: self.status &= ~FLAG_C & 0xFF
        self._set_zn(t & 0xFF)

    # --- addressing modes -----------------------------------------------
    cdef inline int _imm(self):
        cdef int a = self.pc
        self.pc = (self.pc + 1) & 0xFFFF
        return a

    cdef inline int _zp(self):
        cdef int v = self._read(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    cdef inline int _zpx(self):
        cdef int v = (self._read(self.pc) + self.x) & 0xFF
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    cdef inline int _zpy(self):
        cdef int v = (self._read(self.pc) + self.y) & 0xFF
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    cdef inline int _abs(self):
        cdef int v = self._read16(self.pc)
        self.pc = (self.pc + 2) & 0xFFFF
        return v

    cdef inline int _abx(self, bint extra):
        cdef int b = self._read16(self.pc)
        cdef int a
        self.pc = (self.pc + 2) & 0xFFFF
        a = (b + self.x) & 0xFFFF
        if extra and (b & 0xFF00) != (a & 0xFF00):
            self.cycles += 1
        return a

    cdef inline int _aby(self, bint extra):
        cdef int b = self._read16(self.pc)
        cdef int a
        self.pc = (self.pc + 2) & 0xFFFF
        a = (b + self.y) & 0xFFFF
        if extra and (b & 0xFF00) != (a & 0xFF00):
            self.cycles += 1
        return a

    cdef inline int _ind(self):
        cdef int p = self._read16(self.pc)
        self.pc = (self.pc + 2) & 0xFFFF
        return self._read16_bug(p)

    cdef inline int _izx(self):
        cdef int z = (self._read(self.pc) + self.x) & 0xFF
        self.pc = (self.pc + 1) & 0xFFFF
        return self._read(z) | (self._read((z + 1) & 0xFF) << 8)

    cdef inline int _izy(self, bint extra):
        cdef int z = self._read(self.pc)
        cdef int b
        cdef int a
        self.pc = (self.pc + 1) & 0xFFFF
        b = self._read(z) | (self._read((z + 1) & 0xFF) << 8)
        a = (b + self.y) & 0xFFFF
        if extra and (b & 0xFF00) != (a & 0xFF00):
            self.cycles += 1
        return a

    cdef inline void _branch(self, bint cond):
        cdef int o = self._read(self.pc)
        cdef int old
        self.pc = (self.pc + 1) & 0xFFFF
        if cond:
            self.cycles += 1
            old = self.pc
            if o & 0x80:
                o -= 0x100
            self.pc = (self.pc + o) & 0xFFFF
            if (old & 0xFF00) != (self.pc & 0xFF00):
                self.cycles += 1

    cpdef _service_nmi(self):
        self._push((self.pc >> 8) & 0xFF)
        self._push(self.pc & 0xFF)
        self._push((self.status & ~FLAG_B) | FLAG_U)
        self.status |= FLAG_I
        self.pc = self._read16(0xFFFA)
        self.cycles += 7
        self.ppu.step(21)

    cpdef _service_irq(self):
        self._push((self.pc >> 8) & 0xFF)
        self._push(self.pc & 0xFF)
        self._push((self.status & ~FLAG_B) | FLAG_U)
        self.status |= FLAG_I
        self.pc = self._read16(0xFFFE)
        self.cycles += 7
        self.ppu.step(21)
        self.apu.step(7)

    cpdef step_instruction(self):
        # Pre-instruction interrupt service.
        if self.ppu.nmi_pending:
            self.ppu.nmi_pending = False
            self._service_nmi()
        elif (not (self.status & FLAG_I)) and (self.apu.irq_pending or self.cart.mmc3_irq_pending):
            self._service_irq()
        cdef int op_addr = self.pc
        cdef int op = self._read(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        cdef int cyc = 2
        cdef int a, v, ci, target, change, mute_chk
        # Massive opcode dispatch — sorted by mnemonic family for readability.
        if   op == 0xEA: cyc = 2  # NOP
        elif op == 0xA9: self.a = self._read(self._imm()); self._set_zn(self.a); cyc = 2
        elif op == 0xA5: self.a = self._read(self._zp());  self._set_zn(self.a); cyc = 3
        elif op == 0xB5: self.a = self._read(self._zpx()); self._set_zn(self.a); cyc = 4
        elif op == 0xAD: self.a = self._read(self._abs()); self._set_zn(self.a); cyc = 4
        elif op == 0xBD: self.a = self._read(self._abx(True)); self._set_zn(self.a); cyc = 4
        elif op == 0xB9: self.a = self._read(self._aby(True)); self._set_zn(self.a); cyc = 4
        elif op == 0xA1: self.a = self._read(self._izx()); self._set_zn(self.a); cyc = 6
        elif op == 0xB1: self.a = self._read(self._izy(True)); self._set_zn(self.a); cyc = 5
        elif op == 0xA2: self.x = self._read(self._imm()); self._set_zn(self.x); cyc = 2
        elif op == 0xA6: self.x = self._read(self._zp());  self._set_zn(self.x); cyc = 3
        elif op == 0xB6: self.x = self._read(self._zpy()); self._set_zn(self.x); cyc = 4
        elif op == 0xAE: self.x = self._read(self._abs()); self._set_zn(self.x); cyc = 4
        elif op == 0xBE: self.x = self._read(self._aby(True)); self._set_zn(self.x); cyc = 4
        elif op == 0xA0: self.y = self._read(self._imm()); self._set_zn(self.y); cyc = 2
        elif op == 0xA4: self.y = self._read(self._zp());  self._set_zn(self.y); cyc = 3
        elif op == 0xB4: self.y = self._read(self._zpx()); self._set_zn(self.y); cyc = 4
        elif op == 0xAC: self.y = self._read(self._abs()); self._set_zn(self.y); cyc = 4
        elif op == 0xBC: self.y = self._read(self._abx(True)); self._set_zn(self.y); cyc = 4
        elif op == 0x85: self._write(self._zp(),  self.a); cyc = 3
        elif op == 0x95: self._write(self._zpx(), self.a); cyc = 4
        elif op == 0x8D: self._write(self._abs(), self.a); cyc = 4
        elif op == 0x9D: self._write(self._abx(False), self.a); cyc = 5
        elif op == 0x99: self._write(self._aby(False), self.a); cyc = 5
        elif op == 0x81: self._write(self._izx(), self.a); cyc = 6
        elif op == 0x91: self._write(self._izy(False), self.a); cyc = 6
        elif op == 0x86: self._write(self._zp(),  self.x); cyc = 3
        elif op == 0x96: self._write(self._zpy(), self.x); cyc = 4
        elif op == 0x8E: self._write(self._abs(), self.x); cyc = 4
        elif op == 0x84: self._write(self._zp(),  self.y); cyc = 3
        elif op == 0x94: self._write(self._zpx(), self.y); cyc = 4
        elif op == 0x8C: self._write(self._abs(), self.y); cyc = 4
        elif op == 0xAA: self.x = self.a; self._set_zn(self.x); cyc = 2
        elif op == 0xA8: self.y = self.a; self._set_zn(self.y); cyc = 2
        elif op == 0x8A: self.a = self.x; self._set_zn(self.a); cyc = 2
        elif op == 0x98: self.a = self.y; self._set_zn(self.a); cyc = 2
        elif op == 0xBA: self.x = self.sp; self._set_zn(self.x); cyc = 2
        elif op == 0x9A: self.sp = self.x; cyc = 2
        elif op == 0xE8: self.x = (self.x + 1) & 0xFF; self._set_zn(self.x); cyc = 2
        elif op == 0xC8: self.y = (self.y + 1) & 0xFF; self._set_zn(self.y); cyc = 2
        elif op == 0xCA: self.x = (self.x - 1) & 0xFF; self._set_zn(self.x); cyc = 2
        elif op == 0x88: self.y = (self.y - 1) & 0xFF; self._set_zn(self.y); cyc = 2
        # ADC
        elif op == 0x69: self._adc(self._read(self._imm())); cyc = 2
        elif op == 0x65: self._adc(self._read(self._zp()));  cyc = 3
        elif op == 0x75: self._adc(self._read(self._zpx())); cyc = 4
        elif op == 0x6D: self._adc(self._read(self._abs())); cyc = 4
        elif op == 0x7D: self._adc(self._read(self._abx(True))); cyc = 4
        elif op == 0x79: self._adc(self._read(self._aby(True))); cyc = 4
        elif op == 0x61: self._adc(self._read(self._izx())); cyc = 6
        elif op == 0x71: self._adc(self._read(self._izy(True))); cyc = 5
        # SBC (incl. illegal $EB alias)
        elif op == 0xE9 or op == 0xEB: self._sbc(self._read(self._imm())); cyc = 2
        elif op == 0xE5: self._sbc(self._read(self._zp()));  cyc = 3
        elif op == 0xF5: self._sbc(self._read(self._zpx())); cyc = 4
        elif op == 0xED: self._sbc(self._read(self._abs())); cyc = 4
        elif op == 0xFD: self._sbc(self._read(self._abx(True))); cyc = 4
        elif op == 0xF9: self._sbc(self._read(self._aby(True))); cyc = 4
        elif op == 0xE1: self._sbc(self._read(self._izx())); cyc = 6
        elif op == 0xF1: self._sbc(self._read(self._izy(True))); cyc = 5
        # AND / ORA / EOR
        elif op == 0x29: self.a &= self._read(self._imm()); self._set_zn(self.a); cyc = 2
        elif op == 0x25: self.a &= self._read(self._zp());  self._set_zn(self.a); cyc = 3
        elif op == 0x35: self.a &= self._read(self._zpx()); self._set_zn(self.a); cyc = 4
        elif op == 0x2D: self.a &= self._read(self._abs()); self._set_zn(self.a); cyc = 4
        elif op == 0x3D: self.a &= self._read(self._abx(True)); self._set_zn(self.a); cyc = 4
        elif op == 0x39: self.a &= self._read(self._aby(True)); self._set_zn(self.a); cyc = 4
        elif op == 0x21: self.a &= self._read(self._izx()); self._set_zn(self.a); cyc = 6
        elif op == 0x31: self.a &= self._read(self._izy(True)); self._set_zn(self.a); cyc = 5
        elif op == 0x09: self.a |= self._read(self._imm()); self._set_zn(self.a); cyc = 2
        elif op == 0x05: self.a |= self._read(self._zp());  self._set_zn(self.a); cyc = 3
        elif op == 0x15: self.a |= self._read(self._zpx()); self._set_zn(self.a); cyc = 4
        elif op == 0x0D: self.a |= self._read(self._abs()); self._set_zn(self.a); cyc = 4
        elif op == 0x1D: self.a |= self._read(self._abx(True)); self._set_zn(self.a); cyc = 4
        elif op == 0x19: self.a |= self._read(self._aby(True)); self._set_zn(self.a); cyc = 4
        elif op == 0x01: self.a |= self._read(self._izx()); self._set_zn(self.a); cyc = 6
        elif op == 0x11: self.a |= self._read(self._izy(True)); self._set_zn(self.a); cyc = 5
        elif op == 0x49: self.a ^= self._read(self._imm()); self._set_zn(self.a); cyc = 2
        elif op == 0x45: self.a ^= self._read(self._zp());  self._set_zn(self.a); cyc = 3
        elif op == 0x55: self.a ^= self._read(self._zpx()); self._set_zn(self.a); cyc = 4
        elif op == 0x4D: self.a ^= self._read(self._abs()); self._set_zn(self.a); cyc = 4
        elif op == 0x5D: self.a ^= self._read(self._abx(True)); self._set_zn(self.a); cyc = 4
        elif op == 0x59: self.a ^= self._read(self._aby(True)); self._set_zn(self.a); cyc = 4
        elif op == 0x41: self.a ^= self._read(self._izx()); self._set_zn(self.a); cyc = 6
        elif op == 0x51: self.a ^= self._read(self._izy(True)); self._set_zn(self.a); cyc = 5
        # CMP / CPX / CPY
        elif op == 0xC9: self._cmp(self.a, self._read(self._imm())); cyc = 2
        elif op == 0xC5: self._cmp(self.a, self._read(self._zp())); cyc = 3
        elif op == 0xD5: self._cmp(self.a, self._read(self._zpx())); cyc = 4
        elif op == 0xCD: self._cmp(self.a, self._read(self._abs())); cyc = 4
        elif op == 0xDD: self._cmp(self.a, self._read(self._abx(True))); cyc = 4
        elif op == 0xD9: self._cmp(self.a, self._read(self._aby(True))); cyc = 4
        elif op == 0xC1: self._cmp(self.a, self._read(self._izx())); cyc = 6
        elif op == 0xD1: self._cmp(self.a, self._read(self._izy(True))); cyc = 5
        elif op == 0xE0: self._cmp(self.x, self._read(self._imm())); cyc = 2
        elif op == 0xE4: self._cmp(self.x, self._read(self._zp())); cyc = 3
        elif op == 0xEC: self._cmp(self.x, self._read(self._abs())); cyc = 4
        elif op == 0xC0: self._cmp(self.y, self._read(self._imm())); cyc = 2
        elif op == 0xC4: self._cmp(self.y, self._read(self._zp())); cyc = 3
        elif op == 0xCC: self._cmp(self.y, self._read(self._abs())); cyc = 4
        # Branches
        elif op == 0x10: self._branch((self.status & FLAG_N) == 0); cyc = 2
        elif op == 0x30: self._branch((self.status & FLAG_N) != 0); cyc = 2
        elif op == 0x50: self._branch((self.status & FLAG_V) == 0); cyc = 2
        elif op == 0x70: self._branch((self.status & FLAG_V) != 0); cyc = 2
        elif op == 0x90: self._branch((self.status & FLAG_C) == 0); cyc = 2
        elif op == 0xB0: self._branch((self.status & FLAG_C) != 0); cyc = 2
        elif op == 0xD0: self._branch((self.status & FLAG_Z) == 0); cyc = 2
        elif op == 0xF0: self._branch((self.status & FLAG_Z) != 0); cyc = 2
        # Flags
        elif op == 0x18: self.status &= ~FLAG_C & 0xFF; cyc = 2
        elif op == 0x38: self.status |= FLAG_C; cyc = 2
        elif op == 0x58: self.status &= ~FLAG_I & 0xFF; cyc = 2
        elif op == 0x78: self.status |= FLAG_I; cyc = 2
        elif op == 0xD8: self.status &= ~FLAG_D & 0xFF; cyc = 2
        elif op == 0xF8: self.status |= FLAG_D; cyc = 2
        elif op == 0xB8: self.status &= ~FLAG_V & 0xFF; cyc = 2
        # Stack
        elif op == 0x48: self._push(self.a); cyc = 3
        elif op == 0x68: self.a = self._pull(); self._set_zn(self.a); cyc = 4
        elif op == 0x08: self._push(self.status | FLAG_B | FLAG_U); cyc = 3
        elif op == 0x28: self.status = (self._pull() | FLAG_U) & ~FLAG_B & 0xFF; cyc = 4
        # JMP / JSR / RTS / BRK / RTI
        elif op == 0x4C: self.pc = self._abs(); cyc = 3
        elif op == 0x6C: self.pc = self._ind(); cyc = 5
        elif op == 0x20:
            target = self._abs()
            ret = (self.pc - 1) & 0xFFFF
            self._push((ret >> 8) & 0xFF); self._push(ret & 0xFF)
            self.pc = target; cyc = 6
        elif op == 0x60:
            lo = self._pull(); hi = self._pull()
            self.pc = ((hi << 8) | lo) + 1; self.pc &= 0xFFFF; cyc = 6
        elif op == 0x00:
            self.pc = (self.pc + 1) & 0xFFFF
            self._push((self.pc >> 8) & 0xFF); self._push(self.pc & 0xFF)
            self._push(self.status | FLAG_B | FLAG_U)
            self.status |= FLAG_I
            self.pc = self._read16(0xFFFE); cyc = 7
        elif op == 0x40:
            self.status = (self._pull() | FLAG_U) & ~FLAG_B & 0xFF
            lo = self._pull(); hi = self._pull()
            self.pc = lo | (hi << 8); cyc = 6
        # BIT
        elif op == 0x24:
            v = self._read(self._zp())
            if (self.a & v) == 0: self.status |= FLAG_Z
            else: self.status &= ~FLAG_Z & 0xFF
            if v & 0x40: self.status |= FLAG_V
            else: self.status &= ~FLAG_V & 0xFF
            if v & 0x80: self.status |= FLAG_N
            else: self.status &= ~FLAG_N & 0xFF
            cyc = 3
        elif op == 0x2C:
            v = self._read(self._abs())
            if (self.a & v) == 0: self.status |= FLAG_Z
            else: self.status &= ~FLAG_Z & 0xFF
            if v & 0x40: self.status |= FLAG_V
            else: self.status &= ~FLAG_V & 0xFF
            if v & 0x80: self.status |= FLAG_N
            else: self.status &= ~FLAG_N & 0xFF
            cyc = 4
        # INC / DEC memory
        elif op == 0xE6: a = self._zp();  v = (self._read(a)+1)&0xFF; self._write(a,v); self._set_zn(v); cyc = 5
        elif op == 0xF6: a = self._zpx(); v = (self._read(a)+1)&0xFF; self._write(a,v); self._set_zn(v); cyc = 6
        elif op == 0xEE: a = self._abs(); v = (self._read(a)+1)&0xFF; self._write(a,v); self._set_zn(v); cyc = 6
        elif op == 0xFE: a = self._abx(False); v = (self._read(a)+1)&0xFF; self._write(a,v); self._set_zn(v); cyc = 7
        elif op == 0xC6: a = self._zp();  v = (self._read(a)-1)&0xFF; self._write(a,v); self._set_zn(v); cyc = 5
        elif op == 0xD6: a = self._zpx(); v = (self._read(a)-1)&0xFF; self._write(a,v); self._set_zn(v); cyc = 6
        elif op == 0xCE: a = self._abs(); v = (self._read(a)-1)&0xFF; self._write(a,v); self._set_zn(v); cyc = 6
        elif op == 0xDE: a = self._abx(False); v = (self._read(a)-1)&0xFF; self._write(a,v); self._set_zn(v); cyc = 7
        # Shifts / rotates
        elif op == 0x0A:
            if self.a & 0x80: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            self.a = (self.a << 1) & 0xFF; self._set_zn(self.a); cyc = 2
        elif op == 0x06: a=self._zp();  v=self._read(a); self.status = (self.status|FLAG_C) if (v&0x80) else (self.status&~FLAG_C&0xFF); v=(v<<1)&0xFF; self._write(a,v); self._set_zn(v); cyc=5
        elif op == 0x16: a=self._zpx(); v=self._read(a); self.status = (self.status|FLAG_C) if (v&0x80) else (self.status&~FLAG_C&0xFF); v=(v<<1)&0xFF; self._write(a,v); self._set_zn(v); cyc=6
        elif op == 0x0E: a=self._abs(); v=self._read(a); self.status = (self.status|FLAG_C) if (v&0x80) else (self.status&~FLAG_C&0xFF); v=(v<<1)&0xFF; self._write(a,v); self._set_zn(v); cyc=6
        elif op == 0x1E: a=self._abx(False); v=self._read(a); self.status = (self.status|FLAG_C) if (v&0x80) else (self.status&~FLAG_C&0xFF); v=(v<<1)&0xFF; self._write(a,v); self._set_zn(v); cyc=7
        elif op == 0x4A:
            if self.a & 1: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            self.a = (self.a >> 1) & 0xFF; self._set_zn(self.a); cyc = 2
        elif op == 0x46: a=self._zp();  v=self._read(a); self.status = (self.status|FLAG_C) if (v&1) else (self.status&~FLAG_C&0xFF); v=(v>>1)&0xFF; self._write(a,v); self._set_zn(v); cyc=5
        elif op == 0x56: a=self._zpx(); v=self._read(a); self.status = (self.status|FLAG_C) if (v&1) else (self.status&~FLAG_C&0xFF); v=(v>>1)&0xFF; self._write(a,v); self._set_zn(v); cyc=6
        elif op == 0x4E: a=self._abs(); v=self._read(a); self.status = (self.status|FLAG_C) if (v&1) else (self.status&~FLAG_C&0xFF); v=(v>>1)&0xFF; self._write(a,v); self._set_zn(v); cyc=6
        elif op == 0x5E: a=self._abx(False); v=self._read(a); self.status = (self.status|FLAG_C) if (v&1) else (self.status&~FLAG_C&0xFF); v=(v>>1)&0xFF; self._write(a,v); self._set_zn(v); cyc=7
        elif op == 0x2A:
            ci = 1 if (self.status & FLAG_C) else 0
            if self.a & 0x80: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            self.a = ((self.a << 1) | ci) & 0xFF; self._set_zn(self.a); cyc = 2
        elif op == 0x26 or op == 0x36 or op == 0x2E or op == 0x3E:
            if op == 0x26: a=self._zp();  cyc=5
            elif op == 0x36: a=self._zpx(); cyc=6
            elif op == 0x2E: a=self._abs(); cyc=6
            else: a=self._abx(False); cyc=7
            v = self._read(a); ci = 1 if (self.status & FLAG_C) else 0
            if v & 0x80: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            v = ((v << 1) | ci) & 0xFF; self._write(a, v); self._set_zn(v)
        elif op == 0x6A:
            ci = 1 if (self.status & FLAG_C) else 0
            if self.a & 1: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            self.a = ((self.a >> 1) | (ci << 7)) & 0xFF; self._set_zn(self.a); cyc = 2
        elif op == 0x66 or op == 0x76 or op == 0x6E or op == 0x7E:
            if op == 0x66: a=self._zp();  cyc=5
            elif op == 0x76: a=self._zpx(); cyc=6
            elif op == 0x6E: a=self._abs(); cyc=6
            else: a=self._abx(False); cyc=7
            v = self._read(a); ci = 1 if (self.status & FLAG_C) else 0
            if v & 1: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            v = ((v >> 1) | (ci << 7)) & 0xFF; self._write(a, v); self._set_zn(v)
        # NOPs (multi-byte)
        elif op == 0x1A or op == 0x3A or op == 0x5A or op == 0x7A or op == 0xDA or op == 0xFA: cyc = 2
        elif op == 0x80 or op == 0x82 or op == 0x89 or op == 0xC2 or op == 0xE2:
            self._imm(); cyc = 2
        elif op == 0x04 or op == 0x44 or op == 0x64:
            self._zp(); cyc = 3
        elif op == 0x14 or op == 0x34 or op == 0x54 or op == 0x74 or op == 0xD4 or op == 0xF4:
            self._zpx(); cyc = 4
        elif op == 0x0C:
            self._abs(); cyc = 4
        elif op == 0x1C or op == 0x3C or op == 0x5C or op == 0x7C or op == 0xDC or op == 0xFC:
            self._abx(True); cyc = 4
        # LAX
        elif op == 0xA7: v=self._read(self._zp());  self.a=v; self.x=v; self._set_zn(v); cyc=3
        elif op == 0xB7: v=self._read(self._zpy()); self.a=v; self.x=v; self._set_zn(v); cyc=4
        elif op == 0xAF: v=self._read(self._abs()); self.a=v; self.x=v; self._set_zn(v); cyc=4
        elif op == 0xBF: v=self._read(self._aby(True)); self.a=v; self.x=v; self._set_zn(v); cyc=4
        elif op == 0xA3: v=self._read(self._izx()); self.a=v; self.x=v; self._set_zn(v); cyc=6
        elif op == 0xB3: v=self._read(self._izy(True)); self.a=v; self.x=v; self._set_zn(v); cyc=5
        # SAX
        elif op == 0x87: self._write(self._zp(),  self.a & self.x); cyc=3
        elif op == 0x97: self._write(self._zpy(), self.a & self.x); cyc=4
        elif op == 0x8F: self._write(self._abs(), self.a & self.x); cyc=4
        elif op == 0x83: self._write(self._izx(), self.a & self.x); cyc=6
        # DCP / ISB / SLO / RLA / SRE / RRA — see batch handler below
        elif op == 0xC7 or op == 0xD7 or op == 0xCF or op == 0xDF or op == 0xDB or op == 0xC3 or op == 0xD3:
            if   op == 0xC7: a=self._zp();  cyc=5
            elif op == 0xD7: a=self._zpx(); cyc=6
            elif op == 0xCF: a=self._abs(); cyc=6
            elif op == 0xDF: a=self._abx(False); cyc=7
            elif op == 0xDB: a=self._aby(False); cyc=7
            elif op == 0xC3: a=self._izx(); cyc=8
            else:            a=self._izy(False); cyc=8
            v = (self._read(a) - 1) & 0xFF; self._write(a, v); self._cmp(self.a, v)
        elif op == 0xE7 or op == 0xF7 or op == 0xEF or op == 0xFF or op == 0xFB or op == 0xE3 or op == 0xF3:
            if   op == 0xE7: a=self._zp();  cyc=5
            elif op == 0xF7: a=self._zpx(); cyc=6
            elif op == 0xEF: a=self._abs(); cyc=6
            elif op == 0xFF: a=self._abx(False); cyc=7
            elif op == 0xFB: a=self._aby(False); cyc=7
            elif op == 0xE3: a=self._izx(); cyc=8
            else:            a=self._izy(False); cyc=8
            v = (self._read(a) + 1) & 0xFF; self._write(a, v); self._sbc(v)
        elif op == 0x07 or op == 0x17 or op == 0x0F or op == 0x1F or op == 0x1B or op == 0x03 or op == 0x13:
            if   op == 0x07: a=self._zp();  cyc=5
            elif op == 0x17: a=self._zpx(); cyc=6
            elif op == 0x0F: a=self._abs(); cyc=6
            elif op == 0x1F: a=self._abx(False); cyc=7
            elif op == 0x1B: a=self._aby(False); cyc=7
            elif op == 0x03: a=self._izx(); cyc=8
            else:            a=self._izy(False); cyc=8
            v = self._read(a)
            if v & 0x80: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            v = (v << 1) & 0xFF; self._write(a, v)
            self.a |= v; self._set_zn(self.a)
        elif op == 0x27 or op == 0x37 or op == 0x2F or op == 0x3F or op == 0x3B or op == 0x23 or op == 0x33:
            if   op == 0x27: a=self._zp();  cyc=5
            elif op == 0x37: a=self._zpx(); cyc=6
            elif op == 0x2F: a=self._abs(); cyc=6
            elif op == 0x3F: a=self._abx(False); cyc=7
            elif op == 0x3B: a=self._aby(False); cyc=7
            elif op == 0x23: a=self._izx(); cyc=8
            else:            a=self._izy(False); cyc=8
            v = self._read(a); ci = 1 if (self.status & FLAG_C) else 0
            if v & 0x80: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            v = ((v << 1) | ci) & 0xFF; self._write(a, v)
            self.a &= v; self._set_zn(self.a)
        elif op == 0x47 or op == 0x57 or op == 0x4F or op == 0x5F or op == 0x5B or op == 0x43 or op == 0x53:
            if   op == 0x47: a=self._zp();  cyc=5
            elif op == 0x57: a=self._zpx(); cyc=6
            elif op == 0x4F: a=self._abs(); cyc=6
            elif op == 0x5F: a=self._abx(False); cyc=7
            elif op == 0x5B: a=self._aby(False); cyc=7
            elif op == 0x43: a=self._izx(); cyc=8
            else:            a=self._izy(False); cyc=8
            v = self._read(a)
            if v & 1: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            v = (v >> 1) & 0xFF; self._write(a, v)
            self.a ^= v; self._set_zn(self.a)
        elif op == 0x67 or op == 0x77 or op == 0x6F or op == 0x7F or op == 0x7B or op == 0x63 or op == 0x73:
            if   op == 0x67: a=self._zp();  cyc=5
            elif op == 0x77: a=self._zpx(); cyc=6
            elif op == 0x6F: a=self._abs(); cyc=6
            elif op == 0x7F: a=self._abx(False); cyc=7
            elif op == 0x7B: a=self._aby(False); cyc=7
            elif op == 0x63: a=self._izx(); cyc=8
            else:            a=self._izy(False); cyc=8
            v = self._read(a); ci = 1 if (self.status & FLAG_C) else 0
            if v & 1: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            v = ((v >> 1) | (ci << 7)) & 0xFF; self._write(a, v)
            self._adc(v)
        elif op == 0x0B or op == 0x2B:
            self.a &= self._read(self._imm()); self._set_zn(self.a)
            if self.a & 0x80: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            cyc = 2
        elif op == 0x4B:
            self.a &= self._read(self._imm())
            if self.a & 1: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            self.a >>= 1; self._set_zn(self.a); cyc = 2
        elif op == 0x6B:
            self.a &= self._read(self._imm())
            ci = 1 if (self.status & FLAG_C) else 0
            self.a = ((self.a >> 1) | (ci << 7)) & 0xFF; self._set_zn(self.a)
            if self.a & 0x40: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            if (((self.a >> 6) ^ (self.a >> 5)) & 1): self.status |= FLAG_V
            else: self.status &= ~FLAG_V & 0xFF
            cyc = 2
        elif op == 0xCB:
            v = self._read(self._imm())
            t = ((self.a & self.x) - v) & 0x1FF
            self.x = t & 0xFF
            if (self.a & self.x) >= v: self.status |= FLAG_C
            else: self.status &= ~FLAG_C & 0xFF
            self._set_zn(self.x); cyc = 2
        else:
            cyc = 2  # unrecognised: treat as NOP

        self.status |= FLAG_U
        self.cycles += cyc
        self.executed_instr += 1
        # PPU + APU clocking + MMC3 IRQ scanline detection.
        cdef int old_sl = self.ppu.scanline
        self.ppu.step(cyc * 3)
        self.apu.step(cyc)
        cdef int new_sl
        if self.cart.mapper == 4:
            new_sl = self.ppu.scanline
            if new_sl != old_sl and (self.ppu.mask & 0x18) and 0 <= new_sl < 240:
                if self.cart.mmc3_irq_counter == 0 or self.cart.mmc3_irq_reload:
                    self.cart.mmc3_irq_counter = self.cart.mmc3_irq_latch
                    self.cart.mmc3_irq_reload = False
                else:
                    self.cart.mmc3_irq_counter -= 1
                if self.cart.mmc3_irq_counter == 0 and self.cart.mmc3_irq_enable:
                    self.cart.mmc3_irq_pending = True


# ---------------------------------------------------------------------------
# Core — the public facade the GUI talks to.
# ---------------------------------------------------------------------------

cdef class Core:
    cdef public Cpu cpu
    cdef public Ppu ppu
    cdef public Apu apu
    cdef public Cart cart
    cdef public bint rom_loaded
    cdef public str debug_info
    cdef public str rom_path
    cdef public int frame_counter

    def __cinit__(self):
        self.ppu = Ppu()
        self.apu = Apu()
        self.cart = Cart()
        self.cpu = Cpu()
        self.cpu.ppu = self.ppu
        self.cpu.apu = self.apu
        self.cpu.cart = self.cart
        # Wire DMC sample fetch through the CPU bus.
        self.apu.cpu_read_fn = self._dmc_read
        self.rom_loaded = False
        self.debug_info = ""
        self.rom_path = ""
        self.frame_counter = 0

    def _dmc_read(self, int addr):
        return self.cpu._read(addr)

    cpdef bint load_rom(self, str path):
        if not self.cart.load(path):
            return False
        # Reset CPU state.
        self.cpu.a = 0; self.cpu.x = 0; self.cpu.y = 0; self.cpu.sp = 0xFD
        self.cpu.status = 0x24; self.cpu.cycles = 7
        self.cpu.executed_instr = 0
        # Wipe PPU + APU live state.
        self.ppu = Ppu()
        self.ppu.set_mirroring(self.cart.mirroring)
        self.cpu.ppu = self.ppu
        self.apu = Apu()
        self.apu.cpu_read_fn = self._dmc_read
        self.cpu.apu = self.apu
        # Push CHR data into PPU pattern table.
        self.cart.apply_chr(self.ppu)
        # Read reset vector through the CPU bus (so mappers see it).
        self.cpu.pc = self.cpu._read(0xFFFC) | (self.cpu._read(0xFFFD) << 8)
        self.frame_counter = 0
        self.rom_loaded = True
        self.rom_path = path
        self.debug_info = "Mapper {} | PRG {}KB | CHR {}KB | Mirror {} | Reset ${:04X}".format(
            self.cart.mapper, self.cart.prg_size // 1024,
            self.cart.chr_size // 1024, self.cart.mirroring, self.cpu.pc)
        return True

    cpdef str run_frame(self):
        if not self.rom_loaded:
            return "No ROM loaded"
        cdef int target = self.cpu.cycles + 29780
        while self.cpu.cycles < target:
            self.cpu.step_instruction()
        self.frame_counter += 1
        self.ppu.render_frame()
        self.debug_info = "F:{} PC:${:04X} A:${:02X} X:${:02X} Y:${:02X} P:${:02X} SP:${:02X} SL:{}".format(
            self.frame_counter, self.cpu.pc, self.cpu.a, self.cpu.x, self.cpu.y,
            self.cpu.status, self.cpu.sp, self.ppu.scanline)
        # Persist battery RAM each frame (cheap; OS caches it).
        if self.cart.has_battery and self.rom_path:
            self.cart.save_battery(self.rom_path)
        return self.debug_info

    cpdef bytes get_framebuffer(self):
        return bytes(self.ppu.framebuffer)

    cpdef bytes consume_audio(self):
        return self.apu.consume_audio()

    cpdef int consume_apu_activity(self):
        return self.apu.consume_activity()

    cpdef set_controller1(self, int state):
        self.cpu.controller1_state = state & 0xFF
        if self.cpu.controller_strobe:
            self.cpu.controller1_shift = self.cpu.controller1_state

    cpdef set_controller2(self, int state):
        self.cpu.controller2_state = state & 0xFF
        if self.cpu.controller_strobe:
            self.cpu.controller2_shift = self.cpu.controller2_state

    cpdef reset(self):
        if not self.rom_loaded:
            return
        self.cpu.a = 0; self.cpu.x = 0; self.cpu.y = 0; self.cpu.sp = 0xFD
        self.cpu.status = 0x24; self.cpu.cycles = 7
        self.cpu.executed_instr = 0
        self.ppu = Ppu()
        self.ppu.set_mirroring(self.cart.mirroring)
        self.cpu.ppu = self.ppu
        self.apu = Apu()
        self.apu.cpu_read_fn = self._dmc_read
        self.cpu.apu = self.apu
        self.cart.apply_chr(self.ppu)
        self.cpu.pc = self.cpu._read(0xFFFC) | (self.cpu._read(0xFFFD) << 8)
        self.frame_counter = 0

    cpdef dict save_state(self):
        cdef int i
        cdef list mmc3_banks_list = []
        for i in range(8):
            mmc3_banks_list.append(self.cart.mmc3_banks[i])
        return {
            "cpu": {
                "pc": self.cpu.pc, "a": self.cpu.a, "x": self.cpu.x, "y": self.cpu.y,
                "sp": self.cpu.sp, "status": self.cpu.status, "cycles": self.cpu.cycles,
                "ram": bytes(self.cpu.ram),
                "ctl1s": self.cpu.controller1_state, "ctl2s": self.cpu.controller2_state,
            },
            "ppu": {
                "vram": bytes(self.ppu.vram), "vrx": bytes(self.ppu.vram_extra),
                "pal": bytes(self.ppu.palette_ram), "oam": bytes(self.ppu.oam),
                "chr": bytes(self.ppu.chr),
                "ctrl": self.ppu.ctrl, "mask": self.ppu.mask, "status": self.ppu.status,
                "v": self.ppu.v, "t": self.ppu.t, "x": self.ppu.x, "w": self.ppu.w,
                "buf": self.ppu.ppu_data_buffer,
                "sl": self.ppu.scanline, "dot": self.ppu.dot, "frame": self.ppu.frame,
                "mirror": self.ppu.mirroring,
            },
            "cart": {
                "mmc1": (self.cart.mmc1_shift, self.cart.mmc1_ctrl, self.cart.mmc1_chr0,
                         self.cart.mmc1_chr1, self.cart.mmc1_prg),
                "uxrom": self.cart.uxrom_bank, "cnrom": self.cart.cnrom_chr,
                "mmc3_select": self.cart.mmc3_select,
                "mmc3_banks": mmc3_banks_list,
                "mmc3_modes": (self.cart.mmc3_prg_mode, self.cart.mmc3_chr_mode),
                "mmc3_irq": (self.cart.mmc3_irq_latch, self.cart.mmc3_irq_counter,
                             self.cart.mmc3_irq_enable, self.cart.mmc3_irq_pending),
                "axrom": self.cart.axrom_prg,
                "prg_ram": bytes(self.cart.prg_ram),
                "mirror": self.cart.mirroring,
            },
            "frame_counter": self.frame_counter,
        }

    cpdef load_state(self, dict s):
        cdef dict c = s["cpu"]
        self.cpu.pc = c["pc"]; self.cpu.a = c["a"]; self.cpu.x = c["x"]; self.cpu.y = c["y"]
        self.cpu.sp = c["sp"]; self.cpu.status = c["status"]; self.cpu.cycles = c["cycles"]
        self.cpu.ram[:] = c["ram"]
        self.cpu.controller1_state = c.get("ctl1s", 0)
        self.cpu.controller2_state = c.get("ctl2s", 0)
        cdef dict p = s["ppu"]
        self.ppu.vram[:] = p["vram"]; self.ppu.vram_extra[:] = p["vrx"]
        self.ppu.palette_ram[:] = p["pal"]; self.ppu.oam[:] = p["oam"]
        self.ppu.chr[:] = p["chr"]
        self.ppu.ctrl = p["ctrl"]; self.ppu.mask = p["mask"]; self.ppu.status = p["status"]
        self.ppu.v = p["v"]; self.ppu.t = p["t"]; self.ppu.x = p["x"]; self.ppu.w = p["w"]
        self.ppu.ppu_data_buffer = p["buf"]
        self.ppu.scanline = p["sl"]; self.ppu.dot = p["dot"]; self.ppu.frame = p["frame"]
        self.ppu.set_mirroring(p["mirror"])
        cdef dict ct = s["cart"]
        (self.cart.mmc1_shift, self.cart.mmc1_ctrl, self.cart.mmc1_chr0,
         self.cart.mmc1_chr1, self.cart.mmc1_prg) = ct["mmc1"]
        self.cart.uxrom_bank = ct["uxrom"]; self.cart.cnrom_chr = ct["cnrom"]
        self.cart.mmc3_select = ct["mmc3_select"]
        cdef int i
        for i, v in enumerate(ct["mmc3_banks"]):
            self.cart.mmc3_banks[i] = v
        self.cart.mmc3_prg_mode, self.cart.mmc3_chr_mode = ct["mmc3_modes"]
        (self.cart.mmc3_irq_latch, self.cart.mmc3_irq_counter,
         self.cart.mmc3_irq_enable, self.cart.mmc3_irq_pending) = ct["mmc3_irq"]
        self.cart.axrom_prg = ct["axrom"]
        self.cart.prg_ram[:] = ct["prg_ram"]
        self.cart.mirroring = ct["mirror"]
        self.frame_counter = s["frame_counter"]
'''

nes_core, _label = _bootstrap_cython_core()
if nes_core is not None:
    CORE_LOADED = True
    CORE_NAME = _label
else:
    nes_core = None

if not CORE_LOADED:
    # ------------------------------------------------------------------
    # APU: 2x Pulse, Triangle, Noise, DMC, frame counter, status, IRQ.
    # State-machine accurate enough for register reads ($4015) and frame
    # IRQ to behave correctly. Per-frame audio synthesis is exposed via
    # generate_pcm() so the UI layer can play it through the OS.
    # ------------------------------------------------------------------
    class PyApu:
        # Length counter lookup table (NES hardware ROM).
        LENGTH_TABLE = [
            10, 254, 20,  2, 40,  4, 80,  6, 160,  8, 60, 10, 14, 12, 26, 14,
            12,  16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30,
        ]
        # Pulse duty waveforms (8 samples each, 4 duty modes).
        DUTY_TABLE = [
            [0, 1, 0, 0, 0, 0, 0, 0],   # 12.5%
            [0, 1, 1, 0, 0, 0, 0, 0],   # 25%
            [0, 1, 1, 1, 1, 0, 0, 0],   # 50%
            [1, 0, 0, 1, 1, 1, 1, 1],   # 25% negated
        ]
        # Triangle 32-step sequencer.
        TRIANGLE_SEQ = [
            15, 14, 13, 12, 11, 10,  9,  8,  7,  6,  5,  4,  3,  2,  1,  0,
             0,  1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15,
        ]
        # NTSC noise periods (in CPU cycles).
        NOISE_PERIODS = [
              4,    8,   16,   32,   64,   96,  128,  160,
            202,  254,  380,  508,  762, 1016, 2034, 4068,
        ]
        # NTSC DMC sample rates (in CPU cycles per byte advance).
        DMC_RATES = [
            428, 380, 340, 320, 286, 254, 226, 214,
            190, 160, 142, 128, 106,  84,  72,  54,
        ]

        def __init__(self):
            # Pulse 1 / Pulse 2 state
            self.pulse_enabled = [False, False]
            self.pulse_duty = [0, 0]
            self.pulse_length_halt = [False, False]
            self.pulse_const_vol = [False, False]
            self.pulse_volume = [0, 0]
            self.pulse_env_start = [False, False]
            self.pulse_env_divider = [0, 0]
            self.pulse_env_decay = [0, 0]
            self.pulse_sweep_enabled = [False, False]
            self.pulse_sweep_period = [0, 0]
            self.pulse_sweep_negate = [False, False]
            self.pulse_sweep_shift = [0, 0]
            self.pulse_sweep_reload = [False, False]
            self.pulse_sweep_divider = [0, 0]
            self.pulse_timer_period = [0, 0]
            self.pulse_timer = [0, 0]
            self.pulse_seq_pos = [0, 0]
            self.pulse_length = [0, 0]
            # Triangle channel
            self.tri_enabled = False
            self.tri_length_halt = False  # also linear counter control
            self.tri_linear_reload_value = 0
            self.tri_linear_counter = 0
            self.tri_linear_reload = False
            self.tri_timer_period = 0
            self.tri_timer = 0
            self.tri_seq_pos = 0
            self.tri_length = 0
            # Noise channel
            self.noise_enabled = False
            self.noise_length_halt = False
            self.noise_const_vol = False
            self.noise_volume = 0
            self.noise_env_start = False
            self.noise_env_divider = 0
            self.noise_env_decay = 0
            self.noise_mode = False
            self.noise_period = self.NOISE_PERIODS[0]
            self.noise_timer = 0
            self.noise_shift = 1
            self.noise_length = 0
            # DMC channel
            self.dmc_enabled = False
            self.dmc_irq_enable = False
            self.dmc_loop = False
            self.dmc_rate = self.DMC_RATES[0]
            self.dmc_timer = 0
            self.dmc_output = 0
            self.dmc_sample_addr = 0xC000
            self.dmc_sample_length = 0
            self.dmc_current_addr = 0xC000
            self.dmc_bytes_remaining = 0
            self.dmc_sample_buffer = None  # one-byte fetch buffer
            self.dmc_shift = 0
            self.dmc_bits_remaining = 0
            # Frame counter
            self.frame_mode_5step = False  # bit 7 of $4017
            self.frame_irq_inhibit = False
            self.frame_irq = False
            self.dmc_irq = False
            self.frame_step = 0
            self.frame_cycle = 0
            # Used by host to fetch DMC samples.
            self.cpu_read = None  # injected by core: cpu_read(addr) -> byte

        # ------------- public register interface ----------------------
        def write_register(self, reg, value):
            value &= 0xFF
            if reg == 0x4000 or reg == 0x4004:
                ch = 0 if reg == 0x4000 else 1
                self.pulse_duty[ch] = (value >> 6) & 0x03
                self.pulse_length_halt[ch] = (value & 0x20) != 0
                self.pulse_const_vol[ch] = (value & 0x10) != 0
                self.pulse_volume[ch] = value & 0x0F
            elif reg == 0x4001 or reg == 0x4005:
                ch = 0 if reg == 0x4001 else 1
                self.pulse_sweep_enabled[ch] = (value & 0x80) != 0
                self.pulse_sweep_period[ch] = (value >> 4) & 0x07
                self.pulse_sweep_negate[ch] = (value & 0x08) != 0
                self.pulse_sweep_shift[ch] = value & 0x07
                self.pulse_sweep_reload[ch] = True
            elif reg == 0x4002 or reg == 0x4006:
                ch = 0 if reg == 0x4002 else 1
                self.pulse_timer_period[ch] = (self.pulse_timer_period[ch] & 0x700) | value
            elif reg == 0x4003 or reg == 0x4007:
                ch = 0 if reg == 0x4003 else 1
                self.pulse_timer_period[ch] = (self.pulse_timer_period[ch] & 0xFF) | ((value & 0x07) << 8)
                if self.pulse_enabled[ch]:
                    self.pulse_length[ch] = self.LENGTH_TABLE[(value >> 3) & 0x1F]
                self.pulse_seq_pos[ch] = 0
                self.pulse_env_start[ch] = True
            elif reg == 0x4008:
                self.tri_length_halt = (value & 0x80) != 0
                self.tri_linear_reload_value = value & 0x7F
            elif reg == 0x400A:
                self.tri_timer_period = (self.tri_timer_period & 0x700) | value
            elif reg == 0x400B:
                self.tri_timer_period = (self.tri_timer_period & 0xFF) | ((value & 0x07) << 8)
                if self.tri_enabled:
                    self.tri_length = self.LENGTH_TABLE[(value >> 3) & 0x1F]
                self.tri_linear_reload = True
            elif reg == 0x400C:
                self.noise_length_halt = (value & 0x20) != 0
                self.noise_const_vol = (value & 0x10) != 0
                self.noise_volume = value & 0x0F
            elif reg == 0x400E:
                self.noise_mode = (value & 0x80) != 0
                self.noise_period = self.NOISE_PERIODS[value & 0x0F]
            elif reg == 0x400F:
                if self.noise_enabled:
                    self.noise_length = self.LENGTH_TABLE[(value >> 3) & 0x1F]
                self.noise_env_start = True
            elif reg == 0x4010:
                self.dmc_irq_enable = (value & 0x80) != 0
                self.dmc_loop = (value & 0x40) != 0
                self.dmc_rate = self.DMC_RATES[value & 0x0F]
                if not self.dmc_irq_enable:
                    self.dmc_irq = False
            elif reg == 0x4011:
                self.dmc_output = value & 0x7F
            elif reg == 0x4012:
                self.dmc_sample_addr = 0xC000 | (value << 6)
            elif reg == 0x4013:
                self.dmc_sample_length = (value << 4) | 1
            elif reg == 0x4015:
                # Channel enables; clearing a bit also zeros that length counter.
                self.pulse_enabled[0] = (value & 0x01) != 0
                if not self.pulse_enabled[0]:
                    self.pulse_length[0] = 0
                self.pulse_enabled[1] = (value & 0x02) != 0
                if not self.pulse_enabled[1]:
                    self.pulse_length[1] = 0
                self.tri_enabled = (value & 0x04) != 0
                if not self.tri_enabled:
                    self.tri_length = 0
                self.noise_enabled = (value & 0x08) != 0
                if not self.noise_enabled:
                    self.noise_length = 0
                if value & 0x10:
                    if self.dmc_bytes_remaining == 0:
                        self.dmc_current_addr = self.dmc_sample_addr
                        self.dmc_bytes_remaining = self.dmc_sample_length
                    self.dmc_enabled = True
                else:
                    self.dmc_enabled = False
                    self.dmc_bytes_remaining = 0
                self.dmc_irq = False
            elif reg == 0x4017:
                self.frame_mode_5step = (value & 0x80) != 0
                self.frame_irq_inhibit = (value & 0x40) != 0
                if self.frame_irq_inhibit:
                    self.frame_irq = False
                self.frame_step = 0
                self.frame_cycle = 0
                if self.frame_mode_5step:
                    # 5-step mode immediately clocks envelope+length.
                    self._clock_quarter_frame()
                    self._clock_half_frame()

        def read_status(self):
            v = 0
            if self.pulse_length[0] > 0: v |= 0x01
            if self.pulse_length[1] > 0: v |= 0x02
            if self.tri_length > 0:      v |= 0x04
            if self.noise_length > 0:    v |= 0x08
            if self.dmc_bytes_remaining > 0: v |= 0x10
            if self.frame_irq: v |= 0x40
            if self.dmc_irq:   v |= 0x80
            self.frame_irq = False  # cleared on read
            return v

        # ------------- internal clocking ------------------------------
        def _clock_envelope(self, ch):
            if self.pulse_env_start[ch]:
                self.pulse_env_start[ch] = False
                self.pulse_env_decay[ch] = 15
                self.pulse_env_divider[ch] = self.pulse_volume[ch]
            else:
                if self.pulse_env_divider[ch] == 0:
                    self.pulse_env_divider[ch] = self.pulse_volume[ch]
                    if self.pulse_env_decay[ch] > 0:
                        self.pulse_env_decay[ch] -= 1
                    elif self.pulse_length_halt[ch]:
                        self.pulse_env_decay[ch] = 15
                else:
                    self.pulse_env_divider[ch] -= 1

        def _clock_noise_envelope(self):
            if self.noise_env_start:
                self.noise_env_start = False
                self.noise_env_decay = 15
                self.noise_env_divider = self.noise_volume
            else:
                if self.noise_env_divider == 0:
                    self.noise_env_divider = self.noise_volume
                    if self.noise_env_decay > 0:
                        self.noise_env_decay -= 1
                    elif self.noise_length_halt:
                        self.noise_env_decay = 15
                else:
                    self.noise_env_divider -= 1

        def _clock_sweep(self, ch):
            change = self.pulse_timer_period[ch] >> self.pulse_sweep_shift[ch]
            target = self.pulse_timer_period[ch] + (-change if self.pulse_sweep_negate[ch] else change)
            if ch == 0 and self.pulse_sweep_negate[ch]:
                target -= 1  # pulse 1 ones'-complement quirk
            mute = (self.pulse_timer_period[ch] < 8) or (target > 0x7FF)
            if self.pulse_sweep_divider[ch] == 0 and self.pulse_sweep_enabled[ch] and self.pulse_sweep_shift[ch] != 0 and not mute:
                self.pulse_timer_period[ch] = target & 0x7FF
            if self.pulse_sweep_divider[ch] == 0 or self.pulse_sweep_reload[ch]:
                self.pulse_sweep_divider[ch] = self.pulse_sweep_period[ch]
                self.pulse_sweep_reload[ch] = False
            else:
                self.pulse_sweep_divider[ch] -= 1

        def _clock_length(self):
            for ch in (0, 1):
                if not self.pulse_length_halt[ch] and self.pulse_length[ch] > 0:
                    self.pulse_length[ch] -= 1
            if not self.tri_length_halt and self.tri_length > 0:
                self.tri_length -= 1
            if not self.noise_length_halt and self.noise_length > 0:
                self.noise_length -= 1

        def _clock_linear_counter(self):
            if self.tri_linear_reload:
                self.tri_linear_counter = self.tri_linear_reload_value
            elif self.tri_linear_counter > 0:
                self.tri_linear_counter -= 1
            if not self.tri_length_halt:
                self.tri_linear_reload = False

        def _clock_quarter_frame(self):
            self._clock_envelope(0)
            self._clock_envelope(1)
            self._clock_noise_envelope()
            self._clock_linear_counter()

        def _clock_half_frame(self):
            self._clock_quarter_frame()
            self._clock_length()
            self._clock_sweep(0)
            self._clock_sweep(1)

        def step(self, cpu_cycles):
            # Frame counter is clocked at half the CPU rate; we approximate
            # by counting CPU cycles and triggering at NTSC step boundaries.
            # 4-step: ~3729, 7457, 11186, 14916  (then loop, IRQ on 14916 if not inhibited)
            # 5-step: ~3729, 7457, 11186, 14916, 18641 (no IRQ)
            for _ in range(cpu_cycles):
                self.frame_cycle += 1
                steps_4 = (3729, 7457, 11186, 14916)
                steps_5 = (3729, 7457, 11186, 14916, 18641)
                if not self.frame_mode_5step:
                    if self.frame_cycle == steps_4[0]:
                        self._clock_quarter_frame()
                    elif self.frame_cycle == steps_4[1]:
                        self._clock_half_frame()
                    elif self.frame_cycle == steps_4[2]:
                        self._clock_quarter_frame()
                    elif self.frame_cycle >= steps_4[3]:
                        self._clock_half_frame()
                        if not self.frame_irq_inhibit:
                            self.frame_irq = True
                        self.frame_cycle = 0
                else:
                    if self.frame_cycle == steps_5[0]:
                        self._clock_quarter_frame()
                    elif self.frame_cycle == steps_5[1]:
                        self._clock_half_frame()
                    elif self.frame_cycle == steps_5[2]:
                        self._clock_quarter_frame()
                    elif self.frame_cycle == steps_5[3]:
                        pass
                    elif self.frame_cycle >= steps_5[4]:
                        self._clock_half_frame()
                        self.frame_cycle = 0
            # Channel timers (very simplified — we sample per-frame instead
            # of cycle-accurately for output, which keeps Python fast enough).
            for ch in (0, 1):
                if self.pulse_timer_period[ch] >= 8:
                    advance = max(1, cpu_cycles // (self.pulse_timer_period[ch] + 1))
                    self.pulse_seq_pos[ch] = (self.pulse_seq_pos[ch] + advance) & 0x07
            if self.tri_timer_period >= 2 and self.tri_linear_counter > 0 and self.tri_length > 0:
                advance = max(1, cpu_cycles // (self.tri_timer_period + 1))
                self.tri_seq_pos = (self.tri_seq_pos + advance) & 0x1F
            # Noise LFSR (clocked roughly per CPU cycle group)
            self.noise_timer -= cpu_cycles
            while self.noise_timer <= 0:
                self.noise_timer += max(1, self.noise_period)
                fb = (self.noise_shift & 1) ^ (((self.noise_shift >> (6 if self.noise_mode else 1)) & 1))
                self.noise_shift = ((self.noise_shift >> 1) | (fb << 14)) & 0x7FFF
            # DMC byte fetch
            if self.dmc_enabled and self.dmc_bytes_remaining > 0 and callable(self.cpu_read):
                self.dmc_timer -= cpu_cycles
                while self.dmc_timer <= 0:
                    self.dmc_timer += max(1, self.dmc_rate)
                    sample = self.cpu_read(self.dmc_current_addr) & 0xFF
                    self.dmc_current_addr = ((self.dmc_current_addr + 1) & 0xFFFF) | 0x8000
                    self.dmc_bytes_remaining -= 1
                    # Apply 7-bit delta (very simplified)
                    if sample & 0x01:
                        self.dmc_output = min(127, self.dmc_output + 2)
                    else:
                        self.dmc_output = max(0, self.dmc_output - 2)
                    if self.dmc_bytes_remaining == 0:
                        if self.dmc_loop:
                            self.dmc_current_addr = self.dmc_sample_addr
                            self.dmc_bytes_remaining = self.dmc_sample_length
                        elif self.dmc_irq_enable:
                            self.dmc_irq = True
                        if self.dmc_bytes_remaining == 0:
                            break

        @property
        def irq_pending(self):
            return self.frame_irq or self.dmc_irq

        # ------------- output sampling --------------------------------
        def channel_outputs(self):
            # Returns (p1, p2, tri, noise, dmc) levels suitable for mixing.
            outs = [0, 0]
            for ch in (0, 1):
                if (self.pulse_length[ch] == 0 or self.pulse_timer_period[ch] < 8
                        or not self.pulse_enabled[ch]):
                    outs[ch] = 0
                else:
                    v = self.pulse_volume[ch] if self.pulse_const_vol[ch] else self.pulse_env_decay[ch]
                    outs[ch] = v * self.DUTY_TABLE[self.pulse_duty[ch]][self.pulse_seq_pos[ch]]
            tri = 0
            if self.tri_enabled and self.tri_length > 0 and self.tri_linear_counter > 0 and self.tri_timer_period >= 2:
                tri = self.TRIANGLE_SEQ[self.tri_seq_pos]
            noise = 0
            if self.noise_enabled and self.noise_length > 0 and (self.noise_shift & 1) == 0:
                noise = self.noise_volume if self.noise_const_vol else self.noise_env_decay
            return outs[0], outs[1], tri, noise, self.dmc_output

        def channel_frequencies(self):
            # Approximate Hz per channel for the per-frame synthesizer.
            # NTSC CPU = ~1.789773 MHz; pulse f = CPU / (16 * (T+1))
            cpu = 1789773.0
            p1 = cpu / (16.0 * (self.pulse_timer_period[0] + 1)) if self.pulse_timer_period[0] >= 8 else 0
            p2 = cpu / (16.0 * (self.pulse_timer_period[1] + 1)) if self.pulse_timer_period[1] >= 8 else 0
            tri = cpu / (32.0 * (self.tri_timer_period + 1)) if self.tri_timer_period >= 2 else 0
            return p1, p2, tri

    class PyPpu:
        NES_RGB = [
            (84, 84, 84), (0, 30, 116), (8, 16, 144), (48, 0, 136), (68, 0, 100), (92, 0, 48), (84, 4, 0), (60, 24, 0),
            (32, 42, 0), (8, 58, 0), (0, 64, 0), (0, 60, 0), (0, 50, 60), (0, 0, 0), (0, 0, 0), (0, 0, 0),
            (152, 150, 152), (8, 76, 196), (48, 50, 236), (92, 30, 228), (136, 20, 176), (160, 20, 100), (152, 34, 32), (120, 60, 0),
            (84, 90, 0), (40, 114, 0), (8, 124, 0), (0, 118, 40), (0, 102, 120), (0, 0, 0), (0, 0, 0), (0, 0, 0),
            (236, 238, 236), (76, 154, 236), (120, 124, 236), (176, 98, 236), (228, 84, 236), (236, 88, 180), (236, 106, 100), (212, 136, 32),
            (160, 170, 0), (116, 196, 0), (76, 208, 32), (56, 204, 108), (56, 180, 204), (60, 60, 60), (0, 0, 0), (0, 0, 0),
            (236, 238, 236), (168, 204, 236), (188, 188, 236), (212, 178, 236), (236, 174, 236), (236, 174, 212), (236, 180, 176), (228, 196, 144),
            (204, 210, 120), (180, 222, 120), (168, 226, 144), (152, 226, 180), (160, 214, 228), (160, 162, 160), (0, 0, 0), (0, 0, 0),
        ]

        def __init__(self):
            self.vram = bytearray(0x800)
            # Optional second 2KB for 4-screen carts (e.g. Gauntlet).
            self.vram_extra = bytearray(0x800)
            self.palette_ram = bytearray(32)
            self.oam = bytearray(256)
            self.secondary_oam = bytearray(32)  # 8 sprites x 4 bytes per scanline
            self.chr = bytearray(0x2000)
            self.ctrl = 0
            self.mask = 0
            self.status = 0
            self.oam_addr = 0
            self.v = 0
            self.t = 0
            self.x = 0
            self.w = 0
            self.ppu_data_buffer = 0
            self.nmi_pending = False
            self.scanline = 0
            self.dot = 0
            self.frame = 0
            self.framebuffer = [0] * (256 * 240)
            self.sprite0_hit = False
            # Mirroring modes:
            #   0 = horizontal arrangement -> vertical mirroring  (NT0=NT2, NT1=NT3)
            #   1 = vertical arrangement   -> horizontal mirroring (NT0=NT1, NT2=NT3)
            #   2 = single-screen, lower bank
            #   3 = single-screen, upper bank
            #   4 = four-screen (uses vram_extra as the second 2KB)
            self.mirroring = 0

        def load_chr(self, chr_data):
            if chr_data:
                ln = min(len(chr_data), 0x2000)
                self.chr[:ln] = chr_data[:ln]
                if ln < 0x2000:
                    self.chr[ln:] = b"\x00" * (0x2000 - ln)
            else:
                self.chr[:] = b"\x00" * 0x2000

        def _inc_vram_addr(self):
            self.v = (self.v + (32 if (self.ctrl & 0x04) else 1)) & 0x7FFF

        def _map_nametable_addr(self, addr):
            # Returns a tuple-friendly index into the right 2KB block.
            # Internal helper resolves to (use_extra: bool, offset: int)
            index = (addr - 0x2000) & 0x0FFF
            nt = (index >> 10) & 0x03
            off = index & 0x03FF
            mode = self.mirroring
            if mode == 4:
                # Four-screen: NT0/NT1 in main vram, NT2/NT3 in vram_extra.
                if nt < 2:
                    return (False, (nt * 0x400 + off) & 0x07FF)
                return (True, ((nt - 2) * 0x400 + off) & 0x07FF)
            if mode == 2:
                return (False, off & 0x03FF)
            if mode == 3:
                return (False, 0x400 + (off & 0x03FF))
            if mode == 1:
                # Vertical arrangement -> horizontal mirroring (NT0=NT1, NT2=NT3)
                return (False, ((nt >> 1) * 0x400 + off) & 0x07FF)
            # Default mode 0: horizontal arrangement -> vertical mirroring (NT0=NT2, NT1=NT3)
            return (False, (((nt & 1) * 0x400) + off) & 0x07FF)

        def _nt_read(self, addr):
            use_extra, off = self._map_nametable_addr(addr)
            return (self.vram_extra if use_extra else self.vram)[off]

        def _nt_write(self, addr, value):
            use_extra, off = self._map_nametable_addr(addr)
            (self.vram_extra if use_extra else self.vram)[off] = value & 0xFF

        def _palette_addr(self, addr):
            p = (addr - 0x3F00) & 0x1F
            if p in (0x10, 0x14, 0x18, 0x1C):
                p -= 0x10
            return p

        def read_ppudata(self):
            addr = self.v & 0x3FFF
            self._inc_vram_addr()
            if addr < 0x2000:
                value = self.ppu_data_buffer
                self.ppu_data_buffer = self.chr[addr]
                return value
            if addr < 0x3F00:
                value = self.ppu_data_buffer
                self.ppu_data_buffer = self._nt_read(addr)
                return value
            value = self.palette_ram[self._palette_addr(addr)]
            # PPUDATA buffer behind palette mirrors the underlying nametable byte.
            self.ppu_data_buffer = self._nt_read(addr - 0x1000)
            return value

        def write_ppudata(self, value):
            addr = self.v & 0x3FFF
            self._inc_vram_addr()
            if addr < 0x2000:
                self.chr[addr] = value
            elif addr < 0x3F00:
                self._nt_write(addr, value)
            else:
                self.palette_ram[self._palette_addr(addr)] = value & 0x3F

        def read_register(self, reg):
            reg &= 7
            if reg == 2:  # PPUSTATUS
                value = self.status
                self.status &= 0x7F
                self.w = 0
                return value
            if reg == 4:  # OAMDATA
                return self.oam[self.oam_addr]
            if reg == 7:  # PPUDATA
                return self.read_ppudata()
            return 0

        def write_register(self, reg, value):
            reg &= 7
            value &= 0xFF
            if reg == 0:  # PPUCTRL
                self.ctrl = value
                self.t = (self.t & 0x73FF) | ((value & 0x03) << 10)
            elif reg == 1:  # PPUMASK
                self.mask = value
            elif reg == 3:  # OAMADDR
                self.oam_addr = value
            elif reg == 4:  # OAMDATA
                self.oam[self.oam_addr] = value
                self.oam_addr = (self.oam_addr + 1) & 0xFF
            elif reg == 5:  # PPUSCROLL
                if self.w == 0:
                    self.t = (self.t & 0x7FE0) | (value >> 3)
                    self.x = value & 0x07
                    self.w = 1
                else:
                    self.t = (self.t & 0x0C1F) | ((value & 0x07) << 12) | ((value & 0xF8) << 2)
                    self.w = 0
            elif reg == 6:  # PPUADDR
                if self.w == 0:
                    self.t = (self.t & 0x00FF) | ((value & 0x3F) << 8)
                    self.w = 1
                else:
                    self.t = (self.t & 0x7F00) | value
                    self.v = self.t
                    self.w = 0
            elif reg == 7:
                self.write_ppudata(value)

        def oam_dma(self, page_data):
            for i in range(256):
                self.oam[(self.oam_addr + i) & 0xFF] = page_data[i]
            self.oam_addr = (self.oam_addr + 256) & 0xFF

        def _pixel_from_bg(self, x, y):
            show_bg = (self.mask & 0x08) != 0
            show_left_bg = (self.mask & 0x02) != 0
            if (not show_bg) or (x < 8 and not show_left_bg):
                return 0, self.palette_ram[0] & 0x3F
            # Compose world coords from PPUCTRL base nametable + scroll registers.
            base_nt = self.ctrl & 0x03
            base_x = (base_nt & 1) * 256
            base_y = ((base_nt >> 1) & 1) * 240
            scx = ((self.v & 0x001F) << 3) | self.x
            scy = (((self.v >> 5) & 0x001F) << 3) | ((self.v >> 12) & 0x07)
            wx = base_x + scx + x
            wy = base_y + scy + y
            # Wrap horizontally over 2 NTs (512px) and vertically over 2 NTs (480px).
            wx_total = wx % 512
            wy_total = wy % 480
            nt_x = wx_total // 256
            nt_y = wy_total // 240
            nt_select = (nt_y << 1) | nt_x  # 0..3
            local_x = wx_total - nt_x * 256
            local_y = wy_total - nt_y * 240
            tile_x = local_x // 8
            tile_y = local_y // 8
            # Each nametable is 0x400 bytes starting at 0x2000+nt*0x400.
            nt_base_addr = 0x2000 + nt_select * 0x400
            tile = self._nt_read(nt_base_addr + tile_y * 32 + tile_x)
            attr = self._nt_read(nt_base_addr + 0x3C0 + (tile_y // 4) * 8 + (tile_x // 4))
            quadrant = ((tile_y & 0x02) << 1) | (tile_x & 0x02)
            pal_hi = (attr >> quadrant) & 0x03
            pattern_table = 0x1000 if (self.ctrl & 0x10) else 0x0000
            row = local_y & 0x07
            col = local_x & 0x07
            low = self.chr[(pattern_table + tile * 16 + row) & 0x1FFF]
            high = self.chr[(pattern_table + tile * 16 + row + 8) & 0x1FFF]
            bit = 7 - col
            pix = ((low >> bit) & 1) | (((high >> bit) & 1) << 1)
            if pix == 0:
                return 0, self.palette_ram[0] & 0x3F
            return pix, self.palette_ram[(pal_hi << 2) + pix] & 0x3F

        def _pixel_from_sprites(self, x, y, bg_non_zero):
            show_spr = (self.mask & 0x10) != 0
            show_left_spr = (self.mask & 0x04) != 0
            if (not show_spr) or (x < 8 and not show_left_spr):
                return None
            sprite_h = 16 if (self.ctrl & 0x20) else 8
            for i in range(64):
                o = i * 4
                sy = self.oam[o] + 1
                tile = self.oam[o + 1]
                attr = self.oam[o + 2]
                sx = self.oam[o + 3]
                if y < sy or y >= sy + sprite_h or x < sx or x >= sx + 8:
                    continue
                row = y - sy
                col = x - sx
                if attr & 0x80:
                    row = (sprite_h - 1) - row
                if attr & 0x40:
                    col = 7 - col
                if sprite_h == 16:
                    bank = (tile & 1) * 0x1000
                    tile_index = tile & 0xFE
                    if row >= 8:
                        tile_index += 1
                        row -= 8
                    base = bank + tile_index * 16
                else:
                    base = (0x1000 if (self.ctrl & 0x08) else 0x0000) + tile * 16
                low = self.chr[(base + row) & 0x1FFF]
                high = self.chr[(base + row + 8) & 0x1FFF]
                bit = 7 - col
                pix = ((low >> bit) & 1) | (((high >> bit) & 1) << 1)
                if pix == 0:
                    continue
                # Sprite 0 hit (ignoring some HW-quirk timing details).
                if i == 0 and bg_non_zero and x < 255:
                    self.status |= 0x40
                    self.sprite0_hit = True
                priority_behind_bg = (attr & 0x20) != 0
                if priority_behind_bg and bg_non_zero:
                    return None
                pal = 0x10 + ((attr & 0x03) << 2) + pix
                return self.palette_ram[self._palette_addr(0x3F00 + pal)] & 0x3F
            return None

        def render_frame(self):
            self.sprite0_hit = False
            # Clear sprite-0 hit (bit 6) and sprite-overflow (bit 5) at frame start.
            self.status &= ~0x60
            sprite_h = 16 if (self.ctrl & 0x20) else 8
            sprites_on_line = [0] * 240
            for i in range(64):
                o = i * 4
                sy = self.oam[o] + 1
                if sy >= 240:
                    continue
                y_end = min(240, sy + sprite_h)
                for ly in range(sy, y_end):
                    if 0 <= ly < 240:
                        sprites_on_line[ly] += 1
            for y in range(240):
                if sprites_on_line[y] > 8:
                    self.status |= 0x20  # sprite overflow latches once during the frame
                    break
            for y in range(240):
                row_base = y * 256
                for x in range(256):
                    bg_pix, bg_col = self._pixel_from_bg(x, y)
                    spr_col = self._pixel_from_sprites(x, y, bg_pix != 0)
                    self.framebuffer[row_base + x] = spr_col if spr_col is not None else bg_col
            return self.framebuffer

        def step(self, ppu_cycles):
            for _ in range(ppu_cycles):
                self.dot += 1
                if self.dot >= 341:
                    self.dot = 0
                    self.scanline += 1
                    if self.scanline == 241:
                        self.status |= 0x80
                        if self.ctrl & 0x80:
                            self.nmi_pending = True
                    elif self.scanline >= 262:
                        self.scanline = 0
                        self.frame += 1
                        # Clear VBlank + sprite hit + overflow at pre-render.
                        self.status &= 0x1F

    # Python fallback core with real CPU stepping for mapper 0 ROMs.
    class PyNesCore:
        def __init__(self):
            self.rom_loaded = False
            self.running = False
            self.memory = bytearray(0x10000)
            self.prg_banks = 0
            self.chr_banks = 0
            self.mapper = 0
            self.mirroring = 0
            self.pc = 0x0000
            self.a = 0
            self.x = 0
            self.y = 0
            self.sp = 0xFD
            self.status = 0x24  # IRQ disabled + unused bit
            self.cycles = 0
            self.frame_counter = 0
            self.last_instr = "RESET"
            self.executed_instr = 0
            self.ram = bytearray(0x800)
            self.prg_rom = b""
            self.chr_rom = b""
            self.chr_ram = bytearray(0x2000)
            self.debug_info = ""
            self.ppu = PyPpu()
            self.apu = PyApu()
            self.apu.cpu_read = self._read  # so DMC can fetch sample bytes
            self.prg_bank_16k = 0
            self.chr_bank_8k = 0
            # MMC1 state
            self.mmc1_shift = 0x10
            self.mmc1_ctrl = 0x0C
            self.mmc1_chr0 = 0
            self.mmc1_chr1 = 0
            self.mmc1_prg = 0
            # MMC3 (mapper 4) state
            self.mmc3_select = 0
            self.mmc3_banks = [0, 0, 0, 0, 0, 0, 0, 0]
            self.mmc3_prg_mode = 0
            self.mmc3_chr_mode = 0
            self.mmc3_irq_latch = 0
            self.mmc3_irq_counter = 0
            self.mmc3_irq_enable = False
            self.mmc3_irq_reload = False
            self.mmc3_irq_pending = False
            self.mmc3_prg_ram_enable = True
            # AxROM (mapper 7) state
            self.axrom_prg = 0
            # Battery / work RAM at $6000-$7FFF
            self.prg_ram = bytearray(0x2000)
            # Controllers
            self.controller1_state = 0
            self.controller1_shift = 0
            self.controller2_state = 0
            self.controller2_shift = 0
            self.controller_strobe = 0
            # Legacy field kept for backwards-compatible UI hooks.
            self.apu_activity = 0
            # CPU flags
            self.flag_c = 0x01
            self.flag_z = 0x02
            self.flag_i = 0x04
            self.flag_d = 0x08
            self.flag_b = 0x10
            self.flag_u = 0x20
            self.flag_v = 0x40
            self.flag_n = 0x80

        def _read_prg(self, addr):
            if not self.prg_rom:
                return self.memory[addr]
            if self.mapper == 0:
                if self.prg_banks == 1:
                    return self.prg_rom[(addr - 0x8000) & 0x3FFF]
                return self.prg_rom[(addr - 0x8000) & 0x7FFF]
            if self.mapper == 1:
                mode = (self.mmc1_ctrl >> 2) & 0x03
                bank = self.mmc1_prg & 0x0F
                if mode in (0, 1):
                    bank &= 0x0E
                    base = (bank * 0x4000) + ((addr - 0x8000) & 0x7FFF)
                    return self.prg_rom[base % len(self.prg_rom)]
                if mode == 2:
                    if addr < 0xC000:
                        return self.prg_rom[(addr - 0x8000) & 0x3FFF]
                    base = bank * 0x4000 + ((addr - 0xC000) & 0x3FFF)
                    return self.prg_rom[base % len(self.prg_rom)]
                if addr < 0xC000:
                    base = bank * 0x4000 + ((addr - 0x8000) & 0x3FFF)
                    return self.prg_rom[base % len(self.prg_rom)]
                last = max(0, self.prg_banks - 1) * 0x4000 + ((addr - 0xC000) & 0x3FFF)
                return self.prg_rom[last % len(self.prg_rom)]
            if self.mapper == 2:
                if addr < 0xC000:
                    base = (self.prg_bank_16k % max(1, self.prg_banks)) * 0x4000 + ((addr - 0x8000) & 0x3FFF)
                    return self.prg_rom[base % len(self.prg_rom)]
                base = max(0, self.prg_banks - 1) * 0x4000 + ((addr - 0xC000) & 0x3FFF)
                return self.prg_rom[base % len(self.prg_rom)]
            if self.mapper == 3:
                if self.prg_banks == 1:
                    return self.prg_rom[(addr - 0x8000) & 0x3FFF]
                return self.prg_rom[(addr - 0x8000) & 0x7FFF]
            if self.mapper == 4:
                # MMC3: 8KB PRG bank windows. R6 = $8000 or $C000 swap, R7 = $A000.
                # Last and second-to-last 8KB are fixed depending on prg_mode.
                total_8k = max(1, len(self.prg_rom) // 0x2000)
                last8 = total_8k - 1
                second_last8 = total_8k - 2
                r6 = self.mmc3_banks[6] % total_8k
                r7 = self.mmc3_banks[7] % total_8k
                window = (addr - 0x8000) // 0x2000
                if self.mmc3_prg_mode == 0:
                    bank = (r6, r7, second_last8, last8)[window]
                else:
                    bank = (second_last8, r7, r6, last8)[window]
                base = bank * 0x2000 + (addr & 0x1FFF)
                return self.prg_rom[base % len(self.prg_rom)]
            if self.mapper == 7:
                # AxROM: 32KB switchable PRG banks.
                total_32k = max(1, len(self.prg_rom) // 0x8000)
                bank = self.axrom_prg % total_32k
                base = bank * 0x8000 + (addr - 0x8000)
                return self.prg_rom[base % len(self.prg_rom)]
            return self.prg_rom[(addr - 0x8000) % len(self.prg_rom)]

        def _write_mapper(self, addr, value):
            if addr < 0x8000:
                return
            if self.mapper == 1:
                if value & 0x80:
                    self.mmc1_shift = 0x10
                    self.mmc1_ctrl |= 0x0C
                    return
                commit = self.mmc1_shift & 1
                self.mmc1_shift >>= 1
                self.mmc1_shift |= (value & 1) << 4
                if commit:
                    reg = (addr >> 13) & 0x03
                    data = self.mmc1_shift & 0x1F
                    if reg == 0:
                        self.mmc1_ctrl = data
                        m = data & 0x03
                        new_mirror = (2, 3, 0, 1)[m]
                        self.mirroring = new_mirror
                        if hasattr(self, "ppu"):
                            self.ppu.mirroring = new_mirror
                    elif reg == 1:
                        self.mmc1_chr0 = data
                    elif reg == 2:
                        self.mmc1_chr1 = data
                    else:
                        self.mmc1_prg = data
                    self.mmc1_shift = 0x10
                    self._apply_chr_bank()
            elif self.mapper == 2:
                self.prg_bank_16k = value & 0x0F
            elif self.mapper == 3:
                self.chr_bank_8k = value & 0x03
                self._apply_chr_bank()
            elif self.mapper == 4:
                # MMC3 register space split by even/odd address in 4 ranges.
                if 0x8000 <= addr <= 0x9FFF:
                    if (addr & 1) == 0:  # bank select ($8000)
                        self.mmc3_select = value & 0x07
                        self.mmc3_prg_mode = (value >> 6) & 1
                        self.mmc3_chr_mode = (value >> 7) & 1
                    else:               # bank data ($8001)
                        self.mmc3_banks[self.mmc3_select] = value
                        self._apply_chr_bank()
                elif 0xA000 <= addr <= 0xBFFF:
                    if (addr & 1) == 0:  # mirroring
                        new_mirror = 0 if (value & 1) == 0 else 1  # vert vs horiz
                        # Don't override 4-screen carts.
                        if self.mirroring != 4:
                            self.mirroring = new_mirror
                            self.ppu.mirroring = new_mirror
                    else:
                        self.mmc3_prg_ram_enable = (value & 0x80) != 0
                elif 0xC000 <= addr <= 0xDFFF:
                    if (addr & 1) == 0:
                        self.mmc3_irq_latch = value
                    else:
                        self.mmc3_irq_counter = 0
                        self.mmc3_irq_reload = True
                elif 0xE000 <= addr <= 0xFFFF:
                    if (addr & 1) == 0:
                        self.mmc3_irq_enable = False
                        self.mmc3_irq_pending = False
                    else:
                        self.mmc3_irq_enable = True
            elif self.mapper == 7:
                # AxROM: bits 0-2 select 32KB PRG bank, bit 4 selects single-screen NT.
                self.axrom_prg = value & 0x07
                ss_bank = 1 if (value & 0x10) else 0
                new_mirror = 3 if ss_bank else 2  # single-screen upper / lower
                self.mirroring = new_mirror
                self.ppu.mirroring = new_mirror

        def _apply_chr_bank(self):
            if self.chr_banks == 0:
                self.ppu.chr[:] = self.chr_ram
                return
            if self.mapper == 1:
                chr_mode_4k = (self.mmc1_ctrl & 0x10) != 0
                if chr_mode_4k:
                    b0 = (self.mmc1_chr0 % max(1, self.chr_banks * 2)) * 0x1000
                    b1 = (self.mmc1_chr1 % max(1, self.chr_banks * 2)) * 0x1000
                    self.ppu.chr[0:0x1000] = self.chr_rom[b0:b0 + 0x1000]
                    self.ppu.chr[0x1000:0x2000] = self.chr_rom[b1:b1 + 0x1000]
                else:
                    bank = (self.mmc1_chr0 & 0x1E) % max(1, self.chr_banks)
                    base = bank * 0x2000
                    self.ppu.chr[:] = self.chr_rom[base:base + 0x2000]
            elif self.mapper == 3:
                bank = self.chr_bank_8k % max(1, self.chr_banks)
                base = bank * 0x2000
                self.ppu.chr[:] = self.chr_rom[base:base + 0x2000]
            elif self.mapper == 4:
                # MMC3 CHR layout: R0/R1 = 2KB, R2-R5 = 1KB. CHR mode swaps halves.
                total_1k = max(1, len(self.chr_rom) // 0x400)
                r = [b % total_1k for b in self.mmc3_banks]
                # In CHR mode 0: R0/R1 cover $0000-$0FFF (2KB each), R2-R5 cover $1000-$1FFF.
                # In CHR mode 1: swapped.
                if self.mmc3_chr_mode == 0:
                    layout = [r[0] & ~1, (r[0] & ~1) | 1, r[1] & ~1, (r[1] & ~1) | 1,
                              r[2], r[3], r[4], r[5]]
                else:
                    layout = [r[2], r[3], r[4], r[5],
                              r[0] & ~1, (r[0] & ~1) | 1, r[1] & ~1, (r[1] & ~1) | 1]
                for slot, bank in enumerate(layout):
                    base = bank * 0x400
                    self.ppu.chr[slot * 0x400:(slot + 1) * 0x400] = self.chr_rom[base:base + 0x400]
            elif self.mapper == 7:
                # AxROM uses CHR RAM exclusively in real carts.
                self.ppu.chr[:] = self.chr_ram
            else:
                self.ppu.load_chr(self.chr_rom)

        def _read(self, addr):
            addr &= 0xFFFF
            if addr < 0x2000:
                return self.ram[addr & 0x07FF]
            if 0x2000 <= addr < 0x4000:
                return self.ppu.read_register(addr & 0x07)
            if addr == 0x4015:
                return self.apu.read_status()
            if addr == 0x4016:
                value = self.controller1_shift & 1
                if not self.controller_strobe:
                    self.controller1_shift = ((self.controller1_shift >> 1) | 0x80) & 0xFF
                return value
            if addr == 0x4017:
                value = self.controller2_shift & 1
                if not self.controller_strobe:
                    self.controller2_shift = ((self.controller2_shift >> 1) | 0x80) & 0xFF
                return value
            if 0x4000 <= addr < 0x4020:
                return 0  # open bus / unused APU read range
            if 0x6000 <= addr < 0x8000:
                return self.prg_ram[addr - 0x6000]
            if 0x8000 <= addr <= 0xFFFF and self.prg_rom:
                return self._read_prg(addr)
            return self.memory[addr]

        def _write(self, addr, value):
            addr &= 0xFFFF
            value &= 0xFF
            if addr < 0x2000:
                self.ram[addr & 0x07FF] = value
                return
            if 0x2000 <= addr < 0x4000:
                self.ppu.write_register(addr & 0x07, value)
                return
            if addr == 0x4014:
                page_base = value << 8
                page_data = [self._read((page_base + i) & 0xFFFF) for i in range(256)]
                self.ppu.oam_dma(page_data)
                self.cycles += 513 + (self.cycles & 1)
                return
            if addr == 0x4016:
                # Strobe latches both controllers.
                self.controller_strobe = value & 1
                if self.controller_strobe:
                    self.controller1_shift = self.controller1_state & 0xFF
                    self.controller2_shift = self.controller2_state & 0xFF
                return
            if addr == 0x4017:
                # $4017 serves dual purpose: APU frame counter on writes.
                self.apu.write_register(0x4017, value)
                self.apu_activity = (self.apu_activity + 1) & 0xFFFF
                return
            if addr == 0x4015:
                self.apu.write_register(0x4015, value)
                self.apu_activity = (self.apu_activity + 1) & 0xFFFF
                return
            if 0x4000 <= addr <= 0x4013:
                self.apu.write_register(addr, value)
                self.apu_activity = (self.apu_activity + 1) & 0xFFFF
                return
            if 0x6000 <= addr < 0x8000:
                self.prg_ram[addr - 0x6000] = value
                return
            if addr >= 0x8000:
                self._write_mapper(addr, value)
                return
            self.memory[addr] = value

        def _read16(self, addr):
            lo = self._read(addr)
            hi = self._read((addr + 1) & 0xFFFF)
            return lo | (hi << 8)

        def _read16_bug(self, addr):
            lo = self._read(addr)
            hi = self._read((addr & 0xFF00) | ((addr + 1) & 0x00FF))
            return lo | (hi << 8)

        def _set_flag(self, flag, cond):
            if cond:
                self.status |= flag
            else:
                self.status &= (~flag) & 0xFF

        def _get_flag(self, flag):
            return 1 if (self.status & flag) else 0

        def _set_zn(self, value):
            self._set_flag(self.flag_z, (value & 0xFF) == 0)
            self._set_flag(self.flag_n, (value & 0x80) != 0)

        def _push(self, value):
            self._write(0x0100 + self.sp, value)
            self.sp = (self.sp - 1) & 0xFF

        def _pull(self):
            self.sp = (self.sp + 1) & 0xFF
            return self._read(0x0100 + self.sp)

        def _imm(self):
            addr = self.pc
            self.pc = (self.pc + 1) & 0xFFFF
            return addr

        def _zp(self):
            addr = self._read(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            return addr

        def _zpx(self):
            addr = (self._read(self.pc) + self.x) & 0xFF
            self.pc = (self.pc + 1) & 0xFFFF
            return addr

        def _zpy(self):
            addr = (self._read(self.pc) + self.y) & 0xFF
            self.pc = (self.pc + 1) & 0xFFFF
            return addr

        def _abs(self):
            addr = self._read16(self.pc)
            self.pc = (self.pc + 2) & 0xFFFF
            return addr

        def _abx(self, extra_cycle=False):
            base = self._read16(self.pc)
            self.pc = (self.pc + 2) & 0xFFFF
            addr = (base + self.x) & 0xFFFF
            if extra_cycle and (base & 0xFF00) != (addr & 0xFF00):
                self.cycles += 1
            return addr

        def _aby(self, extra_cycle=False):
            base = self._read16(self.pc)
            self.pc = (self.pc + 2) & 0xFFFF
            addr = (base + self.y) & 0xFFFF
            if extra_cycle and (base & 0xFF00) != (addr & 0xFF00):
                self.cycles += 1
            return addr

        def _ind(self):
            ptr = self._read16(self.pc)
            self.pc = (self.pc + 2) & 0xFFFF
            return self._read16_bug(ptr)

        def _izx(self):
            zp = (self._read(self.pc) + self.x) & 0xFF
            self.pc = (self.pc + 1) & 0xFFFF
            lo = self._read(zp)
            hi = self._read((zp + 1) & 0xFF)
            return lo | (hi << 8)

        def _izy(self, extra_cycle=False):
            zp = self._read(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            base = self._read(zp) | (self._read((zp + 1) & 0xFF) << 8)
            addr = (base + self.y) & 0xFFFF
            if extra_cycle and (base & 0xFF00) != (addr & 0xFF00):
                self.cycles += 1
            return addr

        def _branch(self, cond):
            offset = self._read(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            if cond:
                self.cycles += 1
                old_pc = self.pc
                if offset & 0x80:
                    offset -= 0x100
                self.pc = (self.pc + offset) & 0xFFFF
                if (old_pc & 0xFF00) != (self.pc & 0xFF00):
                    self.cycles += 1

        def _adc(self, value):
            carry = self._get_flag(self.flag_c)
            result = self.a + value + carry
            self._set_flag(self.flag_c, result > 0xFF)
            self._set_flag(
                self.flag_v,
                (~(self.a ^ value) & (self.a ^ result) & 0x80) != 0,
            )
            self.a = result & 0xFF
            self._set_zn(self.a)

        def _sbc(self, value):
            self._adc(value ^ 0xFF)

        def _cmp(self, reg, value):
            tmp = (reg - value) & 0x1FF
            self._set_flag(self.flag_c, reg >= value)
            self._set_flag(self.flag_z, (tmp & 0xFF) == 0)
            self._set_flag(self.flag_n, (tmp & 0x80) != 0)

        def _fetch(self, addr):
            return self._read(addr & 0xFFFF)

        def _execute_instruction(self):
            if self.ppu.nmi_pending:
                self.ppu.nmi_pending = False
                self._service_nmi()
            elif (not self._get_flag(self.flag_i)) and (self.apu.irq_pending or self.mmc3_irq_pending):
                self._service_irq()
            opcode_addr = self.pc
            opcode = self._read(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            base_cycles = 2
            mnemonic = f"OP${opcode:02X}"

            if opcode == 0xEA:  # NOP
                base_cycles = 2
                mnemonic = "NOP"
            elif opcode == 0xA9:  # LDA #imm
                self.a = self._fetch(self._imm())
                self._set_zn(self.a)
                base_cycles = 2
                mnemonic = "LDA #"
            elif opcode == 0xA5:
                self.a = self._fetch(self._zp())
                self._set_zn(self.a)
                base_cycles = 3
                mnemonic = "LDA zp"
            elif opcode == 0xB5:
                self.a = self._fetch(self._zpx())
                self._set_zn(self.a)
                base_cycles = 4
                mnemonic = "LDA zpx"
            elif opcode == 0xAD:
                self.a = self._fetch(self._abs())
                self._set_zn(self.a)
                base_cycles = 4
                mnemonic = "LDA abs"
            elif opcode == 0xBD:
                self.a = self._fetch(self._abx(extra_cycle=True))
                self._set_zn(self.a)
                base_cycles = 4
                mnemonic = "LDA abx"
            elif opcode == 0xB9:
                self.a = self._fetch(self._aby(extra_cycle=True))
                self._set_zn(self.a)
                base_cycles = 4
                mnemonic = "LDA aby"
            elif opcode == 0xA1:
                self.a = self._fetch(self._izx())
                self._set_zn(self.a)
                base_cycles = 6
                mnemonic = "LDA izx"
            elif opcode == 0xB1:
                self.a = self._fetch(self._izy(extra_cycle=True))
                self._set_zn(self.a)
                base_cycles = 5
                mnemonic = "LDA izy"
            elif opcode == 0xA2:  # LDX
                self.x = self._fetch(self._imm())
                self._set_zn(self.x)
                base_cycles = 2
                mnemonic = "LDX #"
            elif opcode == 0xA0:  # LDY
                self.y = self._fetch(self._imm())
                self._set_zn(self.y)
                base_cycles = 2
                mnemonic = "LDY #"
            elif opcode == 0x85:  # STA
                self._write(self._zp(), self.a)
                base_cycles = 3
                mnemonic = "STA zp"
            elif opcode == 0x95:
                self._write(self._zpx(), self.a)
                base_cycles = 4
                mnemonic = "STA zpx"
            elif opcode == 0x8D:
                self._write(self._abs(), self.a)
                base_cycles = 4
                mnemonic = "STA abs"
            elif opcode == 0x9D:
                self._write(self._abx(), self.a)
                base_cycles = 5
                mnemonic = "STA abx"
            elif opcode == 0x99:
                self._write(self._aby(), self.a)
                base_cycles = 5
                mnemonic = "STA aby"
            elif opcode == 0x81:
                self._write(self._izx(), self.a)
                base_cycles = 6
                mnemonic = "STA izx"
            elif opcode == 0x91:
                self._write(self._izy(), self.a)
                base_cycles = 6
                mnemonic = "STA izy"
            elif opcode == 0xAA:  # TAX
                self.x = self.a
                self._set_zn(self.x)
                base_cycles = 2
                mnemonic = "TAX"
            elif opcode == 0xA8:  # TAY
                self.y = self.a
                self._set_zn(self.y)
                base_cycles = 2
                mnemonic = "TAY"
            elif opcode == 0x8A:  # TXA
                self.a = self.x
                self._set_zn(self.a)
                base_cycles = 2
                mnemonic = "TXA"
            elif opcode == 0x98:  # TYA
                self.a = self.y
                self._set_zn(self.a)
                base_cycles = 2
                mnemonic = "TYA"
            elif opcode == 0xE8:  # INX
                self.x = (self.x + 1) & 0xFF
                self._set_zn(self.x)
                base_cycles = 2
                mnemonic = "INX"
            elif opcode == 0xC8:  # INY
                self.y = (self.y + 1) & 0xFF
                self._set_zn(self.y)
                base_cycles = 2
                mnemonic = "INY"
            elif opcode == 0xCA:  # DEX
                self.x = (self.x - 1) & 0xFF
                self._set_zn(self.x)
                base_cycles = 2
                mnemonic = "DEX"
            elif opcode == 0x88:  # DEY
                self.y = (self.y - 1) & 0xFF
                self._set_zn(self.y)
                base_cycles = 2
                mnemonic = "DEY"
            elif opcode == 0x69:  # ADC #
                self._adc(self._fetch(self._imm()))
                base_cycles = 2
                mnemonic = "ADC #"
            elif opcode == 0xE9:  # SBC #
                self._sbc(self._fetch(self._imm()))
                base_cycles = 2
                mnemonic = "SBC #"
            elif opcode == 0x29:  # AND #
                self.a &= self._fetch(self._imm())
                self._set_zn(self.a)
                base_cycles = 2
                mnemonic = "AND #"
            elif opcode == 0x09:  # ORA #
                self.a |= self._fetch(self._imm())
                self._set_zn(self.a)
                base_cycles = 2
                mnemonic = "ORA #"
            elif opcode == 0x49:  # EOR #
                self.a ^= self._fetch(self._imm())
                self._set_zn(self.a)
                base_cycles = 2
                mnemonic = "EOR #"
            elif opcode == 0x4C:  # JMP abs
                self.pc = self._abs()
                base_cycles = 3
                mnemonic = "JMP abs"
            elif opcode == 0x6C:  # JMP ind
                self.pc = self._ind()
                base_cycles = 5
                mnemonic = "JMP ind"
            elif opcode == 0x20:  # JSR
                target = self._abs()
                ret = (self.pc - 1) & 0xFFFF
                self._push((ret >> 8) & 0xFF)
                self._push(ret & 0xFF)
                self.pc = target
                base_cycles = 6
                mnemonic = "JSR"
            elif opcode == 0x60:  # RTS
                lo = self._pull()
                hi = self._pull()
                self.pc = ((hi << 8) | lo) + 1
                self.pc &= 0xFFFF
                base_cycles = 6
                mnemonic = "RTS"
            elif opcode == 0x00:  # BRK
                self.pc = (self.pc + 1) & 0xFFFF
                self._push((self.pc >> 8) & 0xFF)
                self._push(self.pc & 0xFF)
                self._push(self.status | self.flag_b | self.flag_u)
                self._set_flag(self.flag_i, True)
                self.pc = self._read16(0xFFFE)
                base_cycles = 7
                mnemonic = "BRK"
            elif opcode == 0x40:  # RTI
                self.status = (self._pull() | self.flag_u) & (~self.flag_b & 0xFF)
                lo = self._pull()
                hi = self._pull()
                self.pc = lo | (hi << 8)
                base_cycles = 6
                mnemonic = "RTI"
            elif opcode == 0x18:  # CLC
                self._set_flag(self.flag_c, False)
                base_cycles = 2
                mnemonic = "CLC"
            elif opcode == 0x38:  # SEC
                self._set_flag(self.flag_c, True)
                base_cycles = 2
                mnemonic = "SEC"
            elif opcode == 0x58:  # CLI
                self._set_flag(self.flag_i, False)
                base_cycles = 2
                mnemonic = "CLI"
            elif opcode == 0x78:  # SEI
                self._set_flag(self.flag_i, True)
                base_cycles = 2
                mnemonic = "SEI"
            elif opcode == 0xD8:  # CLD
                self._set_flag(self.flag_d, False)
                base_cycles = 2
                mnemonic = "CLD"
            elif opcode == 0xF8:  # SED
                self._set_flag(self.flag_d, True)
                base_cycles = 2
                mnemonic = "SED"
            elif opcode == 0xB8:  # CLV
                self._set_flag(self.flag_v, False)
                base_cycles = 2
                mnemonic = "CLV"
            elif opcode == 0xC9:  # CMP #
                self._cmp(self.a, self._fetch(self._imm()))
                base_cycles = 2
                mnemonic = "CMP #"
            elif opcode == 0xE0:  # CPX #
                self._cmp(self.x, self._fetch(self._imm()))
                base_cycles = 2
                mnemonic = "CPX #"
            elif opcode == 0xC0:  # CPY #
                self._cmp(self.y, self._fetch(self._imm()))
                base_cycles = 2
                mnemonic = "CPY #"
            elif opcode == 0xD0:  # BNE
                self._branch(self._get_flag(self.flag_z) == 0)
                base_cycles = 2
                mnemonic = "BNE"
            elif opcode == 0xF0:  # BEQ
                self._branch(self._get_flag(self.flag_z) == 1)
                base_cycles = 2
                mnemonic = "BEQ"
            elif opcode == 0x10:  # BPL
                self._branch(self._get_flag(self.flag_n) == 0)
                base_cycles = 2
                mnemonic = "BPL"
            elif opcode == 0x30:  # BMI
                self._branch(self._get_flag(self.flag_n) == 1)
                base_cycles = 2
                mnemonic = "BMI"
            elif opcode == 0x90:  # BCC
                self._branch(self._get_flag(self.flag_c) == 0)
                base_cycles = 2
                mnemonic = "BCC"
            elif opcode == 0xB0:  # BCS
                self._branch(self._get_flag(self.flag_c) == 1)
                base_cycles = 2
                mnemonic = "BCS"
            elif opcode == 0x50:  # BVC
                self._branch(self._get_flag(self.flag_v) == 0)
                base_cycles = 2
                mnemonic = "BVC"
            elif opcode == 0x70:  # BVS
                self._branch(self._get_flag(self.flag_v) == 1)
                base_cycles = 2
                mnemonic = "BVS"
            elif opcode == 0x48:  # PHA
                self._push(self.a)
                base_cycles = 3
                mnemonic = "PHA"
            elif opcode == 0x68:  # PLA
                self.a = self._pull()
                self._set_zn(self.a)
                base_cycles = 4
                mnemonic = "PLA"
            elif opcode == 0x08:  # PHP
                self._push(self.status | self.flag_b | self.flag_u)
                base_cycles = 3
                mnemonic = "PHP"
            elif opcode == 0x28:  # PLP
                self.status = (self._pull() | self.flag_u) & (~self.flag_b & 0xFF)
                base_cycles = 4
                mnemonic = "PLP"
            # ---- Stack pointer transfers ---------------------------------
            elif opcode == 0xBA:  # TSX
                self.x = self.sp
                self._set_zn(self.x)
                base_cycles = 2
                mnemonic = "TSX"
            elif opcode == 0x9A:  # TXS
                self.sp = self.x
                base_cycles = 2
                mnemonic = "TXS"
            # ---- BIT -----------------------------------------------------
            elif opcode == 0x24:  # BIT zp
                v = self._fetch(self._zp())
                self._set_flag(self.flag_z, (self.a & v) == 0)
                self._set_flag(self.flag_v, (v & 0x40) != 0)
                self._set_flag(self.flag_n, (v & 0x80) != 0)
                base_cycles = 3
                mnemonic = "BIT zp"
            elif opcode == 0x2C:  # BIT abs
                v = self._fetch(self._abs())
                self._set_flag(self.flag_z, (self.a & v) == 0)
                self._set_flag(self.flag_v, (v & 0x40) != 0)
                self._set_flag(self.flag_n, (v & 0x80) != 0)
                base_cycles = 4
                mnemonic = "BIT abs"
            # ---- Extra addressing modes for ADC/SBC/AND/ORA/EOR/CMP -----
            elif opcode == 0x65:  # ADC zp
                self._adc(self._fetch(self._zp())); base_cycles = 3; mnemonic = "ADC zp"
            elif opcode == 0x75:
                self._adc(self._fetch(self._zpx())); base_cycles = 4; mnemonic = "ADC zpx"
            elif opcode == 0x6D:
                self._adc(self._fetch(self._abs())); base_cycles = 4; mnemonic = "ADC abs"
            elif opcode == 0x7D:
                self._adc(self._fetch(self._abx(extra_cycle=True))); base_cycles = 4; mnemonic = "ADC abx"
            elif opcode == 0x79:
                self._adc(self._fetch(self._aby(extra_cycle=True))); base_cycles = 4; mnemonic = "ADC aby"
            elif opcode == 0x61:
                self._adc(self._fetch(self._izx())); base_cycles = 6; mnemonic = "ADC izx"
            elif opcode == 0x71:
                self._adc(self._fetch(self._izy(extra_cycle=True))); base_cycles = 5; mnemonic = "ADC izy"
            elif opcode == 0xE5:  # SBC zp
                self._sbc(self._fetch(self._zp())); base_cycles = 3; mnemonic = "SBC zp"
            elif opcode == 0xF5:
                self._sbc(self._fetch(self._zpx())); base_cycles = 4; mnemonic = "SBC zpx"
            elif opcode == 0xED:
                self._sbc(self._fetch(self._abs())); base_cycles = 4; mnemonic = "SBC abs"
            elif opcode == 0xFD:
                self._sbc(self._fetch(self._abx(extra_cycle=True))); base_cycles = 4; mnemonic = "SBC abx"
            elif opcode == 0xF9:
                self._sbc(self._fetch(self._aby(extra_cycle=True))); base_cycles = 4; mnemonic = "SBC aby"
            elif opcode == 0xE1:
                self._sbc(self._fetch(self._izx())); base_cycles = 6; mnemonic = "SBC izx"
            elif opcode == 0xF1:
                self._sbc(self._fetch(self._izy(extra_cycle=True))); base_cycles = 5; mnemonic = "SBC izy"
            elif opcode == 0x25:  # AND zp
                self.a &= self._fetch(self._zp()); self._set_zn(self.a); base_cycles = 3; mnemonic = "AND zp"
            elif opcode == 0x35:
                self.a &= self._fetch(self._zpx()); self._set_zn(self.a); base_cycles = 4; mnemonic = "AND zpx"
            elif opcode == 0x2D:
                self.a &= self._fetch(self._abs()); self._set_zn(self.a); base_cycles = 4; mnemonic = "AND abs"
            elif opcode == 0x3D:
                self.a &= self._fetch(self._abx(extra_cycle=True)); self._set_zn(self.a); base_cycles = 4; mnemonic = "AND abx"
            elif opcode == 0x39:
                self.a &= self._fetch(self._aby(extra_cycle=True)); self._set_zn(self.a); base_cycles = 4; mnemonic = "AND aby"
            elif opcode == 0x21:
                self.a &= self._fetch(self._izx()); self._set_zn(self.a); base_cycles = 6; mnemonic = "AND izx"
            elif opcode == 0x31:
                self.a &= self._fetch(self._izy(extra_cycle=True)); self._set_zn(self.a); base_cycles = 5; mnemonic = "AND izy"
            elif opcode == 0x05:  # ORA zp
                self.a |= self._fetch(self._zp()); self._set_zn(self.a); base_cycles = 3; mnemonic = "ORA zp"
            elif opcode == 0x15:
                self.a |= self._fetch(self._zpx()); self._set_zn(self.a); base_cycles = 4; mnemonic = "ORA zpx"
            elif opcode == 0x0D:
                self.a |= self._fetch(self._abs()); self._set_zn(self.a); base_cycles = 4; mnemonic = "ORA abs"
            elif opcode == 0x1D:
                self.a |= self._fetch(self._abx(extra_cycle=True)); self._set_zn(self.a); base_cycles = 4; mnemonic = "ORA abx"
            elif opcode == 0x19:
                self.a |= self._fetch(self._aby(extra_cycle=True)); self._set_zn(self.a); base_cycles = 4; mnemonic = "ORA aby"
            elif opcode == 0x01:
                self.a |= self._fetch(self._izx()); self._set_zn(self.a); base_cycles = 6; mnemonic = "ORA izx"
            elif opcode == 0x11:
                self.a |= self._fetch(self._izy(extra_cycle=True)); self._set_zn(self.a); base_cycles = 5; mnemonic = "ORA izy"
            elif opcode == 0x45:  # EOR zp
                self.a ^= self._fetch(self._zp()); self._set_zn(self.a); base_cycles = 3; mnemonic = "EOR zp"
            elif opcode == 0x55:
                self.a ^= self._fetch(self._zpx()); self._set_zn(self.a); base_cycles = 4; mnemonic = "EOR zpx"
            elif opcode == 0x4D:
                self.a ^= self._fetch(self._abs()); self._set_zn(self.a); base_cycles = 4; mnemonic = "EOR abs"
            elif opcode == 0x5D:
                self.a ^= self._fetch(self._abx(extra_cycle=True)); self._set_zn(self.a); base_cycles = 4; mnemonic = "EOR abx"
            elif opcode == 0x59:
                self.a ^= self._fetch(self._aby(extra_cycle=True)); self._set_zn(self.a); base_cycles = 4; mnemonic = "EOR aby"
            elif opcode == 0x41:
                self.a ^= self._fetch(self._izx()); self._set_zn(self.a); base_cycles = 6; mnemonic = "EOR izx"
            elif opcode == 0x51:
                self.a ^= self._fetch(self._izy(extra_cycle=True)); self._set_zn(self.a); base_cycles = 5; mnemonic = "EOR izy"
            elif opcode == 0xC5:  # CMP zp..izy
                self._cmp(self.a, self._fetch(self._zp())); base_cycles = 3; mnemonic = "CMP zp"
            elif opcode == 0xD5:
                self._cmp(self.a, self._fetch(self._zpx())); base_cycles = 4; mnemonic = "CMP zpx"
            elif opcode == 0xCD:
                self._cmp(self.a, self._fetch(self._abs())); base_cycles = 4; mnemonic = "CMP abs"
            elif opcode == 0xDD:
                self._cmp(self.a, self._fetch(self._abx(extra_cycle=True))); base_cycles = 4; mnemonic = "CMP abx"
            elif opcode == 0xD9:
                self._cmp(self.a, self._fetch(self._aby(extra_cycle=True))); base_cycles = 4; mnemonic = "CMP aby"
            elif opcode == 0xC1:
                self._cmp(self.a, self._fetch(self._izx())); base_cycles = 6; mnemonic = "CMP izx"
            elif opcode == 0xD1:
                self._cmp(self.a, self._fetch(self._izy(extra_cycle=True))); base_cycles = 5; mnemonic = "CMP izy"
            elif opcode == 0xE4:  # CPX zp/abs
                self._cmp(self.x, self._fetch(self._zp())); base_cycles = 3; mnemonic = "CPX zp"
            elif opcode == 0xEC:
                self._cmp(self.x, self._fetch(self._abs())); base_cycles = 4; mnemonic = "CPX abs"
            elif opcode == 0xC4:  # CPY zp/abs
                self._cmp(self.y, self._fetch(self._zp())); base_cycles = 3; mnemonic = "CPY zp"
            elif opcode == 0xCC:
                self._cmp(self.y, self._fetch(self._abs())); base_cycles = 4; mnemonic = "CPY abs"
            # ---- Extra LDX/LDY/STX/STY addressing modes -----------------
            elif opcode == 0xA6:
                self.x = self._fetch(self._zp()); self._set_zn(self.x); base_cycles = 3; mnemonic = "LDX zp"
            elif opcode == 0xB6:
                self.x = self._fetch(self._zpy()); self._set_zn(self.x); base_cycles = 4; mnemonic = "LDX zpy"
            elif opcode == 0xAE:
                self.x = self._fetch(self._abs()); self._set_zn(self.x); base_cycles = 4; mnemonic = "LDX abs"
            elif opcode == 0xBE:
                self.x = self._fetch(self._aby(extra_cycle=True)); self._set_zn(self.x); base_cycles = 4; mnemonic = "LDX aby"
            elif opcode == 0xA4:
                self.y = self._fetch(self._zp()); self._set_zn(self.y); base_cycles = 3; mnemonic = "LDY zp"
            elif opcode == 0xB4:
                self.y = self._fetch(self._zpx()); self._set_zn(self.y); base_cycles = 4; mnemonic = "LDY zpx"
            elif opcode == 0xAC:
                self.y = self._fetch(self._abs()); self._set_zn(self.y); base_cycles = 4; mnemonic = "LDY abs"
            elif opcode == 0xBC:
                self.y = self._fetch(self._abx(extra_cycle=True)); self._set_zn(self.y); base_cycles = 4; mnemonic = "LDY abx"
            elif opcode == 0x86:
                self._write(self._zp(), self.x); base_cycles = 3; mnemonic = "STX zp"
            elif opcode == 0x96:
                self._write(self._zpy(), self.x); base_cycles = 4; mnemonic = "STX zpy"
            elif opcode == 0x8E:
                self._write(self._abs(), self.x); base_cycles = 4; mnemonic = "STX abs"
            elif opcode == 0x84:
                self._write(self._zp(), self.y); base_cycles = 3; mnemonic = "STY zp"
            elif opcode == 0x94:
                self._write(self._zpx(), self.y); base_cycles = 4; mnemonic = "STY zpx"
            elif opcode == 0x8C:
                self._write(self._abs(), self.y); base_cycles = 4; mnemonic = "STY abs"
            # ---- Memory INC/DEC -----------------------------------------
            elif opcode == 0xE6:  # INC zp
                a = self._zp(); v = (self._fetch(a) + 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 5; mnemonic = "INC zp"
            elif opcode == 0xF6:
                a = self._zpx(); v = (self._fetch(a) + 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 6; mnemonic = "INC zpx"
            elif opcode == 0xEE:
                a = self._abs(); v = (self._fetch(a) + 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 6; mnemonic = "INC abs"
            elif opcode == 0xFE:
                a = self._abx(); v = (self._fetch(a) + 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 7; mnemonic = "INC abx"
            elif opcode == 0xC6:
                a = self._zp(); v = (self._fetch(a) - 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 5; mnemonic = "DEC zp"
            elif opcode == 0xD6:
                a = self._zpx(); v = (self._fetch(a) - 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 6; mnemonic = "DEC zpx"
            elif opcode == 0xCE:
                a = self._abs(); v = (self._fetch(a) - 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 6; mnemonic = "DEC abs"
            elif opcode == 0xDE:
                a = self._abx(); v = (self._fetch(a) - 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 7; mnemonic = "DEC abx"
            # ---- Shifts and rotates -------------------------------------
            elif opcode == 0x0A:  # ASL A
                self._set_flag(self.flag_c, (self.a & 0x80) != 0)
                self.a = (self.a << 1) & 0xFF; self._set_zn(self.a); base_cycles = 2; mnemonic = "ASL A"
            elif opcode == 0x06:
                a = self._zp(); v = self._fetch(a); self._set_flag(self.flag_c, (v & 0x80) != 0)
                v = (v << 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 5; mnemonic = "ASL zp"
            elif opcode == 0x16:
                a = self._zpx(); v = self._fetch(a); self._set_flag(self.flag_c, (v & 0x80) != 0)
                v = (v << 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 6; mnemonic = "ASL zpx"
            elif opcode == 0x0E:
                a = self._abs(); v = self._fetch(a); self._set_flag(self.flag_c, (v & 0x80) != 0)
                v = (v << 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 6; mnemonic = "ASL abs"
            elif opcode == 0x1E:
                a = self._abx(); v = self._fetch(a); self._set_flag(self.flag_c, (v & 0x80) != 0)
                v = (v << 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 7; mnemonic = "ASL abx"
            elif opcode == 0x4A:  # LSR A
                self._set_flag(self.flag_c, (self.a & 0x01) != 0)
                self.a = (self.a >> 1) & 0xFF; self._set_zn(self.a); base_cycles = 2; mnemonic = "LSR A"
            elif opcode == 0x46:
                a = self._zp(); v = self._fetch(a); self._set_flag(self.flag_c, (v & 0x01) != 0)
                v = (v >> 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 5; mnemonic = "LSR zp"
            elif opcode == 0x56:
                a = self._zpx(); v = self._fetch(a); self._set_flag(self.flag_c, (v & 0x01) != 0)
                v = (v >> 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 6; mnemonic = "LSR zpx"
            elif opcode == 0x4E:
                a = self._abs(); v = self._fetch(a); self._set_flag(self.flag_c, (v & 0x01) != 0)
                v = (v >> 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 6; mnemonic = "LSR abs"
            elif opcode == 0x5E:
                a = self._abx(); v = self._fetch(a); self._set_flag(self.flag_c, (v & 0x01) != 0)
                v = (v >> 1) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 7; mnemonic = "LSR abx"
            elif opcode == 0x2A:  # ROL A
                carry_in = self._get_flag(self.flag_c)
                self._set_flag(self.flag_c, (self.a & 0x80) != 0)
                self.a = ((self.a << 1) | carry_in) & 0xFF; self._set_zn(self.a); base_cycles = 2; mnemonic = "ROL A"
            elif opcode == 0x26:
                a = self._zp(); v = self._fetch(a); ci = self._get_flag(self.flag_c)
                self._set_flag(self.flag_c, (v & 0x80) != 0)
                v = ((v << 1) | ci) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 5; mnemonic = "ROL zp"
            elif opcode == 0x36:
                a = self._zpx(); v = self._fetch(a); ci = self._get_flag(self.flag_c)
                self._set_flag(self.flag_c, (v & 0x80) != 0)
                v = ((v << 1) | ci) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 6; mnemonic = "ROL zpx"
            elif opcode == 0x2E:
                a = self._abs(); v = self._fetch(a); ci = self._get_flag(self.flag_c)
                self._set_flag(self.flag_c, (v & 0x80) != 0)
                v = ((v << 1) | ci) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 6; mnemonic = "ROL abs"
            elif opcode == 0x3E:
                a = self._abx(); v = self._fetch(a); ci = self._get_flag(self.flag_c)
                self._set_flag(self.flag_c, (v & 0x80) != 0)
                v = ((v << 1) | ci) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 7; mnemonic = "ROL abx"
            elif opcode == 0x6A:  # ROR A
                ci = self._get_flag(self.flag_c)
                self._set_flag(self.flag_c, (self.a & 0x01) != 0)
                self.a = ((self.a >> 1) | (ci << 7)) & 0xFF; self._set_zn(self.a); base_cycles = 2; mnemonic = "ROR A"
            elif opcode == 0x66:
                a = self._zp(); v = self._fetch(a); ci = self._get_flag(self.flag_c)
                self._set_flag(self.flag_c, (v & 0x01) != 0)
                v = ((v >> 1) | (ci << 7)) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 5; mnemonic = "ROR zp"
            elif opcode == 0x76:
                a = self._zpx(); v = self._fetch(a); ci = self._get_flag(self.flag_c)
                self._set_flag(self.flag_c, (v & 0x01) != 0)
                v = ((v >> 1) | (ci << 7)) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 6; mnemonic = "ROR zpx"
            elif opcode == 0x6E:
                a = self._abs(); v = self._fetch(a); ci = self._get_flag(self.flag_c)
                self._set_flag(self.flag_c, (v & 0x01) != 0)
                v = ((v >> 1) | (ci << 7)) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 6; mnemonic = "ROR abs"
            elif opcode == 0x7E:
                a = self._abx(); v = self._fetch(a); ci = self._get_flag(self.flag_c)
                self._set_flag(self.flag_c, (v & 0x01) != 0)
                v = ((v >> 1) | (ci << 7)) & 0xFF; self._write(a, v); self._set_zn(v); base_cycles = 7; mnemonic = "ROR abx"
            # ---- Common multi-byte NOPs (silently advance) --------------
            elif opcode in (0x1A, 0x3A, 0x5A, 0x7A, 0xDA, 0xFA):
                base_cycles = 2; mnemonic = "NOP"
            elif opcode in (0x80, 0x82, 0x89, 0xC2, 0xE2):
                self._imm(); base_cycles = 2; mnemonic = "NOP #"
            elif opcode in (0x04, 0x44, 0x64):
                self._zp(); base_cycles = 3; mnemonic = "NOP zp"
            elif opcode in (0x14, 0x34, 0x54, 0x74, 0xD4, 0xF4):
                self._zpx(); base_cycles = 4; mnemonic = "NOP zpx"
            elif opcode == 0x0C:
                self._abs(); base_cycles = 4; mnemonic = "NOP abs"
            elif opcode in (0x1C, 0x3C, 0x5C, 0x7C, 0xDC, 0xFC):
                self._abx(extra_cycle=True); base_cycles = 4; mnemonic = "NOP abx"
            else:
                # Unknown opcode: treat as 2-cycle NOP to keep stepping.
                base_cycles = 2
                mnemonic = f"NOP* ${opcode:02X}"

            self.status |= self.flag_u
            self.cycles += base_cycles
            self.executed_instr += 1
            self.last_instr = f"{mnemonic} @${opcode_addr:04X}"
            # Track scanline transitions for MMC3 IRQ before we forget the old SL.
            old_sl = self.ppu.scanline
            self.ppu.step(base_cycles * 3)
            self.apu.step(base_cycles)
            if self.mapper == 4:
                new_sl = self.ppu.scanline
                # MMC3 IRQ counter is clocked at the start of each visible scanline
                # and the post-render dummy line, when rendering is enabled.
                if new_sl != old_sl and (self.ppu.mask & 0x18) and 0 <= new_sl < 240:
                    if self.mmc3_irq_counter == 0 or self.mmc3_irq_reload:
                        self.mmc3_irq_counter = self.mmc3_irq_latch
                        self.mmc3_irq_reload = False
                    else:
                        self.mmc3_irq_counter -= 1
                    if self.mmc3_irq_counter == 0 and self.mmc3_irq_enable:
                        self.mmc3_irq_pending = True

        def _service_nmi(self):
            self._push((self.pc >> 8) & 0xFF)
            self._push(self.pc & 0xFF)
            self._push((self.status & ~self.flag_b) | self.flag_u)
            self._set_flag(self.flag_i, True)
            self.pc = self._read16(0xFFFA)
            self.cycles += 7
            self.ppu.step(21)

        def _service_irq(self):
            self._push((self.pc >> 8) & 0xFF)
            self._push(self.pc & 0xFF)
            self._push((self.status & ~self.flag_b) | self.flag_u)
            self._set_flag(self.flag_i, True)
            self.pc = self._read16(0xFFFE)
            self.cycles += 7
            self.ppu.step(21)
            self.apu.step(7)

        def load_rom(self, filepath):
            try:
                with open(filepath, 'rb') as f:
                    header = f.read(16)
                    if len(header) < 16:
                        print("ROM too small to contain iNES header.")
                        return False
                    
                    # Check for iNES Magic Number: 'N', 'E', 'S', EOF
                    if header[:4] != b'NES\x1a':
                        print("Invalid ROM header.")
                        return False
                        
                    self.prg_banks = header[4]
                    self.chr_banks = header[5]
                    
                    # Flags 6 & 7 contain mapper and mirroring info
                    flag6 = header[6]
                    flag7 = header[7]
                    self.mapper = (flag7 & 0xF0) | ((flag6 & 0xF0) >> 4)
                    # iNES mirroring: bit 3 of flag6 = four-screen, else bit 0 selects H/V.
                    if flag6 & 0x08:
                        self.mirroring = 4  # four-screen
                    else:
                        self.mirroring = flag6 & 0x01  # 0 = vertical-mirror, 1 = horizontal-mirror
                    if self.mapper not in (0, 1, 2, 3):
                        print(f"Mapper {self.mapper} not supported in Python core yet.")
                        return False
                    
                    # Skip trainer if present
                    if flag6 & 0x04:
                        f.read(512)
                        
                    # Read PRG-ROM (Program Data)
                    prg_data = f.read(self.prg_banks * 16384)
                    expected_prg = self.prg_banks * 16384
                    if len(prg_data) != expected_prg:
                        print("ROM ended before expected PRG data size.")
                        return False
                    
                    chr_data = f.read(self.chr_banks * 8192) if self.chr_banks > 0 else b""
                    if self.chr_banks > 0 and len(chr_data) != self.chr_banks * 8192:
                        print("ROM ended before expected CHR data size.")
                        return False

                    # Load PRG-ROM into memory at 0x8000 (mapper 0)
                    self.memory[:] = b"\x00" * 0x10000
                    self.ram[:] = b"\x00" * 0x800
                    self.prg_rom = prg_data
                    self.chr_rom = chr_data
                    self.chr_ram[:] = b"\x00" * 0x2000
                    self.ppu = PyPpu()
                    self.ppu.mirroring = self.mirroring
                    self.prg_bank_16k = 0
                    self.chr_bank_8k = 0
                    self.mmc1_shift = 0x10
                    self.mmc1_ctrl = 0x0C
                    self.mmc1_chr0 = 0
                    self.mmc1_chr1 = 0
                    self.mmc1_prg = 0
                    self._apply_chr_bank()
                    for i, byte in enumerate(prg_data):
                        if 0x8000 + i < 0x10000:
                            self.memory[0x8000 + i] = byte
                    
                    # Mirror PRG ROM if only 1 bank (16KB)
                    if self.prg_banks == 1:
                        for i in range(16384):
                            self.memory[0xC000 + i] = self.memory[0x8000 + i]
                            
                    self.a = 0
                    self.x = 0
                    self.y = 0
                    self.sp = 0xFD
                    self.status = 0x24
                    self.cycles = 7
                    self.executed_instr = 0
                    self.frame_counter = 0
                    # Reset vector from CPU address space.
                    self.pc = self._read16(0xFFFC)
                    
                    self.debug_info = (
                        f"PRG Banks: {self.prg_banks} (16KB each)\n"
                        f"CHR Banks: {self.chr_banks} (8KB each)\n"
                        f"Mapper: {self.mapper}\n"
                        f"Reset Vector: ${self.pc:04X}"
                    )
                    
                    self.rom_loaded = True
                    return True
            except Exception as e:
                print(f"Error loading ROM: {e}")
                return False
                
        def run_frame(self):
            if not self.rom_loaded:
                return "No ROM loaded"
            # NTSC frame budget for CPU ~= 29780.5 cycles
            target = self.cycles + 29780
            while self.cycles < target:
                self._execute_instruction()
            self.frame_counter += 1
            self.ppu.render_frame()
            self.debug_info = (
                f"Frame: {self.frame_counter}\n"
                f"PC:${self.pc:04X}  A:${self.a:02X} X:${self.x:02X} Y:${self.y:02X}\n"
                f"SP:${self.sp:02X}  P:${self.status:02X}  Cy:{self.cycles}\n"
                f"Instr: {self.executed_instr}\n"
                f"PPU: SL:{self.ppu.scanline} DOT:{self.ppu.dot} VBL:{1 if (self.ppu.status & 0x80) else 0}\n"
                f"Last: {self.last_instr}"
            )
            return self.debug_info

        def get_framebuffer(self):
            return self.ppu.framebuffer

        def set_controller1(self, state):
            self.controller1_state = state & 0xFF
            if self.controller_strobe:
                self.controller1_shift = self.controller1_state

        def consume_apu_activity(self):
            val = self.apu_activity
            self.apu_activity = 0
            return val

        def save_state(self):
            return {
                "pc": self.pc,
                "a": self.a,
                "x": self.x,
                "y": self.y,
                "sp": self.sp,
                "status": self.status,
                "cycles": self.cycles,
                "frame_counter": self.frame_counter,
                "executed_instr": self.executed_instr,
                "ram": bytes(self.ram),
                "ppu_vram": bytes(self.ppu.vram),
                "ppu_palette": bytes(self.ppu.palette_ram),
                "ppu_oam": bytes(self.ppu.oam),
                "ppu_ctrl": self.ppu.ctrl,
                "ppu_mask": self.ppu.mask,
                "ppu_status": self.ppu.status,
                "ppu_v": self.ppu.v,
                "ppu_t": self.ppu.t,
                "ppu_x": self.ppu.x,
                "ppu_w": self.ppu.w,
                "controller1_state": self.controller1_state,
                "controller1_shift": self.controller1_shift,
                "controller_strobe": self.controller_strobe,
                "prg_bank_16k": self.prg_bank_16k,
                "chr_bank_8k": self.chr_bank_8k,
                "mmc1_shift": self.mmc1_shift,
                "mmc1_ctrl": self.mmc1_ctrl,
                "mmc1_chr0": self.mmc1_chr0,
                "mmc1_chr1": self.mmc1_chr1,
                "mmc1_prg": self.mmc1_prg,
            }

        def load_state(self, state):
            self.pc = state["pc"]
            self.a = state["a"]
            self.x = state["x"]
            self.y = state["y"]
            self.sp = state["sp"]
            self.status = state["status"]
            self.cycles = state["cycles"]
            self.frame_counter = state["frame_counter"]
            self.executed_instr = state["executed_instr"]
            self.ram[:] = state["ram"]
            self.ppu.vram[:] = state["ppu_vram"]
            self.ppu.palette_ram[:] = state["ppu_palette"]
            self.ppu.oam[:] = state["ppu_oam"]
            self.ppu.ctrl = state["ppu_ctrl"]
            self.ppu.mask = state["ppu_mask"]
            self.ppu.status = state["ppu_status"]
            self.ppu.v = state["ppu_v"]
            self.ppu.t = state["ppu_t"]
            self.ppu.x = state["ppu_x"]
            self.ppu.w = state["ppu_w"]
            self.controller1_state = state["controller1_state"]
            self.controller1_shift = state["controller1_shift"]
            self.controller_strobe = state["controller_strobe"]
            self.prg_bank_16k = state.get("prg_bank_16k", 0)
            self.chr_bank_8k = state.get("chr_bank_8k", 0)
            self.mmc1_shift = state.get("mmc1_shift", 0x10)
            self.mmc1_ctrl = state.get("mmc1_ctrl", 0x0C)
            self.mmc1_chr0 = state.get("mmc1_chr0", 0)
            self.mmc1_chr1 = state.get("mmc1_chr1", 0)
            self.mmc1_prg = state.get("mmc1_prg", 0)
            self._apply_chr_bank()
            self.ppu.render_frame()
            
        def reset(self):
            if self.rom_loaded:
                self.a = 0
                self.x = 0
                self.y = 0
                self.sp = 0xFD
                self.status = 0x24
                self.cycles = 7
                self.executed_instr = 0
                self.frame_counter = 0
                self.pc = self._read16(0xFFFC)
                self.ppu = PyPpu()
                self.ppu.mirroring = self.mirroring
                self._apply_chr_bank()
                self.controller1_state = 0
                self.controller1_shift = 0
                self.controller_strobe = 0
            self.running = False

    nes_core = PyNesCore()


class GeminiNesApp:
    def __init__(self, root):
        self.root = root
        self.root.title("A.C Holding nes emu 0.1")
        self.root.geometry("600x400")
        self.root.minsize(600, 400)
        self.root.configure(bg="black")
        
        self.core = nes_core if not CORE_LOADED else nes_core.Core()
        self.is_running = False
        self.rom_loaded = False
        self.frame_image = None
        self.frame_image_scaled = None
        self._hex_palette = None
        self._rgb_palette = None
        self.speed_multiplier = 1.0
        self.speed_accumulator = 0.0
        self.save_slot = None
        self.controller1_state = 0
        self._fps_last = time.time()
        self._fps_frames = 0
        self._fps_value = 0.0
        self.is_fullscreen = False
        self._last_audio_tick = 0.0
        self._pulse_wav_path = self._create_pulse_wav()
        self._afplay_path = shutil.which("afplay")
        
        self.setup_ui()
        self.bind_controls()
        self.render_loop()

    def setup_ui(self):
        # Top Control Frame
        control_frame = tk.Frame(self.root, bg="black")
        control_frame.pack(side=tk.TOP, fill=tk.X, pady=2)

        # Style configurations requested: Blue buttons, Black text
        btn_style = {
            "bg": "blue",
            "fg": "black",
            "activebackground": "#3333ff",
            "activeforeground": "black",
            "font": ("Courier", 8, "bold"),
            "relief": tk.RAISED,
            "bd": 1,
            "padx": 2,
            "pady": 0,
        }

        self.btn_load = tk.Button(control_frame, text="LOAD", command=self.load_rom, **btn_style)
        self.btn_load.pack(side=tk.LEFT, padx=2)

        self.btn_start = tk.Button(control_frame, text="START", command=self.start_emulation, **btn_style)
        self.btn_start.pack(side=tk.LEFT, padx=2)

        self.btn_stop = tk.Button(control_frame, text="STOP", command=self.stop_emulation, **btn_style)
        self.btn_stop.pack(side=tk.LEFT, padx=2)

        self.btn_reset = tk.Button(control_frame, text="RESET", command=self.reset_emulation, **btn_style)
        self.btn_reset.pack(side=tk.LEFT, padx=2)

        self.btn_fullscreen = tk.Button(control_frame, text="FULL", command=self.toggle_fullscreen, **btn_style)
        self.btn_fullscreen.pack(side=tk.LEFT, padx=2)

        # Inline speed control to save vertical space
        tk.Label(control_frame, text="SPD", bg="black", fg="cyan", font=("Courier", 8)).pack(side=tk.LEFT, padx=(8, 2))
        self.speed_scale = tk.Scale(
            control_frame,
            from_=0.5,
            to=4.0,
            resolution=0.5,
            orient=tk.HORIZONTAL,
            length=120,
            sliderlength=14,
            width=8,
            showvalue=0,
            bg="black",
            fg="cyan",
            troughcolor="#001830",
            highlightthickness=0,
            command=self.set_speed,
        )
        self.speed_scale.set(1.0)
        self.speed_scale.pack(side=tk.LEFT, padx=2)

        # Status Label
        self.status_label = tk.Label(
            self.root,
            text="Core: " + CORE_NAME,
            bg="black",
            fg="blue",
            font=("Courier", 8),
            anchor="w",
        )
        self.status_label.pack(side=tk.BOTTOM, fill=tk.X, pady=1)

        # Emulator Screen at native 256x240 to fit a 600x400 window cleanly
        self.screen = tk.Canvas(
            self.root,
            width=256,
            height=240,
            bg="#0a0a0a",
            highlightthickness=1,
            highlightbackground="blue",
        )
        self.screen.pack(side=tk.TOP, pady=4)
        self._build_scanline_overlay()

        self.screen.create_text(
            128, 120,
            text="NO ROM LOADED",
            fill="blue",
            font=("Courier", 12, "bold"),
            tags="status_text"
        )

    def bind_controls(self):
        self.root.bind("<KeyPress>", self.on_key_press)
        self.root.bind("<KeyRelease>", self.on_key_release)
        self.root.bind("<F5>", lambda _e: self.save_state())
        self.root.bind("<F6>", lambda _e: self.load_state())
        self.root.bind("<KeyPress-minus>", lambda _e: self.nudge_speed(-0.5))
        self.root.bind("<KeyPress-equal>", lambda _e: self.nudge_speed(0.5))
        self.root.bind("<KeyPress-plus>", lambda _e: self.nudge_speed(0.5))
        self.root.bind("<KeyPress-1>", lambda _e: self.set_speed(1.0))
        self.root.bind("<KeyPress-2>", lambda _e: self.set_speed(2.0))
        self.root.bind("<KeyPress-4>", lambda _e: self.set_speed(4.0))
        self.root.bind("<F11>", lambda _e: self.toggle_fullscreen())
        self.root.bind("<Escape>", lambda _e: self.set_fullscreen(False))
        self.root.focus_set()

    def _build_scanline_overlay(self):
        self.screen.delete("scanline")
        # Subtle blue CRT scanlines on every other row at native 256x240
        for y in range(0, 240, 2):
            self.screen.create_line(0, y, 256, y, fill="#00162a", tags="scanline")

    def set_speed(self, value):
        try:
            self.speed_multiplier = max(0.5, min(4.0, float(value)))
            if hasattr(self, "speed_scale"):
                self.speed_scale.set(self.speed_multiplier)
        except (TypeError, ValueError):
            self.speed_multiplier = 1.0

    def nudge_speed(self, delta):
        self.set_speed(self.speed_multiplier + delta)

    def set_fullscreen(self, enabled):
        self.is_fullscreen = bool(enabled)
        self.root.attributes("-fullscreen", self.is_fullscreen)

    def toggle_fullscreen(self):
        self.set_fullscreen(not self.is_fullscreen)

    def _create_pulse_wav(self):
        # Small pulse wave used as a minimal APU placeholder.
        fd, path = tempfile.mkstemp(prefix="catsnes_pulse_", suffix=".wav")
        os.close(fd)
        sample_rate = 22050
        duration = 0.055
        freq = 880.0
        total = int(sample_rate * duration)
        pcm = bytearray()
        for i in range(total):
            t = i / sample_rate
            v = 12000 if math.sin(2.0 * math.pi * freq * t) >= 0 else -12000
            pcm.extend(int(v).to_bytes(2, "little", signed=True))
        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(bytes(pcm))
        return path

    def _play_pulse(self):
        now = time.time()
        if now - self._last_audio_tick < 0.06:
            return
        self._last_audio_tick = now
        try:
            if not self._afplay_path:
                raise RuntimeError("afplay unavailable")
            threading.Thread(
                target=subprocess.run,
                args=([self._afplay_path, self._pulse_wav_path],),
                kwargs={"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL},
                daemon=True,
            ).start()
        except Exception:
            try:
                self.root.bell()
            except Exception:
                pass

    def _set_btn(self, mask, pressed):
        if pressed:
            self.controller1_state |= mask
        else:
            self.controller1_state &= (~mask) & 0xFF
        if hasattr(self.core, "set_controller1"):
            self.core.set_controller1(self.controller1_state)

    def on_key_press(self, event):
        k = event.keysym.lower()
        if k == "up":
            self._set_btn(0x10, True)
        elif k == "down":
            self._set_btn(0x20, True)
        elif k == "left":
            self._set_btn(0x40, True)
        elif k == "right":
            self._set_btn(0x80, True)
        elif k == "z":
            self._set_btn(0x01, True)  # A
        elif k == "x":
            self._set_btn(0x02, True)  # B
        elif k == "shift_l" or k == "shift_r":
            self._set_btn(0x04, True)  # Select
        elif k == "return":
            self._set_btn(0x08, True)  # Start

    def on_key_release(self, event):
        k = event.keysym.lower()
        if k == "up":
            self._set_btn(0x10, False)
        elif k == "down":
            self._set_btn(0x20, False)
        elif k == "left":
            self._set_btn(0x40, False)
        elif k == "right":
            self._set_btn(0x80, False)
        elif k == "z":
            self._set_btn(0x01, False)
        elif k == "x":
            self._set_btn(0x02, False)
        elif k == "shift_l" or k == "shift_r":
            self._set_btn(0x04, False)
        elif k == "return":
            self._set_btn(0x08, False)

    def save_state(self):
        if hasattr(self.core, "save_state"):
            self.save_slot = copy.deepcopy(self.core.save_state())
            self.status_label.config(text="State saved (F5).")
        else:
            self.status_label.config(text="State save not supported by this core yet.")

    def load_state(self):
        if self.save_slot is None:
            self.status_label.config(text="No saved state yet.")
            return
        if hasattr(self.core, "load_state"):
            self.core.load_state(copy.deepcopy(self.save_slot))
            self.status_label.config(text="State loaded (F6).")
        else:
            self.status_label.config(text="State load not supported by this core yet.")

    def load_rom(self):
        filepath = filedialog.askopenfilename(
            title="Select NES ROM",
            filetypes=(("NES ROMs", "*.nes"), ("All files", "*.*"))
        )
        if filepath:
            try:
                success = self.core.load_rom(filepath)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load ROM:\n{e}")
                return
            if success:
                self.rom_loaded = True
                self.status_label.config(text=f"Loaded: {os.path.basename(filepath)}")
                self.screen.delete("all")
                self._build_scanline_overlay()
                
                # Display Cartridge Header Info to prove it's reading the ROM
                if not CORE_LOADED:
                    self.screen.create_text(
                        128, 60,
                        text=f"CARTRIDGE PARSED\n\n{self.core.debug_info}",
                        fill="cyan", font=("Courier", 8), justify="center", tags="status_text",
                    )
                else:
                    self.screen.create_text(
                        128, 120, text="READY TO PLAY",
                        fill="cyan", font=("Courier", 12, "bold"), tags="status_text",
                    )
            else:
                messagebox.showerror("Error", "Failed to load ROM. Is it a valid .nes file?")

    def start_emulation(self):
        core_rom_loaded = getattr(self.core, "rom_loaded", None)
        if (core_rom_loaded is False) or (core_rom_loaded is None and not self.rom_loaded):
            messagebox.showwarning("Warning", "Please load a ROM first!")
            return
            
        self.is_running = True
        self.status_label.config(text="Emulation Running...")

    def stop_emulation(self):
        self.is_running = False
        self.status_label.config(text="Emulation Paused.")

    def reset_emulation(self):
        self.is_running = False
        if hasattr(self.core, "reset"):
            self.core.reset()
        self.rom_loaded = False
        self.screen.delete("all")
        self._build_scanline_overlay()
        self.screen.create_text(128, 120, text="SYSTEM RESET", fill="blue", font=("Courier", 12, "bold"), tags="status_text")
        self.status_label.config(text="System Reset.")

    def render_loop(self):
        if self.is_running:
            telemetry = "No CPU core"
            self.speed_accumulator += self.speed_multiplier
            runs = int(self.speed_accumulator)
            if runs > 0:
                self.speed_accumulator -= runs
                for _ in range(runs):
                    if hasattr(self.core, "run_frame"):
                        telemetry = self.core.run_frame()
            self.screen.delete("frame")
            self.screen.delete("running_status")
            if hasattr(self.core, "get_framebuffer"):
                self._draw_ppu_frame(self.core.get_framebuffer())
            self._fps_frames += 1
            now = time.time()
            if now - self._fps_last >= 1.0:
                self._fps_value = self._fps_frames / (now - self._fps_last)
                self._fps_last = now
                self._fps_frames = 0
            self.screen.create_text(
                128,
                228,
                text=f"FPS:{self._fps_value:.1f}  x{self.speed_multiplier:.1f}",
                fill="cyan",
                font=("Courier", 7),
                justify="center",
                tags="running_status",
            )
            # Push the more detailed telemetry into the bottom status label.
            try:
                short = telemetry.split("\n")[0] if isinstance(telemetry, str) else ""
                self.status_label.config(text=f"FPS:{self._fps_value:.1f} x{self.speed_multiplier:.1f}  {short}")
            except Exception:
                pass
            if hasattr(self.core, "consume_apu_activity") and self.core.consume_apu_activity() > 0:
                self._play_pulse()
            self.screen.tag_raise("scanline")

        # Schedule the next frame (~60 FPS)
        self.root.after(16, self.render_loop)

    def _draw_ppu_frame(self, framebuffer):
        if not framebuffer or len(framebuffer) < (256 * 240):
            return
        palette = self.core.ppu.NES_RGB if hasattr(self.core, "ppu") else []
        if self._rgb_palette is None and palette:
            self._rgb_palette = [bytes((r, g, b)) for (r, g, b) in palette]
        if not self._rgb_palette:
            return
        # Pull PPUMASK so we can render greyscale + RGB emphasis like real hardware.
        mask = getattr(self.core.ppu, "mask", 0)
        greyscale = (mask & 0x01) != 0
        em_r = (mask & 0x20) != 0
        em_g = (mask & 0x40) != 0
        em_b = (mask & 0x80) != 0
        any_em = em_r or em_g or em_b
        # Emphasis: boost the named channel(s) and dim the others, like the NTSC PPU.
        if any_em:
            er = 1.10 if em_r else 0.85
            eg = 1.10 if em_g else 0.85
            eb = 1.10 if em_b else 0.85
        else:
            er = eg = eb = 1.0
        # Build a native 256x240 frame and apply subtle CRT-style scanline dim.
        rgb = bytearray(256 * 240 * 3)
        dst = 0
        for y in range(240):
            src_base = y * 256
            dim = 0.85 if (y & 1) else 1.0
            for x in range(256):
                idx = framebuffer[src_base + x] & 0x3F
                if greyscale:
                    idx &= 0x30  # collapse hue, keep luminance group
                px = self._rgb_palette[idx] if idx < len(self._rgb_palette) else b"\x00\x00\x00"
                r = min(255, max(0, int(px[0] * dim * er + 4)))
                g = min(255, max(0, int(px[1] * dim * eg + 4)))
                b = min(255, max(0, int(px[2] * dim * eb + 6)))
                rgb[dst] = r
                rgb[dst + 1] = g
                rgb[dst + 2] = b
                dst += 3
        header = b"P6 256 240 255 "
        ppm = header + bytes(rgb)
        self.frame_image_scaled = tk.PhotoImage(data=ppm, format="PPM")
        self.screen.create_image(0, 0, image=self.frame_image_scaled, anchor=tk.NW, tags="frame")

if __name__ == "__main__":
    root = tk.Tk()
    app = GeminiNesApp(root)
    root.mainloop()
