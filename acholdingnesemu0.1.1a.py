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

# Attempt to load the pre-baked Cython core (in priority order).
CORE_LOADED = False
CORE_NAME = "Pure Python Backend"
nes_core = None

def _try_import(name, label):
    global nes_core, CORE_LOADED, CORE_NAME
    try:
        nes_core = __import__(name)
        CORE_LOADED = True
        CORE_NAME = label
        return True
    except Exception:
        return False

# Order: newest Cython core first, then older ones, then on-the-fly compile.
if not _try_import("catsnescore_0_2", "Cat'snescore 0.2 (Cython)"):
    if not _try_import("catsnescore_0_1", "Cat'snescore 0.1 (Cython)"):
        if not _try_import("gemininesemu0_1", "gemininesemu0_1 (Cython)"):
            # Try pyximport so a .pyx file next to us can compile on demand.
            try:
                import pyximport  # type: ignore
                pyximport.install(language_level=3)
                if _try_import("catsnescore_0_2", "Cat'snescore 0.2 (Cython, pyximport)"):
                    pass
            except Exception:
                pass

if not CORE_LOADED:
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