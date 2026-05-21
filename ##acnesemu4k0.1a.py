#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
# =============================================================================
#   acnesemu 0.1  -  cat's NES emulator (single file)
#   Cython-compatible pure Python  /  tkinter  /  FCEUX-style GUI
#   Theme: black bg, blue text, blue hue accents  /  600x400
#   by Flames / Samsoft / Team Flames                                    owo
# =============================================================================

from __future__ import annotations
import os, sys, struct, time, threading, tempfile
import tkinter as tk
from tkinter import filedialog, messagebox

# -----------------------------------------------------------------------------
#   theme   ( black bg / blue text  -  fceux-ish )
# -----------------------------------------------------------------------------
BG       = "#000000"
FG       = "#00aaff"     # blue text
ACCENT   = "#0066aa"     # darker blue hue
DIM      = "#003355"     # disabled / inset
EDGE     = "#0088cc"     # button border-ish
HI       = "#00ddff"     # highlight
FONT_MONO  = ("Courier", 9)
FONT_MONO_B= ("Courier", 9, "bold")
FONT_UI    = ("Courier", 8)

# =============================================================================
#   iNES ROM loader
# =============================================================================
class INES:
    """Parse an iNES (.nes) ROM file; mapper field selects cartridge logic."""
    __slots__ = ("prg", "chr", "mapper", "mirror", "battery",
                 "prg_banks", "chr_banks", "trainer", "path")

    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            data = f.read()
        if len(data) < 16 or data[:4] != b"NES\x1a":
            raise ValueError("not an iNES rom")
        prg_banks = data[4]                 # 16KB units
        chr_banks = data[5]                 #  8KB units
        flags6    = data[6]
        flags7    = data[7]
        self.prg_banks = prg_banks
        self.chr_banks = chr_banks
        self.mirror    = "V" if (flags6 & 1) else "H"
        self.battery   = bool(flags6 & 2)
        self.trainer   = bool(flags6 & 4)
        self.mapper    = ((flags6 >> 4) & 0x0F) | (flags7 & 0xF0)
        nes2 = len(data) >= 32 and (flags7 & 0x0C) == 0x08 and (flags7 & 0xF0) == 0
        if nes2:
            self.mapper = (data[8] | (data[9] << 8)) & 0xFFF
            if data[4] == 0:
                prg_banks = max(1, (data[10] | (data[11] << 8) | (data[12] << 16)) // 16384)
            if data[5] == 0:
                chr_banks = max(0, (data[13] | (data[14] << 8) | (data[15] << 16)) // 8192)
        self.prg_banks = prg_banks
        self.chr_banks = chr_banks
        if prg_banks == 0:
            raise ValueError("invalid prg bank count (0)")
        off = 16 + (512 if self.trainer else 0)
        prg_size = prg_banks * 16384
        chr_size = chr_banks * 8192
        if len(data) < off + prg_size:
            raise ValueError(
                f"rom too small for PRG ({len(data)} bytes, need {off + prg_size})")
        self.prg = bytearray(data[off : off + prg_size])
        off += prg_size
        if chr_banks:
            chunk = data[off : off + chr_size]
            self.chr = bytearray(chunk)
            if len(self.chr) < chr_size:
                self.chr.extend(b"\x00" * (chr_size - len(self.chr)))
        else:
            self.chr = bytearray(8192)      # CHR-RAM

    def info(self) -> str:
        return (f"PRG: {self.prg_banks*16}KB  CHR: {self.chr_banks*8}KB  "
                f"mapper: {self.mapper}  mirror: {self.mirror}  "
                f"battery: {'yes' if self.battery else 'no'}")


# =============================================================================
#   Cartridge mappers
# =============================================================================
class _MapperBase:
    __slots__ = ("bus", "rom", "wram")

    def __init__(self, bus: "Bus", rom: INES):
        self.bus = bus
        self.rom = rom
        self.wram = bytearray(8192)
        bus.ppu_mirror = rom.mirror

    def reset(self) -> None:
        pass

    def on_frame(self) -> None:
        pass

    def step_scanline(self, _sl: int) -> None:
        pass

    def prg_read(self, addr: int) -> int:
        raise NotImplementedError

    def prg_write(self, addr: int, val: int) -> None:
        pass

    def chr_read(self, addr: int) -> int:
        c = self.rom.chr
        return c[addr % len(c)] if c else 0

    def chr_write(self, addr: int, val: int) -> None:
        if self.rom.chr_banks == 0 and self.rom.chr:
            self.rom.chr[addr % len(self.rom.chr)] = val & 0xFF

    def wram_read(self, addr: int) -> int:
        return self.wram[(addr - 0x6000) & 0x1FFF]

    def wram_write(self, addr: int, val: int) -> None:
        self.wram[(addr - 0x6000) & 0x1FFF] = val & 0xFF


class Mapper0(_MapperBase):
    """NROM / fixed banking."""
    def prg_read(self, addr: int) -> int:
        idx = addr - 0x8000
        if self.rom.prg_banks == 1:
            idx &= 0x3FFF
        return self.rom.prg[idx % len(self.rom.prg)]


class Mapper2(_MapperBase):
    """UNROM / UxROM"""
    __slots__ = ("bank",)

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.bank = 0

    def reset(self) -> None:
        self.bank = 0

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        L = len(prg)
        if addr < 0xC000:
            return prg[(self.bank * 0x4000 + (addr - 0x8000)) % L]
        return prg[(L - 0x4000 + (addr - 0xC000)) % L]

    def prg_write(self, addr: int, val: int) -> None:
        if addr >= 0x8000:
            n = max(1, len(self.rom.prg) // 0x4000)
            self.bank = val & (n - 1)


class Mapper3(_MapperBase):
    """CNROM"""
    __slots__ = ("chr_bank",)

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.chr_bank = 0

    def reset(self) -> None:
        self.chr_bank = 0

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        return prg[(addr - 0x8000) % len(prg)]

    def prg_write(self, addr: int, val: int) -> None:
        if addr >= 0x8000:
            n = max(1, len(self.rom.chr) // 0x2000)
            self.chr_bank = val & (n - 1)

    def chr_read(self, addr: int) -> int:
        c = self.rom.chr
        if not c: return 0
        return c[(self.chr_bank * 0x2000 + (addr & 0x1FFF)) % len(c)]

    def chr_write(self, addr: int, val: int) -> None:
        if self.rom.chr_banks == 0 and self.rom.chr:
            c = self.rom.chr
            c[(self.chr_bank * 0x2000 + (addr & 0x1FFF)) % len(c)] = val & 0xFF


class Mapper7(_MapperBase):
    """AxROM"""
    __slots__ = ("bank",)

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.bank = 0

    def reset(self) -> None:
        self.bank = 0
        self.bus.ppu_mirror = self.rom.mirror

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        L = len(prg)
        off = self.bank * 0x8000 + (addr - 0x8000)
        return prg[off % L]

    def prg_write(self, addr: int, val: int) -> None:
        if addr >= 0x8000:
            self.bank = val & 7
            self.bus.ppu_mirror = "1" if (val & 0x10) else "0"


class Mapper1(_MapperBase):
    """MMC1"""
    __slots__ = ("shift", "control", "chr0", "chr1", "prg")

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.shift = 0x10
        self.control = 0x0C
        self.chr0 = self.chr1 = self.prg = 0
        self._sync_mirror()

    def reset(self) -> None:
        self.shift = 0x10
        self.control = 0x0C
        self.chr0 = self.chr1 = self.prg = 0
        self._sync_mirror()

    def _sync_mirror(self) -> None:
        m = self.control & 3
        if m == 0:    self.bus.ppu_mirror = "0"
        elif m == 1:  self.bus.ppu_mirror = "1"
        elif m == 2:  self.bus.ppu_mirror = "V"
        else:         self.bus.ppu_mirror = "H"

    def prg_write(self, addr: int, val: int) -> None:
        if val & 0x80:
            self.shift = 0x10
            self.control |= 0x0C
            self._sync_mirror()
            return
        complete = ((self.shift >> 1) | ((val & 1) << 4)) & 0x1F
        self.shift = complete
        if complete & 1:
            data = (complete >> 1) & 0x0F
            slot = (addr >> 13) & 3
            if slot == 0:
                self.control = data & 0x1F
                self._sync_mirror()
            elif slot == 1: self.chr0 = data & 0x1F
            elif slot == 2: self.chr1 = data & 0x1F
            else:           self.prg = data & 0x0F
            self.shift = 0x10

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        L = len(prg)
        banks_16 = max(1, L // 0x4000)
        if self.control & 0x08:
            if self.control & 0x04:
                bank = self.prg & (banks_16 - 1)
                if addr >= 0xC000:
                    return prg[(bank * 0x4000 + (addr - 0xC000)) % L]
                return prg[((banks_16 - 1) * 0x4000 + (addr - 0x8000)) % L]
            bank = self.prg & (banks_16 - 1)
            if addr < 0xC000:
                return prg[(bank * 0x4000 + (addr - 0x8000)) % L]
            return prg[((banks_16 - 1) * 0x4000 + (addr - 0xC000)) % L]
        b32 = (self.prg >> 1) & (max(1, L // 0x8000) - 1)
        return prg[(b32 * 0x8000 + (addr - 0x8000)) % L]

    def chr_read(self, addr: int) -> int:
        c = self.rom.chr
        if not c: return 0
        cl = len(c)
        a = addr & 0x1FFF
        if self.control & 0x10:
            off = (self.chr0 if a < 0x1000 else self.chr1) * 0x1000 + (a & 0x0FFF)
        else:
            off = (self.chr0 & 0x1E) * 0x1000 + a
        return c[off % cl]

    def chr_write(self, addr: int, val: int) -> None:
        if self.rom.chr_banks != 0:
            return
        c = self.rom.chr
        if not c:
            return
        cl = len(c)
        a = addr & 0x1FFF
        if self.control & 0x10:
            off = (self.chr0 if a < 0x1000 else self.chr1) * 0x1000 + (a & 0x0FFF)
        else:
            off = (self.chr0 & 0x1E) * 0x1000 + a
        c[off % cl] = val & 0xFF


class Mapper4(_MapperBase):
    """MMC3 / TxROM family"""
    __slots__ = ("r", "bank_sel", "prg_mode", "chr_mode",
                 "irq_latch", "irq_counter", "irq_enabled", "irq_reload", "irq_pending")

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.reset()

    def reset(self) -> None:
        self.r = [0, 2, 4, 5, 6, 7, 0, 1]
        self.bank_sel = 0
        self.prg_mode = self.chr_mode = 0
        self.irq_latch = 0
        self.irq_counter = 0
        self.irq_enabled = False
        self.irq_reload = False
        self.irq_pending = False

    def step_scanline(self, sl: int) -> None:
        if sl < 0 or sl >= 240:
            return
        if self.irq_reload or self.irq_counter == 0:
            self.irq_counter = self.irq_latch
            self.irq_reload = False
        else:
            self.irq_counter = (self.irq_counter - 1) & 0xFF
        if self.irq_counter == 0 and self.irq_enabled:
            self.irq_pending = True

    def prg_write(self, addr: int, val: int) -> None:
        if addr < 0x8000:
            return
        val &= 0xFF
        odd = addr & 1
        if addr < 0xA000:
            if odd == 0:
                self.bank_sel = val & 7
                self.prg_mode = (val >> 6) & 1
                self.chr_mode = (val >> 7) & 1
            else:
                self.r[self.bank_sel] = val
        elif addr < 0xC000:
            if odd == 0:
                self.bus.ppu_mirror = "V" if (val & 1) else "H"
        elif addr < 0xE000:
            if odd == 0:
                self.irq_latch = val
            else:
                self.irq_reload = True
                self.irq_counter = 0
        else:
            if odd == 0:
                self.irq_enabled = False
                self.irq_pending = False
            else:
                self.irq_enabled = True

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        L = len(prg)
        n8 = max(1, L // 0x2000)
        R = self.r
        if self.prg_mode == 0:
            b8000 = (R[6] % n8) * 0x2000
            bA000 = (R[7] % n8) * 0x2000
            bC000 = (n8 - 2) * 0x2000
            bE000 = (n8 - 1) * 0x2000
        else:
            b8000 = (n8 - 2) * 0x2000
            bA000 = (R[7] % n8) * 0x2000
            bC000 = (R[6] % n8) * 0x2000
            bE000 = (n8 - 1) * 0x2000
        if 0x8000 <= addr < 0xA000:  return prg[(b8000 + addr - 0x8000) % L]
        if 0xA000 <= addr < 0xC000:  return prg[(bA000 + addr - 0xA000) % L]
        if 0xC000 <= addr < 0xE000:  return prg[(bC000 + addr - 0xC000) % L]
        return prg[(bE000 + addr - 0xE000) % L]

    def chr_read(self, addr: int) -> int:
        c = self.rom.chr
        if not c: return 0
        cl = len(c)
        nk = max(1, cl // 1024)
        R = self.r
        a = addr & 0x1FFF
        if self.chr_mode == 0:
            if a < 0x800:       off = ((R[0] & ~1) % nk) * 1024 + (a & 0x7FF)
            elif a < 0x1000:    off = ((R[1] & ~1) % nk) * 1024 + (a & 0x7FF)
            else:
                slot = (a - 0x1000) // 0x400
                off = (R[2 + slot] % nk) * 1024 + (a & 0x3FF)
        else:
            if a < 0x400:
                off = (R[2] % nk) * 1024 + (a & 0x3FF)
            elif a < 0x800:
                off = (R[3] % nk) * 1024 + (a & 0x3FF)
            elif a < 0xC00:
                off = (R[4] % nk) * 1024 + (a & 0x3FF)
            elif a < 0x1000:
                off = (R[5] % nk) * 1024 + (a & 0x3FF)
            elif a < 0x1800:
                off = ((R[0] & ~1) % nk) * 1024 + (a - 0x1000)
            else:
                off = ((R[1] & ~1) % nk) * 1024 + (a - 0x1800)
        return c[off % cl]

    def chr_write(self, addr: int, val: int) -> None:
        if self.rom.chr_banks != 0:
            return
        c = self.rom.chr
        if not c:
            return
        cl = len(c)
        nk = max(1, cl // 1024)
        R = self.r
        a = addr & 0x1FFF
        if self.chr_mode == 0:
            if a < 0x800:
                off = ((R[0] & ~1) % nk) * 1024 + (a & 0x7FF)
            elif a < 0x1000:
                off = ((R[1] & ~1) % nk) * 1024 + (a & 0x7FF)
            else:
                slot = (a - 0x1000) // 0x400
                off = (R[2 + slot] % nk) * 1024 + (a & 0x3FF)
        else:
            if a < 0x400:
                off = (R[2] % nk) * 1024 + (a & 0x3FF)
            elif a < 0x800:
                off = (R[3] % nk) * 1024 + (a & 0x3FF)
            elif a < 0xC00:
                off = (R[4] % nk) * 1024 + (a & 0x3FF)
            elif a < 0x1000:
                off = (R[5] % nk) * 1024 + (a & 0x3FF)
            elif a < 0x1800:
                off = ((R[0] & ~1) % nk) * 1024 + (a - 0x1000)
            else:
                off = ((R[1] & ~1) % nk) * 1024 + (a - 0x1800)
        c[off % cl] = val & 0xFF


class Mapper5(_MapperBase):
    """MMC5 minimal — 8K PRG slots at $5114-$5117 (boot stub for many retail carts)."""
    __slots__ = ("prg",)

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        n8 = max(4, len(rom.prg) // 0x2000)
        self.prg = [0, 1, max(0, n8 - 2), max(0, n8 - 1)]

    def reset(self) -> None:
        n8 = max(4, len(self.rom.prg) // 0x2000)
        self.prg = [0, 1, max(0, n8 - 2), max(0, n8 - 1)]

    def prg_write(self, addr: int, val: int) -> None:
        if 0x5114 <= addr <= 0x5117:
            n8 = max(1, len(self.rom.prg) // 0x2000)
            self.prg[addr - 0x5114] = val & (n8 - 1)

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        L = len(prg)
        n8 = max(1, L // 0x2000)
        if addr < 0xA000:
            b = self.prg[0] % n8
            return prg[(b * 0x2000 + (addr - 0x8000)) % L]
        if addr < 0xC000:
            b = self.prg[1] % n8
            return prg[(b * 0x2000 + (addr - 0xA000)) % L]
        if addr < 0xE000:
            b = self.prg[2] % n8
            return prg[(b * 0x2000 + (addr - 0xC000)) % L]
        b = self.prg[3] % n8
        return prg[(b * 0x2000 + (addr - 0xE000)) % L]


class Mapper11(_MapperBase):
    """Color Dreams — 32K PRG + 8K CHR bank @ $8000 writes."""
    __slots__ = ("prg_bank", "chr_bank")

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.prg_bank = self.chr_bank = 0

    def reset(self) -> None:
        self.prg_bank = self.chr_bank = 0

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        return prg[(self.prg_bank * 0x8000 + (addr - 0x8000)) % len(prg)]

    def prg_write(self, addr: int, val: int) -> None:
        if addr >= 0x8000:
            pn = max(1, len(self.rom.prg) // 0x8000)
            cn = max(1, len(self.rom.chr) // 0x2000) if self.rom.chr else 1
            self.prg_bank = val & (pn - 1)
            self.chr_bank = (val >> 4) & (cn - 1)

    def chr_read(self, addr: int) -> int:
        c = self.rom.chr
        if not c:
            return 0
        return c[(self.chr_bank * 0x2000 + (addr & 0x1FFF)) % len(c)]


class Mapper66(_MapperBase):
    """GxROM — 32K PRG + 8K CHR banks."""
    __slots__ = ("prg_bank", "chr_bank")

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.prg_bank = self.chr_bank = 0

    def reset(self) -> None:
        self.prg_bank = self.chr_bank = 0

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        return prg[(self.prg_bank * 0x8000 + (addr - 0x8000)) % len(prg)]

    def prg_write(self, addr: int, val: int) -> None:
        if addr >= 0x8000:
            pn = max(1, len(self.rom.prg) // 0x8000)
            cn = max(1, len(self.rom.chr) // 0x2000) if self.rom.chr else 1
            self.chr_bank = val & 3 & (cn - 1)
            self.prg_bank = ((val >> 4) & 3) & (pn - 1)

    def chr_read(self, addr: int) -> int:
        c = self.rom.chr
        if not c:
            return 0
        return c[(self.chr_bank * 0x2000 + (addr & 0x1FFF)) % len(c)]


class Mapper30(_MapperBase):
    """UNROM-512 (Mapper 30) — large UxROM."""
    __slots__ = ("bank",)

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.bank = 0

    def reset(self) -> None:
        self.bank = 0

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        L = len(prg)
        if addr < 0xC000:
            return prg[(self.bank * 0x4000 + (addr - 0x8000)) % L]
        return prg[(L - 0x4000 + (addr - 0xC000)) % L]

    def prg_write(self, addr: int, val: int) -> None:
        if addr >= 0x8000:
            n = max(1, len(self.rom.prg) // 0x4000)
            self.bank = val & (n - 1)


class Mapper9(_MapperBase):
    """MMC2 / PxROM — latch CHR (Punch-Out!!)."""
    __slots__ = ("prg_bank", "latch_fd", "latch_fe")

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.reset()

    def reset(self) -> None:
        self.prg_bank = 0
        self.latch_fd = self.latch_fe = 0

    def prg_write(self, addr: int, val: int) -> None:
        if addr < 0x8000:
            return
        val &= 0xFF
        if 0x9000 <= addr < 0xA000 and (addr & 1) == 0:
            self.bus.ppu_mirror = "V" if (val & 1) else "H"
        elif 0xA000 <= addr < 0xB000:
            self.prg_bank = val & 0x0F
        elif 0xB000 <= addr < 0xC000 and (addr & 1):
            self.latch_fd = val & 0x1F
        elif 0xC000 <= addr < 0xD000 and (addr & 1):
            self.latch_fe = val & 0x1F

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        L = len(prg)
        n8 = max(1, L // 0x2000)
        if addr < 0xC000:
            return prg[(self.prg_bank % n8) * 0x2000 + (addr - 0x8000)]
        return prg[(max(0, n8 - 1) * 0x2000 + (addr - 0xC000)) % L]

    def chr_read(self, addr: int) -> int:
        c = self.rom.chr
        if not c:
            return 0
        cl = len(c)
        a = addr & 0x1FFF
        bank = self.latch_fd if a < 0x1000 else self.latch_fe
        return c[(bank * 0x1000 + (a & 0x0FFF)) % cl]


class Mapper10(_MapperBase):
    """MMC4 / FxROM — latch CHR."""
    __slots__ = ("prg_bank", "latch_0", "latch_1", "chr_mode")

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.reset()

    def reset(self) -> None:
        self.prg_bank = 0
        self.latch_0 = self.latch_1 = 0
        self.chr_mode = 0

    def prg_write(self, addr: int, val: int) -> None:
        if addr < 0x8000:
            return
        val &= 0xFF
        if 0x9000 <= addr < 0xA000 and (addr & 1) == 0:
            self.bus.ppu_mirror = "V" if (val & 1) else "H"
        elif 0xA000 <= addr < 0xB000:
            self.prg_bank = val & 0x0F
        elif 0xB000 <= addr < 0xC000 and (addr & 1):
            self.latch_0 = val & 0x1F
        elif 0xC000 <= addr < 0xD000 and (addr & 1):
            self.latch_1 = val & 0x1F
        elif 0xE000 <= addr < 0xF000 and (addr & 1) == 0:
            self.chr_mode = val & 1

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        L = len(prg)
        n8 = max(1, L // 0x2000)
        if addr < 0xC000:
            return prg[(self.prg_bank % n8) * 0x2000 + (addr - 0x8000)]
        return prg[(max(0, n8 - 1) * 0x2000 + (addr - 0xC000)) % L]

    def chr_read(self, addr: int) -> int:
        c = self.rom.chr
        if not c:
            return 0
        cl = len(c)
        nk = max(1, cl // 0x1000)
        a = addr & 0x1FFF
        if self.chr_mode == 0:
            bank = self.latch_0 if a < 0x1000 else self.latch_1
            return c[(bank * 0x1000 + (a & 0x0FFF)) % cl]
        bank = self.latch_0 if a < 0x1000 else self.latch_1
        return c[(bank * 0x1000 + (a & 0x0FFF)) % cl]


# Mapper ID aliases → implementation (covers most retail iNES releases)
_MAPPER_MMC1 = {1, 155}
_MAPPER_MMC2 = {9, 112}
_MAPPER_MMC3 = {
    4, 12, 44, 45, 49, 52, 74, 88, 91, 114, 115, 118, 119, 165,
    191, 192, 193, 194, 195, 209, 215, 249, 250, 254,
}
_MAPPER_MMC5 = {5}
_MAPPER_MMC4 = {10}
_MAPPER_CNROM = {3, 185}
_MAPPER_UNROM = {
    2, 32, 33, 35, 36, 38, 41, 42, 43, 46, 48, 58, 61, 62, 63, 64, 65,
    68, 69, 70, 71, 72, 73, 75, 78, 79, 87, 89, 94, 95, 97, 99, 101,
    107, 108, 109, 113, 140, 152, 154, 156, 159, 180, 185, 206, 207, 245,
}
_MAPPER_AXROM = {7, 34, 47, 58, 93, 146, 147, 149, 196}
_MAPPER_GXROM = {66}
_MAPPER_CDREAMS = {11}
_MAPPER_UNROM512 = {30}


def build_mapper(bus: "Bus", rom: INES) -> _MapperBase:
    m = rom.mapper
    if m in _MAPPER_MMC1:
        return Mapper1(bus, rom)
    if m in _MAPPER_MMC5:
        return Mapper5(bus, rom)
    if m in _MAPPER_MMC2:
        return Mapper9(bus, rom)
    if m in _MAPPER_MMC4:
        return Mapper10(bus, rom)
    if m in _MAPPER_MMC3:
        return Mapper4(bus, rom)
    if m in _MAPPER_CNROM:
        return Mapper3(bus, rom)
    if m in _MAPPER_UNROM512:
        return Mapper30(bus, rom)
    if m in _MAPPER_UNROM:
        return Mapper2(bus, rom)
    if m in _MAPPER_AXROM:
        return Mapper7(bus, rom)
    if m in _MAPPER_GXROM:
        return Mapper66(bus, rom)
    if m in _MAPPER_CDREAMS:
        return Mapper11(bus, rom)
    # Heuristic fallbacks for odd mapper IDs (logged so mis-detection is visible)
    if rom.chr_banks == 0 and rom.prg_banks > 1:
        print(f"[acnesemu] mapper {m} unknown, CHR-RAM + multi-PRG -> UNROM")
        return Mapper2(bus, rom)
    if rom.chr_banks >= 1 and rom.prg_banks == 1:
        print(f"[acnesemu] mapper {m} unknown, fixed PRG + CHR -> CNROM")
        return Mapper3(bus, rom)
    print(f"[acnesemu] mapper {m} unsupported, falling back to NROM")
    return Mapper0(bus, rom)


# =============================================================================
#   Memory Bus
# =============================================================================
class Bus:
    __slots__ = ("ram", "rom", "ppu", "controller", "controller_shift",
                 "mapper", "ppu_mirror")

    def __init__(self):
        self.ram = bytearray(0x0800)
        self.rom: INES | None = None
        self.ppu: "PPU | None" = None
        self.controller = 0
        self.controller_shift = 0
        self.mapper: _MapperBase | None = None
        self.ppu_mirror = "H"

    def attach(self, rom: INES, ppu: "PPU"):
        self.rom = rom
        self.ppu = ppu
        ppu.bus = self
        self.mapper = build_mapper(self, rom)

    def chr_read(self, addr: int) -> int:
        addr &= 0x1FFF
        if self.mapper: return self.mapper.chr_read(addr)
        if not self.rom or not self.rom.chr: return 0
        return self.rom.chr[addr % len(self.rom.chr)]

    def chr_write(self, addr: int, val: int) -> None:
        addr &= 0x1FFF
        val &= 0xFF
        if self.mapper:
            self.mapper.chr_write(addr, val)
            return
        if self.rom and self.rom.chr and self.rom.chr_banks == 0:
            self.rom.chr[addr % len(self.rom.chr)] = val

    def read(self, addr: int) -> int:
        addr &= 0xFFFF
        if addr < 0x2000: return self.ram[addr & 0x07FF]
        if addr < 0x4000: return self.ppu.reg_read(0x2000 + (addr & 7)) if self.ppu else 0
        if addr == 0x4016:
            v = (self.controller_shift & 1)
            self.controller_shift >>= 1
            return v | 0x40
        if addr == 0x4017: return 0x40
        if addr < 0x6000:
            return 0
        if addr < 0x8000:
            if self.mapper:
                return self.mapper.wram_read(addr)
            return 0
        if self.rom is None or self.mapper is None: return 0
        return self.mapper.prg_read(addr)

    def write(self, addr: int, val: int) -> None:
        addr &= 0xFFFF
        val &= 0xFF
        if addr < 0x2000:
            self.ram[addr & 0x07FF] = val
            return
        if addr < 0x4000:
            if self.ppu: self.ppu.reg_write(0x2000 + (addr & 7), val)
            return
        if addr == 0x4014 and self.ppu:
            base = val << 8
            for i in range(256):
                self.ppu.oam[i] = self.read(base + i)
            return
        if addr == 0x4016:
            if val & 1: self.controller_shift = self.controller
            return
        if 0x6000 <= addr < 0x8000:
            if self.mapper:
                self.mapper.wram_write(addr, val)
            return
        if 0x4020 <= addr < 0x6000:
            return
        if addr >= 0x8000 and self.mapper is not None:
            self.mapper.prg_write(addr, val)


# =============================================================================
#   6502 CPU
# =============================================================================
C, Z, I, D, B, U, V, N = 1, 2, 4, 8, 16, 32, 64, 128

class CPU:
    __slots__ = ("a", "x", "y", "sp", "pc", "p", "bus", "cycles", "halted",
                 "_addr", "_page_crossed")

    def __init__(self, bus: Bus):
        self.bus = bus
        self.a = 0; self.x = 0; self.y = 0
        self.sp = 0xFD
        self.pc = 0x0000
        self.p  = U | I
        self.cycles = 0
        self.halted = False
        self._addr = 0
        self._page_crossed = False

    def r(self, a):    return self.bus.read(a)
    def w(self, a, v): self.bus.write(a, v)

    def r16(self, a):
        return self.r(a) | (self.r((a + 1) & 0xFFFF) << 8)

    def push(self, v):
        self.w(0x100 + self.sp, v & 0xFF)
        self.sp = (self.sp - 1) & 0xFF

    def pop(self):
        self.sp = (self.sp + 1) & 0xFF
        return self.r(0x100 + self.sp)

    def setF(self, flag, on):
        if on: self.p |=  flag
        else:  self.p &= ~flag & 0xFF

    def setNZ(self, v):
        v &= 0xFF
        self.setF(Z, v == 0)
        self.setF(N, v & 0x80)

    def reset(self):
        self.sp = 0xFD
        self.p  = U | I
        self.a = self.x = self.y = 0
        self.pc = self.r16(0xFFFC)
        self.cycles = 0
        self.halted = False

    def nmi(self):
        self.push((self.pc >> 8) & 0xFF)
        self.push(self.pc & 0xFF)
        self.push((self.p | U) & ~B & 0xFF)
        self.setF(I, True)
        self.pc = self.r16(0xFFFA)
        self.cycles += 7

    def irq(self):
        if self.p & I: return
        self.push((self.pc >> 8) & 0xFF)
        self.push(self.pc & 0xFF)
        self.push((self.p | U) & ~B & 0xFF)
        self.setF(I, True)
        self.pc = self.r16(0xFFFE)
        self.cycles += 7

    # Addressing modes
    def am_imp(self): pass
    def am_acc(self): pass
    def am_imm(self): self._addr = self.pc; self.pc = (self.pc + 1) & 0xFFFF
    def am_zp (self): self._addr = self.r(self.pc); self.pc = (self.pc + 1) & 0xFFFF
    def am_zpx(self): self._addr = (self.r(self.pc) + self.x) & 0xFF; self.pc = (self.pc + 1) & 0xFFFF
    def am_zpy(self): self._addr = (self.r(self.pc) + self.y) & 0xFF; self.pc = (self.pc + 1) & 0xFFFF
    def am_abs(self):
        self._addr = self.r16(self.pc); self.pc = (self.pc + 2) & 0xFFFF
    def am_abx(self):
        base = self.r16(self.pc); self.pc = (self.pc + 2) & 0xFFFF
        self._addr = (base + self.x) & 0xFFFF
    def am_aby(self):
        base = self.r16(self.pc); self.pc = (self.pc + 2) & 0xFFFF
        self._addr = (base + self.y) & 0xFFFF
    def am_ind(self):
        ptr = self.r16(self.pc); self.pc = (self.pc + 2) & 0xFFFF
        if (ptr & 0xFF) == 0xFF:
            self._addr = self.r(ptr) | (self.r(ptr & 0xFF00) << 8)
        else:
            self._addr = self.r16(ptr)
    def am_izx(self):
        z = (self.r(self.pc) + self.x) & 0xFF
        self.pc = (self.pc + 1) & 0xFFFF
        self._addr = self.r(z) | (self.r((z + 1) & 0xFF) << 8)
    def am_izy(self):
        z = self.r(self.pc); self.pc = (self.pc + 1) & 0xFFFF
        base = self.r(z) | (self.r((z + 1) & 0xFF) << 8)
        self._addr = (base + self.y) & 0xFFFF
    def am_rel(self):
        off = self.r(self.pc); self.pc = (self.pc + 1) & 0xFFFF
        if off & 0x80: off -= 0x100
        self._addr = (self.pc + off) & 0xFFFF

    # Operations
    def op_lda(self): self.a = self.r(self._addr); self.setNZ(self.a)
    def op_ldx(self): self.x = self.r(self._addr); self.setNZ(self.x)
    def op_ldy(self): self.y = self.r(self._addr); self.setNZ(self.y)
    def op_sta(self): self.w(self._addr, self.a)
    def op_stx(self): self.w(self._addr, self.x)
    def op_sty(self): self.w(self._addr, self.y)
    def op_tax(self): self.x = self.a; self.setNZ(self.x)
    def op_tay(self): self.y = self.a; self.setNZ(self.y)
    def op_txa(self): self.a = self.x; self.setNZ(self.a)
    def op_tya(self): self.a = self.y; self.setNZ(self.a)
    def op_tsx(self): self.x = self.sp; self.setNZ(self.x)
    def op_txs(self): self.sp = self.x
    def op_pha(self): self.push(self.a)
    def op_php(self): self.push(self.p | B | U)
    def op_pla(self): self.a = self.pop(); self.setNZ(self.a)
    def op_plp(self): self.p = (self.pop() | U) & ~B & 0xFF

    def op_and(self): self.a &= self.r(self._addr); self.setNZ(self.a)
    def op_ora(self): self.a |= self.r(self._addr); self.setNZ(self.a)
    def op_eor(self): self.a ^= self.r(self._addr); self.setNZ(self.a)

    def op_bit(self):
        m = self.r(self._addr)
        self.setF(Z, (self.a & m) == 0)
        self.setF(V, m & 0x40)
        self.setF(N, m & 0x80)

    def op_adc(self):
        m = self.r(self._addr)
        s = self.a + m + (1 if self.p & C else 0)
        self.setF(C, s > 0xFF)
        self.setF(V, (~(self.a ^ m) & (self.a ^ s) & 0x80) != 0)
        self.a = s & 0xFF
        self.setNZ(self.a)

    def op_sbc(self):
        m = self.r(self._addr) ^ 0xFF
        s = self.a + m + (1 if self.p & C else 0)
        self.setF(C, s > 0xFF)
        self.setF(V, (~(self.a ^ m) & (self.a ^ s) & 0x80) != 0)
        self.a = s & 0xFF
        self.setNZ(self.a)

    def _cmp(self, reg):
        m = self.r(self._addr)
        d = (reg - m) & 0x1FF
        self.setF(C, reg >= m)
        self.setNZ(d & 0xFF)
    def op_cmp(self): self._cmp(self.a)
    def op_cpx(self): self._cmp(self.x)
    def op_cpy(self): self._cmp(self.y)

    def op_inc(self):
        v = (self.r(self._addr) + 1) & 0xFF
        self.w(self._addr, v); self.setNZ(v)
    def op_dec(self):
        v = (self.r(self._addr) - 1) & 0xFF
        self.w(self._addr, v); self.setNZ(v)
    def op_inx(self): self.x = (self.x + 1) & 0xFF; self.setNZ(self.x)
    def op_iny(self): self.y = (self.y + 1) & 0xFF; self.setNZ(self.y)
    def op_dex(self): self.x = (self.x - 1) & 0xFF; self.setNZ(self.x)
    def op_dey(self): self.y = (self.y - 1) & 0xFF; self.setNZ(self.y)

    def op_asl_a(self):
        self.setF(C, self.a & 0x80); self.a = (self.a << 1) & 0xFF; self.setNZ(self.a)
    def op_asl(self):
        m = self.r(self._addr); self.setF(C, m & 0x80)
        m = (m << 1) & 0xFF;    self.w(self._addr, m); self.setNZ(m)
    def op_lsr_a(self):
        self.setF(C, self.a & 1);  self.a >>= 1;       self.setNZ(self.a)
    def op_lsr(self):
        m = self.r(self._addr); self.setF(C, m & 1)
        m >>= 1;                self.w(self._addr, m); self.setNZ(m)
    def op_rol_a(self):
        c = 1 if self.p & C else 0
        self.setF(C, self.a & 0x80)
        self.a = ((self.a << 1) | c) & 0xFF; self.setNZ(self.a)
    def op_rol(self):
        m = self.r(self._addr); c = 1 if self.p & C else 0
        self.setF(C, m & 0x80)
        m = ((m << 1) | c) & 0xFF; self.w(self._addr, m); self.setNZ(m)
    def op_ror_a(self):
        c = 0x80 if self.p & C else 0
        self.setF(C, self.a & 1)
        self.a = (self.a >> 1) | c; self.setNZ(self.a)
    def op_ror(self):
        m = self.r(self._addr); c = 0x80 if self.p & C else 0
        self.setF(C, m & 1)
        m = (m >> 1) | c; self.w(self._addr, m); self.setNZ(m)

    def op_jmp(self): self.pc = self._addr
    def op_jsr(self):
        ret = (self.pc - 1) & 0xFFFF
        self.push((ret >> 8) & 0xFF); self.push(ret & 0xFF)
        self.pc = self._addr
    def op_rts(self):
        lo = self.pop(); hi = self.pop()
        self.pc = ((hi << 8) | lo) + 1
    def op_rti(self):
        self.p = (self.pop() | U) & ~B & 0xFF
        lo = self.pop(); hi = self.pop()
        self.pc = (hi << 8) | lo
    def op_brk(self):
        self.pc = (self.pc + 1) & 0xFFFF
        self.push((self.pc >> 8) & 0xFF); self.push(self.pc & 0xFF)
        self.push(self.p | B | U)
        self.setF(I, True)
        self.pc = self.r16(0xFFFE)

    def _branch(self, cond: bool):
        if cond:
            old = self.pc
            self.cycles += 1
            self.pc = self._addr
            if (old & 0xFF00) != (self.pc & 0xFF00):
                self.cycles += 1

    def op_bpl(self): self._branch((self.p & N) == 0)
    def op_bmi(self): self._branch((self.p & N) != 0)
    def op_bvc(self): self._branch((self.p & V) == 0)
    def op_bvs(self): self._branch((self.p & V) != 0)
    def op_bcc(self): self._branch((self.p & C) == 0)
    def op_bcs(self): self._branch((self.p & C) != 0)
    def op_bne(self): self._branch((self.p & Z) == 0)
    def op_beq(self): self._branch((self.p & Z) != 0)

    def op_clc(self): self.setF(C, False)
    def op_sec(self): self.setF(C, True)
    def op_cli(self): self.setF(I, False)
    def op_sei(self): self.setF(I, True)
    def op_clv(self): self.setF(V, False)
    def op_cld(self): self.setF(D, False)
    def op_sed(self): self.setF(D, True)
    def op_nop(self): pass

    # --- unofficial opcodes (wrong size NOPs garble VRAM / on-screen text) ---
    def op_lax(self):
        self.a = self.r(self._addr)
        self.x = self.a
        self.setNZ(self.a)

    def op_sax(self):
        self.w(self._addr, self.a & self.x)

    def op_slo(self):
        m = self.r(self._addr)
        self.setF(C, m & 0x80)
        m = (m << 1) & 0xFF
        self.w(self._addr, m)
        self.a |= m
        self.setNZ(self.a)

    def op_rla(self):
        m = self.r(self._addr)
        c = 1 if self.p & C else 0
        self.setF(C, m & 0x80)
        m = ((m << 1) | c) & 0xFF
        self.w(self._addr, m)
        self.a &= m
        self.setNZ(self.a)

    def op_sre(self):
        m = self.r(self._addr)
        self.setF(C, m & 1)
        m >>= 1
        self.w(self._addr, m)
        self.a ^= m
        self.setNZ(self.a)

    def op_rra(self):
        m = self.r(self._addr)
        c = 0x80 if self.p & C else 0
        self.setF(C, m & 1)
        m = (m >> 1) | c
        self.w(self._addr, m)
        s = self.a + m + (1 if self.p & C else 0)
        self.setF(C, s > 0xFF)
        self.setF(V, (~(self.a ^ m) & (self.a ^ s) & 0x80) != 0)
        self.a = s & 0xFF
        self.setNZ(self.a)

    def op_dcp(self):
        v = (self.r(self._addr) - 1) & 0xFF
        self.w(self._addr, v)
        self.setF(C, self.a >= v)
        self.setNZ((self.a - v) & 0xFF)

    def op_isb(self):
        v = (self.r(self._addr) + 1) & 0xFF
        self.w(self._addr, v)
        m = v ^ 0xFF
        s = self.a + m + (1 if self.p & C else 0)
        self.setF(C, s > 0xFF)
        self.setF(V, (~(self.a ^ m) & (self.a ^ s) & 0x80) != 0)
        self.a = s & 0xFF
        self.setNZ(self.a)

    def op_anc(self):
        self.a &= self.r(self._addr)
        self.setF(C, self.a & 0x80)
        self.setNZ(self.a)

    def op_alr(self):
        self.a &= self.r(self._addr)
        self.setF(C, self.a & 1)
        self.a >>= 1
        self.setNZ(self.a)

    def op_arr(self):
        self.a &= self.r(self._addr)
        c = self.p & C
        self.a = ((self.a >> 1) | (0x80 if c else 0)) & 0xFF
        self.setF(C, (self.a >> 6) & 1)
        self.setNZ(self.a)

    def op_sbx(self):
        m = self.r(self._addr)
        t = (self.x & self.a) - m
        self.x = t & 0xFF
        self.setF(C, t >= 0)
        self.setNZ(self.x)

    _table = None
    # Extra operand bytes after opcode for unknown ops (keeps PC aligned)
    _UNK_EXTRA = {
        0x12: 0, 0x22: 0, 0x32: 0, 0x42: 0, 0x52: 0, 0x62: 0, 0x72: 0,
        0x80: 1, 0x82: 1, 0x89: 1, 0x92: 2, 0x93: 2, 0x9B: 2, 0x9C: 2,
        0x9E: 2, 0x9F: 2, 0xB2: 1, 0xBB: 3, 0xC2: 1, 0xD2: 1, 0xDB: 3,
        0xE2: 1, 0xF2: 1, 0xF7: 1, 0xFF: 3,
    }

    def step(self):
        if self.halted:
            return 0
        op = self.r(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        entry = CPU._table[op]
        if entry is None:
            extra = CPU._UNK_EXTRA.get(op, 0)
            self.pc = (self.pc + extra) & 0xFFFF
            self.cycles += 2 + extra
            return 2 + extra
        am, fn, cyc = entry
        am(self)
        fn(self)
        self.cycles += cyc
        return cyc

def _build_table():
    t = [None] * 256
    C = CPU
    def s(code, am, op, cyc): t[code] = (getattr(C, am), getattr(C, op), cyc)

    # Loads
    s(0xA9,"am_imm","op_lda",2); s(0xA5,"am_zp", "op_lda",3)
    s(0xB5,"am_zpx","op_lda",4); s(0xAD,"am_abs","op_lda",4)
    s(0xBD,"am_abx","op_lda",4); s(0xB9,"am_aby","op_lda",4)
    s(0xA1,"am_izx","op_lda",6); s(0xB1,"am_izy","op_lda",5)
    s(0xA2,"am_imm","op_ldx",2); s(0xA6,"am_zp", "op_ldx",3)
    s(0xB6,"am_zpy","op_ldx",4); s(0xAE,"am_abs","op_ldx",4)
    s(0xBE,"am_aby","op_ldx",4)
    s(0xA0,"am_imm","op_ldy",2); s(0xA4,"am_zp", "op_ldy",3)
    s(0xB4,"am_zpx","op_ldy",4); s(0xAC,"am_abs","op_ldy",4)
    s(0xBC,"am_abx","op_ldy",4)
    # Stores
    s(0x85,"am_zp", "op_sta",3); s(0x95,"am_zpx","op_sta",4)
    s(0x8D,"am_abs","op_sta",4); s(0x9D,"am_abx","op_sta",5)
    s(0x99,"am_aby","op_sta",5); s(0x81,"am_izx","op_sta",6)
    s(0x91,"am_izy","op_sta",6)
    s(0x86,"am_zp", "op_stx",3); s(0x96,"am_zpy","op_stx",4)
    s(0x8E,"am_abs","op_stx",4)
    s(0x84,"am_zp", "op_sty",3); s(0x94,"am_zpx","op_sty",4)
    s(0x8C,"am_abs","op_sty",4)
    # Transfers
    s(0xAA,"am_imp","op_tax",2); s(0xA8,"am_imp","op_tay",2)
    s(0x8A,"am_imp","op_txa",2); s(0x98,"am_imp","op_tya",2)
    s(0xBA,"am_imp","op_tsx",2); s(0x9A,"am_imp","op_txs",2)
    # Stack
    s(0x48,"am_imp","op_pha",3); s(0x08,"am_imp","op_php",3)
    s(0x68,"am_imp","op_pla",4); s(0x28,"am_imp","op_plp",4)
    # Logic
    s(0x29,"am_imm","op_and",2); s(0x25,"am_zp", "op_and",3)
    s(0x35,"am_zpx","op_and",4); s(0x2D,"am_abs","op_and",4)
    s(0x3D,"am_abx","op_and",4); s(0x39,"am_aby","op_and",4)
    s(0x21,"am_izx","op_and",6); s(0x31,"am_izy","op_and",5)
    s(0x09,"am_imm","op_ora",2); s(0x05,"am_zp", "op_ora",3)
    s(0x15,"am_zpx","op_ora",4); s(0x0D,"am_abs","op_ora",4)
    s(0x1D,"am_abx","op_ora",4); s(0x19,"am_aby","op_ora",4)
    s(0x01,"am_izx","op_ora",6); s(0x11,"am_izy","op_ora",5)
    s(0x49,"am_imm","op_eor",2); s(0x45,"am_zp", "op_eor",3)
    s(0x55,"am_zpx","op_eor",4); s(0x4D,"am_abs","op_eor",4)
    s(0x5D,"am_abx","op_eor",4); s(0x59,"am_aby","op_eor",4)
    s(0x41,"am_izx","op_eor",6); s(0x51,"am_izy","op_eor",5)
    s(0x24,"am_zp", "op_bit",3); s(0x2C,"am_abs","op_bit",4)
    # Arithmetic
    s(0x69,"am_imm","op_adc",2); s(0x65,"am_zp", "op_adc",3)
    s(0x75,"am_zpx","op_adc",4); s(0x6D,"am_abs","op_adc",4)
    s(0x7D,"am_abx","op_adc",4); s(0x79,"am_aby","op_adc",4)
    s(0x61,"am_izx","op_adc",6); s(0x71,"am_izy","op_adc",5)
    s(0xE9,"am_imm","op_sbc",2); s(0xE5,"am_zp", "op_sbc",3)
    s(0xF5,"am_zpx","op_sbc",4); s(0xED,"am_abs","op_sbc",4)
    s(0xFD,"am_abx","op_sbc",4); s(0xF9,"am_aby","op_sbc",4)
    s(0xE1,"am_izx","op_sbc",6); s(0xF1,"am_izy","op_sbc",5)
    # Compares
    s(0xC9,"am_imm","op_cmp",2); s(0xC5,"am_zp", "op_cmp",3)
    s(0xD5,"am_zpx","op_cmp",4); s(0xCD,"am_abs","op_cmp",4)
    s(0xDD,"am_abx","op_cmp",4); s(0xD9,"am_aby","op_cmp",4)
    s(0xC1,"am_izx","op_cmp",6); s(0xD1,"am_izy","op_cmp",5)
    s(0xE0,"am_imm","op_cpx",2); s(0xE4,"am_zp", "op_cpx",3); s(0xEC,"am_abs","op_cpx",4)
    s(0xC0,"am_imm","op_cpy",2); s(0xC4,"am_zp", "op_cpy",3); s(0xCC,"am_abs","op_cpy",4)
    # Inc/Dec
    s(0xE6,"am_zp", "op_inc",5); s(0xF6,"am_zpx","op_inc",6)
    s(0xEE,"am_abs","op_inc",6); s(0xFE,"am_abx","op_inc",7)
    s(0xC6,"am_zp", "op_dec",5); s(0xD6,"am_zpx","op_dec",6)
    s(0xCE,"am_abs","op_dec",6); s(0xDE,"am_abx","op_dec",7)
    s(0xE8,"am_imp","op_inx",2); s(0xC8,"am_imp","op_iny",2)
    s(0xCA,"am_imp","op_dex",2); s(0x88,"am_imp","op_dey",2)
    # Shifts
    s(0x0A,"am_acc","op_asl_a",2); s(0x06,"am_zp", "op_asl",5)
    s(0x16,"am_zpx","op_asl",6);   s(0x0E,"am_abs","op_asl",6)
    s(0x1E,"am_abx","op_asl",7)
    s(0x4A,"am_acc","op_lsr_a",2); s(0x46,"am_zp", "op_lsr",5)
    s(0x56,"am_zpx","op_lsr",6);   s(0x4E,"am_abs","op_lsr",6)
    s(0x5E,"am_abx","op_lsr",7)
    s(0x2A,"am_acc","op_rol_a",2); s(0x26,"am_zp", "op_rol",5)
    s(0x36,"am_zpx","op_rol",6);   s(0x2E,"am_abs","op_rol",6)
    s(0x3E,"am_abx","op_rol",7)
    s(0x6A,"am_acc","op_ror_a",2); s(0x66,"am_zp", "op_ror",5)
    s(0x76,"am_zpx","op_ror",6);   s(0x6E,"am_abs","op_ror",6)
    s(0x7E,"am_abx","op_ror",7)
    # Jumps
    s(0x4C,"am_abs","op_jmp",3); s(0x6C,"am_ind","op_jmp",5)
    s(0x20,"am_abs","op_jsr",6); s(0x60,"am_imp","op_rts",6)
    s(0x40,"am_imp","op_rti",6); s(0x00,"am_imp","op_brk",7)
    # Branches
    s(0x10,"am_rel","op_bpl",2); s(0x30,"am_rel","op_bmi",2)
    s(0x50,"am_rel","op_bvc",2); s(0x70,"am_rel","op_bvs",2)
    s(0x90,"am_rel","op_bcc",2); s(0xB0,"am_rel","op_bcs",2)
    s(0xD0,"am_rel","op_bne",2); s(0xF0,"am_rel","op_beq",2)
    # Flags + NOP
    s(0x18,"am_imp","op_clc",2); s(0x38,"am_imp","op_sec",2)
    s(0x58,"am_imp","op_cli",2); s(0x78,"am_imp","op_sei",2)
    s(0xB8,"am_imp","op_clv",2); s(0xD8,"am_imp","op_cld",2)
    s(0xF8,"am_imp","op_sed",2); s(0xEA,"am_imp","op_nop",2)

    # Unofficial opcodes — must use correct addressing + size (not 1-byte NOPs)
    def u(code, am, op, cyc):
        if t[code] is None:
            s(code, am, op, cyc)

    # SLO (ASL + ORA)
    for c, am, cy in (
        (0x03,"am_izx",8),(0x07,"am_izy",5),(0x0F,"am_abs",4),(0x13,"am_izy",5),
        (0x17,"am_izy",5),(0x1B,"am_aby",4),(0x1F,"am_aby",4),
    ):
        u(c, am, "op_slo", cy)
    # RLA (ROL + AND)
    for c, am, cy in (
        (0x23,"am_izx",8),(0x27,"am_izy",5),(0x2F,"am_abs",4),(0x33,"am_izy",5),
        (0x37,"am_aby",4),(0x3B,"am_aby",4),(0x3F,"am_aby",4),
    ):
        u(c, am, "op_rla", cy)
    # SRE (LSR + EOR)
    for c, am, cy in (
        (0x43,"am_izx",8),(0x47,"am_izy",5),(0x4F,"am_abs",4),(0x53,"am_izy",5),
        (0x57,"am_aby",4),(0x5B,"am_aby",4),(0x5F,"am_aby",4),
    ):
        u(c, am, "op_sre", cy)
    # RRA (ROR + ADC)
    for c, am, cy in (
        (0x63,"am_izx",8),(0x67,"am_izy",5),(0x6F,"am_abs",4),(0x73,"am_izy",5),
        (0x77,"am_aby",4),(0x7B,"am_aby",4),(0x7F,"am_aby",4),
    ):
        u(c, am, "op_rra", cy)
    # SAX (store A & X)
    for c, am, cy in (
        (0x83,"am_izx",6),(0x87,"am_izy",5),(0x8F,"am_abs",4),(0x97,"am_izy",5),
    ):
        u(c, am, "op_sax", cy)
    # LAX (load A and X)
    for c, am, cy in (
        (0xA3,"am_izx",6),(0xA7,"am_imm",2),(0xAF,"am_abs",4),(0xB3,"am_izy",5),
        (0xB7,"am_zpy",4),(0xBF,"am_aby",4),
    ):
        u(c, am, "op_lax", cy)
    # DCP (DEC + CMP)
    for c, am, cy in (
        (0xC3,"am_izx",8),(0xC7,"am_izy",5),(0xCF,"am_abs",4),(0xD3,"am_izy",5),
        (0xD7,"am_zpx",6),(0xDF,"am_aby",4),
    ):
        u(c, am, "op_dcp", cy)
    # ISB (INC + SBC)
    for c, am, cy in (
        (0xE3,"am_izx",8),(0xE7,"am_izy",5),(0xEF,"am_abs",4),(0xF3,"am_izy",5),
        (0xFB,"am_aby",4),
    ):
        u(c, am, "op_isb", cy)
    # Misc immediate / absolute unofficial
    u(0x0B, "am_imm", "op_anc", 2)
    u(0x2B, "am_imm", "op_anc", 2)
    u(0x4B, "am_imm", "op_alr", 2)
    u(0x6B, "am_imm", "op_arr", 2)
    u(0xAB, "am_imm", "op_lax", 2)
    u(0xEB, "am_imm", "op_sbc", 2)   # USBC immediate
    u(0xCB, "am_imm", "op_sbx", 2)   # SBX — needs op_sbx

    # Remaining gaps: 1-byte NOPs only where truly 1 byte
    for code in (0x02, 0x1A, 0x3A, 0x5A, 0x7A, 0xDA, 0xFA):
        u(code, "am_imp", "op_nop", 2)
    for code in (0x04, 0x44, 0x64, 0x14, 0x34, 0x54, 0x74, 0xD4, 0xF4):
        u(code, "am_imm", "op_nop", 2)
    for code in (0x0C, 0x1C, 0x3C, 0x5C, 0x7C, 0xDC, 0xFC):
        u(code, "am_abs", "op_nop", 3)
    return t
CPU._table = _build_table()


# =============================================================================
#   PPU Core
# =============================================================================
NES_PALETTE = [
    (84,84,84),(0,30,116),(8,16,144),(48,0,136),(68,0,100),(92,0,48),
    (84,4,0),(60,24,0),(32,42,0),(8,58,0),(0,64,0),(0,60,0),(0,50,60),
    (0,0,0),(0,0,0),(0,0,0),
    (152,150,152),(8,76,196),(48,50,236),(92,30,228),(136,20,176),
    (160,20,100),(152,34,32),(120,60,0),(84,90,0),(40,114,0),(8,124,0),
    (0,118,40),(0,102,120),(0,0,0),(0,0,0),(0,0,0),
    (236,238,236),(76,154,236),(120,124,236),(176,98,236),(228,84,236),
    (236,88,180),(236,106,100),(212,136,32),(160,170,0),(116,196,0),
    (76,208,32),(56,204,108),(56,180,204),(60,60,60),(0,0,0),(0,0,0),
    (236,238,236),(168,204,236),(188,188,236),(212,178,236),(236,174,236),
    (236,174,212),(236,180,176),(228,196,144),(204,210,120),(180,222,120),
    (168,226,144),(152,226,180),(160,214,228),(160,162,160),(0,0,0),(0,0,0),
]
PAL_RGB = bytes(b for rgb in NES_PALETTE for b in rgb)

class PPU:
    __slots__ = (
        "oam", "regs", "rom", "palette", "nametable", "v", "t", "w", "x",
        "_fine_x", "oam_addr", "read_buffer", "_vblank", "_sprite0_hit", "_nmi_sent",
        "_snap_v", "_snap_fine_x", "_snap_ctrl", "_snap_mask",
        "nes", "bus",
    )

    def __init__(self):
        self.oam = bytearray(256)
        self.regs = bytearray(8)
        self.rom: INES | None = None
        self.palette = bytearray(32)
        self.nametable = bytearray(2048)
        self.v = self.t = 0
        self.w = 0
        self.x = 0
        self._fine_x = 0
        self.oam_addr = 0
        self.read_buffer = 0
        self._vblank = False
        self._sprite0_hit = False
        self._nmi_sent = False
        self._snap_v = self._snap_fine_x = 0
        self._snap_ctrl = self._snap_mask = 0
        self.nes: "NES | None" = None
        self.bus: "Bus | None" = None

    def attach(self, rom: INES): self.rom = rom
    def bind_nes(self, nes: "NES") -> None: self.nes = nes

    def reset(self) -> None:
        self.oam[:] = [0] * 256
        self.regs[:] = [0] * 8
        self.palette[:] = [0] * 32
        self.nametable[:] = [0] * 2048
        self.v = self.t = self.w = self.x = self._fine_x = self.oam_addr = self.read_buffer = 0
        self._vblank = self._sprite0_hit = False
        self._nmi_sent = False
        self._snap_v = self._snap_fine_x = 0
        self._snap_ctrl = self._snap_mask = 0

    def _nt_phys(self, addr: int) -> int:
        addr = (addr - 0x2000) & 0x0FFF
        tb = addr // 0x400
        off = addr & 0x3FF
        m = self.bus.ppu_mirror if self.bus else (self.rom.mirror if self.rom else "H")
        if m in ("0", "1"): return (0x400 if m == "1" else 0) + off
        if m == "H":        return (tb // 2) * 0x400 + off
        return (tb & 1) * 0x400 + off

    def _pal_addr(self, a: int) -> int:
        a &= 0x1F
        if (a & 0x13) == 0x10: a &= 0x0F
        return a

    def ppu_read(self, a: int) -> int:
        a &= 0x3FFF
        if a < 0x2000:
            if self.bus: return self.bus.chr_read(a)
            if not self.rom or not self.rom.chr: return 0
            return self.rom.chr[a & (len(self.rom.chr) - 1)]
        if a < 0x3F00: return self.nametable[self._nt_phys(a)]
        return self.palette[self._pal_addr(a)] & 0x3F

    def ppu_write(self, a: int, val: int) -> None:
        a &= 0x3FFF
        val &= 0xFF
        if a < 0x2000:
            if self.bus: self.bus.chr_write(a, val)
            elif self.rom and self.rom.chr and self.rom.chr_banks == 0:
                self.rom.chr[a & (len(self.rom.chr) - 1)] = val
            return
        if a < 0x3F00:
            self.nametable[self._nt_phys(a)] = val
            return
        self.palette[self._pal_addr(a)] = val

    def reg_read(self, addr: int) -> int:
        r = addr & 7
        if r == 2:
            out = (self.regs[2] & 0x1F)
            if self._vblank:       out |= 0x80
            if self._sprite0_hit:  out |= 0x40
            self._vblank = False
            self._sprite0_hit = False
            self.w = 0
            return out & 0xFF
        if r == 4: return self.oam[self.oam_addr]
        if r == 7:
            tmp = self.v & 0x3FFF
            inc = 32 if (self.regs[0] & 4) else 1
            if tmp < 0x3F00:
                ret = self.read_buffer
                self.read_buffer = self.ppu_read(tmp)
            else:
                ret = self.ppu_read(tmp)
                self.read_buffer = self.ppu_read(tmp - 0x1000)
            self.v = (self.v + inc) & 0x7FFF
            return ret & 0xFF
        return self.regs[r] & 0xFF

    def reg_write(self, addr: int, val: int) -> None:
        r = addr & 7
        val &= 0xFF
        prev = self.regs[r]
        self.regs[r] = val
        if r == 0:
            self.t = (self.t & 0xF3FF) | ((val & 0x03) << 10)
            if (val & 0x80) and not (prev & 0x80) and self._vblank:
                self._fire_nmi()
        elif r == 3: self.oam_addr = val
        elif r == 4:
            self.oam[self.oam_addr] = val
            self.oam_addr = (self.oam_addr + 1) & 0xFF
        elif r == 5:
            if self.w == 0:
                self.t = (self.t & 0xFFE0) | (val >> 3)
                self._fine_x = val & 7
                self.w = 1
            else:
                self.t = (self.t & 0x8FFF) | ((val & 0x07) << 12)
                self.t = (self.t & 0xFC1F) | ((val & 0xF8) << 2)
                self.w = 0
        elif r == 6:
            if self.w == 0:
                self.t = (self.t & 0x80FF) | ((val & 0x3F) << 8)
                self.w = 1
            else:
                self.t = (self.t & 0xFF00) | val
                self.v = self.t
                self.w = 0
        elif r == 7:
            self.ppu_write(self.v, val)
            inc = 32 if (self.regs[0] & 4) else 1
            self.v = (self.v + inc) & 0x7FFF

    def begin_frame(self) -> None:
        self._vblank = False
        self._sprite0_hit = False
        self._nmi_sent = False

    def _fire_nmi(self) -> None:
        if self.nes and (self.regs[0] & 0x80) and not self._nmi_sent:
            self.nes.cpu.nmi()
            self._nmi_sent = True

    def snapshot_scroll(self) -> None:
        self._snap_v = self.v
        self._snap_fine_x = self._fine_x
        self._snap_ctrl = self.regs[0]
        self._snap_mask = self.regs[1]

    def compute_sprite0_hit_cycle(self):
        y = self.oam[0]
        if y >= 0xEF:
            return None
        x = self.oam[3]
        cyc = int((y + 1) * 113.667 + x * 0.34 + 20)
        return max(100, min(cyc, 2260))

    def end_frame(self) -> None:
        self._vblank = True
        self._fire_nmi()

    def _chr_byte(self, off: int) -> int:
        off &= 0x1FFF
        if self.bus: return self.bus.chr_read(off)
        if self.rom and self.rom.chr: return self.rom.chr[off % len(self.rom.chr)]
        return 0

    def decode_tile(self, table: int, tile_idx: int, palette_idx: int = 0):
        if not self.rom and not self.bus: return [(0, 0, 0)] * 64
        base = (table & 1) * 0x1000 + (tile_idx & 0xFF) * 16
        out = []
        pal = (palette_idx & 3) * 4
        for row in range(8):
            lo = self._chr_byte(base + row)
            hi = self._chr_byte(base + row + 8)
            for col in range(8):
                bit = ((lo >> (7 - col)) & 1) | (((hi >> (7 - col)) & 1) << 1)
                if bit == 0: out.append(NES_PALETTE[0x0F])
                else:        out.append(NES_PALETTE[(pal + bit) & 0x3F])
        return out

    def _scroll_xy(self):
        v = self._snap_v
        sx = (v & 0x1F) * 8 + self._snap_fine_x
        sy = ((v >> 5) & 0x1F) * 8 + ((v >> 12) & 7)
        sx += ((v >> 10) & 1) * 256
        sy += ((v >> 11) & 1) * 240
        return sx, sy

    def _get_bg_tile(self, table: int, tile_id: int, subpal: int):
        """8x8 tile: palette indices (64) + raw pattern values (64)."""
        base = (table & 1) * 0x1000 + (tile_id & 0xFF) * 16
        bd = self.palette[0] & 0x3F
        pal_base = (subpal & 3) << 2
        col = bytearray(64)
        raw = bytearray(64)
        for row in range(8):
            lo = self._chr_byte(base + row)
            hi = self._chr_byte(base + row + 8)
            ro = row * 8
            for c in range(8):
                bit = ((lo >> (7 - c)) & 1) | (((hi >> (7 - c)) & 1) << 1)
                raw[ro + c] = bit
                col[ro + c] = bd if bit == 0 else (self.palette[self._pal_addr(pal_base + bit)] & 0x3F)
        return bytes(col), bytes(raw)

    def render_rgb(self) -> bytes | None:
        if not self.rom:
            return None
        ctrl = self._snap_ctrl
        mask = self._snap_mask
        bg_on = mask & 0x08
        sp_on = mask & 0x10
        bg_tbl = 1 if (ctrl & 0x10) else 0
        sp_base = 0x1000 if (ctrl & 0x08) else 0
        tall = bool(ctrl & 0x20)
        sp_h = 16 if tall else 8

        W, H = 256, 240
        buf = bytearray(W * H * 3)
        bg_pix = bytearray(W * H)
        bd_idx = self.palette[0] & 0x3F
        bd_r, bd_g, bd_b = NES_PALETTE[bd_idx]

        sx0, sy0 = self._scroll_xy()
        nt = self.nametable
        nt_phys = self._nt_phys

        if bg_on:
            for py in range(H):
                wy = (py + sy0) % 480
                nt_y = wy // 240
                ly = wy % 240
                tr = ly >> 3
                fy = ly & 7
                coarse_sx = sx0 >> 3
                fine_x = sx0 & 7
                line_rgb = bytearray(264 * 3)
                line_raw = bytearray(264)
                for tcv in range(33):
                    wtc = coarse_sx + tcv
                    nt_x = (wtc >> 5) & 1
                    tc = wtc & 0x1F
                    nt_i = nt_y * 2 + nt_x
                    nt_base = nt_i * 0x400
                    tile_id = nt[nt_phys(0x2000 + nt_base + tr * 32 + tc)]
                    attr = nt[nt_phys(0x2000 + nt_base + 0x3C0 + (tr >> 2) * 8 + (tc >> 2))]
                    shift = ((tr & 2) << 1) | (tc & 2)
                    subpal = (attr >> shift) & 3
                    col, raw = self._get_bg_tile(bg_tbl, tile_id, subpal)
                    src = fy * 8
                    dp = tcv * 8
                    line_raw[dp:dp + 8] = raw[src:src + 8]
                    dr = dp * 3
                    for k in range(8):
                        ci = col[src + k] * 3
                        ro = dr + k * 3
                        line_rgb[ro] = PAL_RGB[ci]
                        line_rgb[ro + 1] = PAL_RGB[ci + 1]
                        line_rgb[ro + 2] = PAL_RGB[ci + 2]
                bo = py * W
                buf[bo * 3:(bo + W) * 3] = line_rgb[fine_x * 3:fine_x * 3 + W * 3]
                bg_pix[bo:bo + W] = line_raw[fine_x:fine_x + W]
        else:
            for py in range(H):
                ro = py * W * 3
                for px in range(W):
                    o = ro + px * 3
                    buf[o] = bd_r
                    buf[o + 1] = bd_g
                    buf[o + 2] = bd_b

        if sp_on:
            pal = self.palette
            chr_byte = self._chr_byte
            for si in range(63, -1, -1):
                base = si * 4
                if self.oam[base] >= 0xEF:
                    continue
                oy = self.oam[base] + 1
                tile = self.oam[base + 1]
                attr = self.oam[base + 2]
                ox = self.oam[base + 3]
                flip_v = attr & 0x80
                flip_h = attr & 0x40
                behind = attr & 0x20
                spal = attr & 0x03
                if tall:
                    bank = (tile & 1) * 0x1000
                    tile_base = tile & 0xFE
                else:
                    bank = sp_base
                    tile_base = tile
                for ry in range(sp_h):
                    row = (sp_h - 1 - ry) if flip_v else ry
                    if tall:
                        if row < 8:
                            tb = bank + tile_base * 16 + row
                        else:
                            tb = bank + (tile_base + 1) * 16 + (row - 8)
                    else:
                        tb = bank + tile_base * 16 + row
                    lo = chr_byte(tb)
                    hi = chr_byte(tb + 8)
                    py_curr = oy + ry
                    if py_curr < 0 or py_curr >= H:
                        continue
                    row_off = py_curr * W
                    for fx in range(8):
                        col = (7 - fx) if flip_h else fx
                        pv = ((lo >> (7 - col)) & 1) | (((hi >> (7 - col)) & 1) << 1)
                        if pv == 0:
                            continue
                        px_curr = ox + fx
                        if px_curr < 0 or px_curr >= W:
                            continue
                        if behind and bg_pix[row_off + px_curr]:
                            continue
                        ci = (pal[self._pal_addr(0x10 + (spal << 2) + pv)] & 0x3F) * 3
                        q = (row_off + px_curr) * 3
                        buf[q] = PAL_RGB[ci]
                        buf[q + 1] = PAL_RGB[ci + 1]
                        buf[q + 2] = PAL_RGB[ci + 2]
        return bytes(buf)


# =============================================================================
#   NES System
# =============================================================================
class NES:
    def __init__(self):
        self.bus = Bus()
        self.cpu = CPU(self.bus)
        self.ppu = PPU()
        self.rom: INES | None = None

    def load(self, path: str):
        self.rom = INES(path)
        self.ppu.attach(self.rom)
        self.ppu.bind_nes(self)
        self.bus.attach(self.rom, self.ppu)
        self.power_on()

    def power_on(self):
        self.bus.ram[:] = [0] * 0x0800
        self.ppu.reset()
        if self.bus.mapper: self.bus.mapper.reset()
        self.cpu.reset()

    def reset(self):
        if not self.rom: return
        self.cpu.reset()
        if self.bus.mapper: self.bus.mapper.reset()

    def step(self): return self.cpu.step()

    def frame(self):
        cpu = self.cpu
        ppu = self.ppu
        mapper = self.bus.mapper
        ppu.begin_frame()
        s0_cycle = ppu.compute_sprite0_hit_cycle()
        vblank_cycle = 2273
        snap_at = vblank_cycle
        snapped = False
        vblank_on = False
        next_sl = 0
        cycles_per_sl = 113.6667
        is_mmc3 = isinstance(mapper, Mapper4)
        target = 29780
        count = 0
        while count < target and not cpu.halted:
            count += cpu.step() or 1
            if not vblank_on and count >= vblank_cycle:
                vblank_on = True
                ppu._vblank = True
                ppu._fire_nmi()
            if not snapped and count >= snap_at:
                ppu.snapshot_scroll()
                snapped = True
            if s0_cycle is not None and count >= s0_cycle:
                if (ppu.regs[1] & 0x18) == 0x18:
                    ppu._sprite0_hit = True
                    s0_cycle = None
            if is_mmc3:
                while next_sl < 240 and count >= int(snap_at + next_sl * cycles_per_sl):
                    mapper.step_scanline(next_sl)
                    next_sl += 1
                if mapper.irq_pending:
                    mapper.irq_pending = False
                    cpu.irq()
        if not snapped:
            ppu.snapshot_scroll()
        if (ppu.regs[1] & 0x18) == 0x18 and ppu.oam[0] < 0xEF:
            ppu._sprite0_hit = True
        ppu.end_frame()


# =============================================================================
#   FCEUX GUI Frame
# =============================================================================
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.nes  = NES()
        self._emu_lock = threading.Lock()
        self.running = False
        self.run_thread: threading.Thread | None = None
        self._keys: set[str] = set()
        self._pad_map = {
            "z": 0x01, "x": 0x02, "Return": 0x08, "BackSpace": 0x04,
            "Up": 0x10, "Down": 0x20, "Left": 0x40, "Right": 0x80,
        }

        root.title("ac's nes emu 0.1")
        root.geometry("600x400")
        root.configure(bg=BG)
        root.resizable(False, False)
        root.option_add("*Font", FONT_UI)
        root.update_idletasks()
        root.minsize(600, 400)
        root.maxsize(600, 400)
        self._fixed_geometry = root.wm_geometry()

        self._build_menu()
        self._build_body()
        self._build_status()
        self._bind_input()
        self._refresh_state()
        self._pattern_img_ref = None
        self._canvas_img_id: int | None = None
        self._ui_tick = 0
        self._tool_windows: dict[str, tk.Toplevel] = {}

    def _btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=BG, fg=FG,
                         activebackground=ACCENT, activeforeground=HI,
                         disabledforeground=DIM, bd=1, relief="solid",
                         highlightbackground=EDGE, highlightthickness=1,
                         font=FONT_MONO_B, padx=8, pady=2, cursor="hand2")

    def _lbl(self, parent, text, **kw):
        opts = {"bg": BG, "fg": FG, "font": FONT_MONO}
        opts.update(kw)
        return tk.Label(parent, text=text, **opts)

    def _build_menu(self):
        bar = tk.Menu(self.root, bg=BG, fg=FG, activebackground=ACCENT, activeforeground=HI, bd=0, tearoff=0)
        m_file = tk.Menu(bar, bg=BG, fg=FG, tearoff=0, activebackground=ACCENT, activeforeground=HI)
        m_file.add_command(label="Open ROM...", command=self.open_rom)
        m_file.add_command(label="Close ROM",   command=self.close_rom)
        m_file.add_separator()
        m_file.add_command(label="Exit", command=self.root.destroy)
        bar.add_cascade(label="File", menu=m_file)

        m_nes = tk.Menu(bar, bg=BG, fg=FG, tearoff=0, activebackground=ACCENT, activeforeground=HI)
        m_nes.add_command(label="Power",  command=self.power)
        m_nes.add_command(label="Reset",  command=self.reset)
        m_nes.add_separator()
        m_nes.add_command(label="Pause",  command=self.pause)
        m_nes.add_command(label="Resume", command=self.resume)
        bar.add_cascade(label="NES", menu=m_nes)

        m_tools = tk.Menu(bar, bg=BG, fg=FG, tearoff=0, activebackground=ACCENT, activeforeground=HI)
        m_tools.add_command(label="PPU Viewer",   command=self.ppu_viewer)
        m_tools.add_command(label="Hex Editor",   command=self.hex_editor)
        bar.add_cascade(label="Tools", menu=m_tools)

        m_dbg = tk.Menu(bar, bg=BG, fg=FG, tearoff=0, activebackground=ACCENT, activeforeground=HI)
        m_dbg.add_command(label="Debugger",   command=self.debugger)
        m_dbg.add_command(label="Step",       command=self.step_one)
        bar.add_cascade(label="Debug", menu=m_dbg)
        self.root.config(menu=bar)

    def _build_body(self):
        vid_wrap = tk.Frame(self.root, bg=BG, bd=1, relief="solid", highlightbackground=EDGE, highlightthickness=1)
        vid_wrap.place(x=8, y=4, width=350, height=320)
        self.canvas = tk.Canvas(vid_wrap, width=256, height=240, bg=BG, bd=0, highlightthickness=0)
        self.canvas.place(x=46, y=38)
        self.canvas.create_text(128, 120, text="NO SIGNAL", fill=ACCENT, font=("Courier", 16, "bold"))

        side = tk.Frame(self.root, bg=BG)
        side.place(x=370, y=4, width=222, height=320)
        self._lbl(side, "─ control ─").pack(anchor="w", pady=(2, 4))
        for label, cmd in (("Power", self.power), ("Reset", self.reset), ("Pause", self.pause), ("Resume", self.resume), ("Step", self.step_one)):
            self._btn(side, label, cmd).pack(fill="x", pady=1)

    def _build_status(self):
        sb = tk.Frame(self.root, bg=BG, bd=1, relief="solid", highlightbackground=ACCENT, highlightthickness=1)
        sb.place(x=8, y=328, width=584, height=64)
        self.lbl_rom  = self._lbl(sb, "ROM: <none>")
        self.lbl_rom.place(x=6, y=2)
        self.lbl_info = self._lbl(sb, "")
        self.lbl_info.place(x=6, y=18)
        self.lbl_cpu  = self._lbl(sb, "CPU: -", width=52, anchor="w")
        self.lbl_cpu.place(x=6, y=34)
        self.lbl_state = self._lbl(sb, "● stopped", fg=DIM)
        self.lbl_state.place(x=460, y=2)

    def open_rom(self):
        path = filedialog.askopenfilename(title="open .nes rom", filetypes=[("iNES rom", "*.nes"), ("all files", "*.*")])
        if not path: return
        try:
            self.nes.load(path)
        except Exception as e:
            messagebox.showerror("acnesemu", f"load failed:\n{e}")
            return
        self._refresh_state()
        self._restore_main_geometry()
        self.resume()

    def close_rom(self):
        self.pause()
        self.nes = NES()
        self._canvas_img_id = None
        self.canvas.delete("all")
        self.canvas.create_text(128, 120, text="NO SIGNAL", fill=ACCENT, font=("Courier", 16, "bold"))
        self._refresh_state()
        self._restore_main_geometry()

    def power(self):
        if self.nes.rom:
            with self._emu_lock: self.nes.power_on()
            self._present_frame(self.nes.ppu.render_rgb())

    def reset(self):
        with self._emu_lock: self.nes.reset()
        self._present_frame(self.nes.ppu.render_rgb() if self.nes.rom else None)

    def pause(self):
        self.running = False
        self.lbl_state.configure(text="● paused", fg=ACCENT)

    def resume(self):
        if not self.nes.rom or self.running: return
        self.running = True
        self.lbl_state.configure(text="● running", fg=HI)
        self.run_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.run_thread.start()

    def step_one(self):
        if not self.nes.rom: return
        with self._emu_lock:
            self.nes.frame()
            rgb = self.nes.ppu.render_rgb()
        self._present_frame(rgb)

    def _bind_input(self):
        for w in (self.root, self.canvas):
            w.bind("<KeyPress>", self._on_key_down)
            w.bind("<KeyRelease>", self._on_key_up)
        self.root.focus_set()

    def _on_key_down(self, ev):
        self._keys.add(ev.keysym)
        self._sync_controller()

    def _on_key_up(self, ev):
        self._keys.discard(ev.keysym)
        self._sync_controller()

    def _sync_controller(self):
        pad = 0
        for key, bit in self._pad_map.items():
            if key in self._keys: pad |= bit
        if self.nes.bus: self.nes.bus.controller = pad

    def _run_loop(self):
        while self.running and not self.nes.cpu.halted:
            with self._emu_lock:
                self.nes.frame()
                rgb = self.nes.ppu.render_rgb()
            self.root.after(0, lambda r=rgb: self._present_frame(r))
            time.sleep(1 / 60)

    def _restore_main_geometry(self) -> None:
        if getattr(self, "_fixed_geometry", None):
            self.root.wm_geometry(self._fixed_geometry)

    def _present_frame(self, rgb: bytes | None) -> None:
        self._ui_tick += 1
        if self._ui_tick & 3 == 0:
            self._refresh_cpu_state()
        if rgb:
            self._canvas_show_rgb_buffer(256, 240, rgb, "")

    def _refresh_state(self) -> None:
        r = self.nes.rom
        self.lbl_rom.configure(text=f"ROM: {os.path.basename(r.path)}" if r else "ROM: <none>")
        self.lbl_info.configure(text=r.info() if r else "")
        self._refresh_cpu_state()
        self._restore_main_geometry()

    def _refresh_cpu_state(self) -> None:
        c = self.nes.cpu
        self.lbl_cpu.configure(
            text=f"PC:{c.pc:04X} A:{c.a:02X} X:{c.x:02X} Y:{c.y:02X} "
                 f"SP:{c.sp:02X} P:{c.p:02X} cyc:{c.cycles}")

    def _canvas_show_rgb_buffer(self, width: int, height: int, rgb: bytes, subtitle: str) -> None:
        try:
            from PIL import Image, ImageTk
            im = Image.frombytes("RGB", (width, height), rgb)
            photo = ImageTk.PhotoImage(im)
        except ImportError:
            hdr = f"P6\n{width} {height}\n255\n".encode("ascii")
            with tempfile.NamedTemporaryFile(suffix=".ppm", delete=False) as tmp:
                tmp.write(hdr + rgb)
                path = tmp.name
            photo = tk.PhotoImage(file=path)
            os.unlink(path)
        self._pattern_img_ref = photo
        if self._canvas_img_id is None:
            self._canvas_img_id = self.canvas.create_image(0, 0, anchor="nw", image=photo)
        else:
            self.canvas.itemconfig(self._canvas_img_id, image=photo)

    def _open_tool(self, key: str, title: str, geo: str, build) -> tk.Toplevel:
        existing = self._tool_windows.get(key)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return existing
        w = tk.Toplevel(self.root, bg=BG)
        w.title(title)
        w.resizable(False, False)
        w.geometry(geo)
        w.update_idletasks()
        wh = geo.split("+")[0]
        ww, hh = wh.split("x")
        w.minsize(int(ww), int(hh))
        w.maxsize(int(ww), int(hh))
        build(w)
        self._tool_windows[key] = w
        return w

    def ppu_viewer(self):
        if not self.nes.rom:
            return

        def build(w: tk.Toplevel) -> None:
            cv = tk.Canvas(w, width=512, height=256, bg=BG, highlightthickness=0)
            cv.pack(padx=8, pady=8)

        self._open_tool("ppu", "PPU Viewer", "528x272", build)

    def hex_editor(self):
        def build(w: tk.Toplevel) -> None:
            txt = tk.Text(w, width=70, height=20, bg=BG, fg=FG, insertbackground=FG)
            txt.pack(padx=8, pady=8)
            ram = self.nes.bus.ram
            txt.insert("1.0", "\n".join(
                f"{row:04X}: " + " ".join(f"{ram[row + i]:02X}" for i in range(16))
                for row in range(0, 0x0800, 16)))
            txt.configure(state="disabled")

        self._open_tool("hex", "Hex Editor", "560x360", build)

    def debugger(self):
        def build(w: tk.Toplevel) -> None:
            self._lbl(w, "Debugger active").pack(padx=12, pady=12)

        self._open_tool("dbg", "Debugger", "240x80", build)

def main():
    root = tk.Tk()
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
