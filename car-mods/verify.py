#!/usr/bin/env python3
"""Read the running PAL MM3 vehicle catalog and compare it with a car manifest.

Read-only: Windows uses OpenProcess/ReadProcessMemory (ctypes); Linux uses
/proc/<pid>/mem. macOS and other systems report that inspection is unsupported.
--log must be the game's own stderr log so its guest-memory mapping is correct.
A JSON result is printed; ready is false before the catalog loads or when an
expected car, ID or rating is missing. A live player's telemetry is included
when a race has started. No save, game file, or game memory is changed.

Example:
    python tools/car-tools/car-mods/verify.py --process-id 1234 --log out/car-check/stderr.log --expected-manifest tools/car-tools/car-mods/cars.json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import time

GAME_NAMES = ("mm3_recomp", "mm3_recomp.exe")
RATINGS = ("TOPSPEED", "ACCELERATION", "MASS", "HANDLING", "DURABILITY")
CATALOG_MANAGER = 0x003C5C94
MANAGER_PTR = 0x003C5CDC


class GuestMemoryError(RuntimeError):
    pass


class ProcessMemory:
    """Read-only access to the game process, one backend per OS."""

    def __init__(self, pid: int):
        self.pid = pid
        self._handle = None
        self._fd = None
        self._kernel32 = None
        if sys.platform == "win32":
            self._open_windows()
        elif os.path.isdir("/proc/%d" % pid):
            self._fd = os.open("/proc/%d/mem" % pid, os.O_RDONLY)
        else:
            raise RuntimeError(f"Process inspection is not supported on {sys.platform} "
                               "(needs Windows, or Linux with /proc).")

    def _open_windows(self) -> None:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32 = kernel32
        # PROCESS_VM_READ | PROCESS_QUERY_INFORMATION; the latter also allows the image-name query.
        self._handle = kernel32.OpenProcess(0x410, False, self.pid)
        if not self._handle:
            raise RuntimeError(f"Cannot inspect MM3 (Win32 error {ctypes.get_last_error()}).")

    @property
    def name(self) -> str:
        if self._kernel32 is not None:
            from ctypes import wintypes
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(32768)
            if not self._kernel32.QueryFullProcessImageNameW(self._handle, 0, buffer, ctypes.byref(size)):
                return ""
            return Path(buffer.value).name
        try:
            return Path("/proc/%d/comm" % self.pid).read_text().strip()
        except OSError:
            return ""

    def read(self, address: int, size: int) -> bytes:
        if self._fd is not None:
            try:
                data = os.pread(self._fd, size, address)
            except OSError:
                data = b""
            if len(data) != size:
                raise GuestMemoryError(f"Cannot read guest memory at {address:08X}.")
            return data
        buffer = ctypes.create_string_buffer(size)
        read_count = ctypes.c_size_t(0)
        ok = self._kernel32.ReadProcessMemory(self._handle, ctypes.c_void_p(address), buffer,
                                              ctypes.c_size_t(size), ctypes.byref(read_count))
        if not ok or read_count.value != size:
            raise GuestMemoryError(f"Cannot read guest memory at {address:08X}.")
        return buffer.raw

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class Guest:
    """Guest (Xbox) addresses, translated through the offset the log reports."""

    def __init__(self, memory: ProcessMemory, offset: int):
        self.memory = memory
        self.offset = offset

    def read(self, address: int, size: int = 4) -> bytes:
        if address < 0x10000 or size < 1 or address + size > 0x08000000:
            raise GuestMemoryError(f"Invalid guest memory range {address:08X}+{size}.")
        return self.memory.read(self.offset + address, size)

    def word(self, address: int) -> int:
        return struct.unpack("<I", self.read(address))[0]

    def float(self, address: int) -> float:
        return struct.unpack("<f", self.read(address))[0]

    def narrow_string(self, address: int) -> str:
        # The PAL game's MSVC string has a 16-byte buffer union at +4 and
        # length/capacity at +0x14/+0x18. Capacity <16 means inline storage.
        header = self.read(address, 0x1C)
        length = struct.unpack_from("<I", header, 0x14)[0]
        capacity = struct.unpack_from("<I", header, 0x18)[0]
        if length > capacity or length > 4096:
            raise GuestMemoryError("Invalid guest narrow string.")
        if length == 0:
            return ""
        if capacity < 16:
            return header[4:4 + length].decode("ascii", errors="replace")
        chars = struct.unpack_from("<I", header, 4)[0]
        return self.read(chars, length).decode("ascii", errors="replace")


def floats(data: bytes) -> list[float]:
    return [struct.unpack_from("<f", data, i)[0] for i in range(0, len(data) - 3, 4)]


def read_log_offset(log: Path) -> int:
    # Read without locking: the running game still owns and appends to this log.
    with open(log, "r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle):
            if line_number >= 512:
                break
            match = re.search(r"offset \+(\d+) from Xbox base", line)
            if match:
                return int(match.group(1))
    raise ValueError("Guest memory mapping is unavailable in this log.")


def read_catalog(guest: Guest) -> tuple[int, list[dict], list[str]]:
    entries: list[dict] = []
    issues: list[str] = []
    catalog = guest.word(CATALOG_MANAGER)
    if catalog:
        start = guest.word(catalog + 8)
        end = guest.word(catalog + 0xC)
        if start and end >= start and (end - start) % 4 == 0 and end - start <= 8192:
            pointers = guest.read(start, end - start) if end > start else b""
            for index in range(len(pointers) // 4):
                entry = struct.unpack_from("<I", pointers, index * 4)[0]
                try:
                    data = guest.read(entry, 0x2A4)
                    entries.append({
                        "index": index,
                        "address": f"0x{entry:08X}",
                        "id": struct.unpack_from("<I", data, 0)[0],
                        "physics_id": struct.unpack_from("<I", data, 4)[0],
                        "alias": guest.narrow_string(entry + 0xC),
                        "hidden": bool(data[0x28]),
                        "locked": bool(data[0x29]),
                        "class": struct.unpack_from("<I", data, 0x30)[0],
                        "ratings": {
                            "TOPSPEED": struct.unpack_from("<i", data, 0x270)[0],
                            "ACCELERATION": struct.unpack_from("<i", data, 0x274)[0],
                            "MASS": struct.unpack_from("<i", data, 0x278)[0],
                            "HANDLING": struct.unpack_from("<i", data, 0x27C)[0],
                            "DURABILITY": struct.unpack_from("<i", data, 0x280)[0],
                        },
                    })
                except Exception as error:
                    issues.append(f"Catalog entry {index}: {error}")
        else:
            issues.append("Catalog pointer range is not ready or is invalid.")
    return catalog, entries, issues


def check_catalog(manifest: dict, entries: list[dict]) -> list[dict]:
    checks = []
    for expected in manifest["cars"]:
        matches = [entry for entry in entries if entry["id"] == int(expected["id"])]
        problems: list[str] = []
        actual = None
        if len(matches) != 1:
            problems.append(f"Expected one loaded car with ID {expected['id']}; found {len(matches)}.")
        else:
            actual = matches[0]
            if actual["alias"].lower() != str(expected["alias"]).lower():
                problems.append(f"Basename is '{actual['alias']}', expected '{expected['alias']}'.")
            if actual["locked"]:
                problems.append("Car is locked in this profile.")
            ratings = expected.get("ratings") or {}
            for rating in RATINGS:
                if rating not in ratings:
                    problems.append(f"Expected manifest lacks rating {rating}.")
                elif actual["ratings"][rating] != int(ratings[rating]):
                    problems.append(f"{rating} is {actual['ratings'][rating]}, expected {ratings[rating]}.")
        checks.append({
            "id": expected["id"],
            "alias": expected["alias"],
            "expected_display_name": expected.get("display_name"),
            "verified": not problems,
            "problems": problems,
            "loaded": actual,
        })
    return checks


def compare_physics(manifest: dict, tuning: dict, alias: str) -> list[dict]:
    matches = []
    for expected in manifest["cars"]:
        differences: list[str] = []
        unchecked: list[str] = []
        for name, wanted_value in (expected.get("physics") or {}).items():
            if name not in tuning:
                unchecked.append(name)
                continue
            observed_values = tuning[name] if isinstance(tuning[name], list) else [tuning[name]]
            wanted_values = wanted_value if isinstance(wanted_value, list) else [wanted_value]
            if len(observed_values) != len(wanted_values):
                differences.append(f"{name} has {len(observed_values)} values, expected {len(wanted_values)}.")
                continue
            for index, (observed_raw, wanted_raw) in enumerate(zip(observed_values, wanted_values)):
                observed = float(observed_raw)
                wanted = float(wanted_raw)
                tolerance = 0.000001 * max(1, abs(wanted))
                if math.isnan(observed) or math.isinf(observed) or abs(observed - wanted) > tolerance:
                    differences.append(f"{name}[{index}] is {observed}, expected {wanted}.")
        # These are value matches, not an inferred car identity.
        matches.append({
            "id": expected["id"],
            "alias": expected["alias"],
            "selected": str(expected["alias"]).lower() == alias.lower(),
            "all_fields_match": not differences and not unchecked,
            "differences": differences,
            "unchecked_fields": unchecked,
        })
    return matches


def read_current_player(guest: Guest, entries: list[dict], manifest: dict | None) -> dict:
    try:
        manager = guest.word(MANAGER_PTR)
        race = guest.word(manager + 0x54) if manager else 0
        player = guest.word(race + 0xD8) if race else 0
        vehicle = guest.word(player + 0x19C) if player else 0
        car = guest.word(vehicle + 8) if vehicle else 0
        simulation = guest.word(car + 0x3C) if car else 0
        engine = guest.word(simulation) if simulation else 0
        body = guest.word(simulation + 4) if simulation else 0
        if not (car and simulation and engine and body):
            return {"available": False}

        # Player+0x1A4 retains the CarBody parser's parameter allocation,
        # which sub_0021ED64 passes to the real physics constructor.
        parameters = guest.word(player + 0x1A4)
        alias = guest.narrow_string(player + 0x1DC)
        selected = [entry for entry in entries if entry["alias"].lower() == alias.lower()]
        tuning = None
        physics_matches: list[dict] = []
        if parameters:
            ratio_start = guest.word(parameters + 0x14)
            ratio_end = guest.word(parameters + 0x18)
            gear_ratios: list[float] = []
            if ratio_start and ratio_end >= ratio_start and ratio_end - ratio_start <= 64 \
                    and (ratio_end - ratio_start) % 4 == 0 and ratio_end > ratio_start:
                gear_ratios = floats(guest.read(ratio_start, ratio_end - ratio_start))
            tuning = {
                "NGears": guest.word(parameters + 0xC),
                "GearRatioVector": gear_ratios,
                "Mass": guest.float(parameters + 0x58),
                "TorqueScale": guest.float(parameters + 0x5C),
                "WheelDrive": guest.word(parameters + 0x28),
                "WheelRatio": guest.float(parameters + 0x60),
                "WheelRadius": guest.float(parameters + 0x64),
                "BrakeTorque": guest.float(parameters + 0xA8),
                "LatFrictionFactor": guest.float(parameters + 0xF4),
                "AirKZZ": guest.float(parameters + 0x98),
                "MaxTorqueRPM": guest.float(parameters + 0x9C),
                "MaxTurboTorqueRPM": guest.float(parameters + 0xA0),
                "MinRPMGearChange": guest.float(parameters + 0xA4),
                "MinRPM": guest.float(parameters + 0xAC),
                "MaxRPM": guest.float(parameters + 0xB0),
                "CutRPM": guest.float(parameters + 0xB4),
                "TimeChangeGear": guest.float(parameters + 0xC4),
                "TimeChangeGearFR": guest.float(parameters + 0xC8),
                "GearStatusDownGearFactor": guest.float(parameters + 0xD4),
            }
            if manifest:
                physics_matches = compare_physics(manifest, tuning, alias)

        # sub_001D57A6 copies these parameters into the live simulation.
        # Read the fixed fields in one block to keep sampling overhead low.
        sim = guest.read(simulation, 0x108)
        gear_count = struct.unpack_from("<I", sim, 0x50)[0]
        ratio_start = struct.unpack_from("<I", sim, 0x3C)[0]
        shift_start = struct.unpack_from("<I", sim, 0x40)[0]
        sim_ratios: list[float] = []
        sim_shifts: list[float] = []
        if ratio_start and gear_count <= 15:
            sim_ratios = floats(guest.read(ratio_start, (gear_count + 1) * 4))
        if shift_start and 0 < gear_count <= 15:
            sim_shifts = floats(guest.read(shift_start, gear_count * 4))
        simulation_tuning = {
            "NGears": gear_count,
            "GearRatioVector": sim_ratios,
            "ShiftSpeedThresholdsKmh": sim_shifts,
            "WheelDrive": struct.unpack_from("<I", sim, 0x58)[0],
            "WheelRatio": struct.unpack_from("<f", sim, 0x90)[0],
            "WheelRadius": struct.unpack_from("<f", sim, 0x94)[0],
            "MaxTorqueRPM": struct.unpack_from("<f", sim, 0xCC)[0],
            "MaxTurboTorqueRPM": struct.unpack_from("<f", sim, 0xD0)[0],
            "MinRPMGearChange": struct.unpack_from("<f", sim, 0xD4)[0],
            "MinRPM": struct.unpack_from("<f", sim, 0xDC)[0],
            "MaxRPM": struct.unpack_from("<f", sim, 0xE0)[0],
            "CutRPM": struct.unpack_from("<f", sim, 0xE4)[0],
            "TimeChangeGear": struct.unpack_from("<f", sim, 0xF4)[0],
            "TimeChangeGearFR": struct.unpack_from("<f", sim, 0xF8)[0],
            "GearStatusDownGearFactor": struct.unpack_from("<f", sim, 0x104)[0],
        }

        # The automatic shift branch uses this body velocity magnitude,
        # converted from m/s to km/h (sub_001CC5D1).
        body_bytes = guest.read(body, 0xD4)
        vx, vy, vz = struct.unpack_from("<fff", body_bytes, 0x20)
        engine_flags = guest.word(engine + 0x10)
        return {
            "available": True,
            "alias": alias,
            "catalog_id": selected[0]["id"] if len(selected) == 1 else None,
            "player": f"0x{player:08X}",
            "car": f"0x{car:08X}",
            "simulation": f"0x{simulation:08X}",
            "engine": f"0x{engine:08X}",
            "body": f"0x{body:08X}",
            "parameters": f"0x{parameters:08X}",
            "tuning": tuning,
            "simulation_tuning": simulation_tuning,
            "simulation_wheel_drive": simulation_tuning["WheelDrive"],
            "physics_manifest_matches": physics_matches,
            "speed": guest.float(simulation + 8),
            "road_speed_kmh": math.sqrt(vx * vx + vy * vy + vz * vz) * 3.6,
            "rpm": guest.float(simulation + 0xC),
            "gear": guest.word(simulation + 0x2C),
            "max_gear": guest.word(simulation + 0x50),
            "transmission": guest.word(simulation + 0x5C),
            "shift_state": engine_flags,
            "shift_active": (engine_flags & 0x08) != 0,
            "shift_timer": guest.float(engine + 0x14),
            "throttle": guest.float(simulation + 0x24),
            "brake": guest.float(simulation + 0x28),
            "requested_throttle": guest.float(car + 0x4C),
            "requested_brake": guest.float(car + 0x50),
            "handbrake": guest.float(car + 0x54),
            "x": struct.unpack_from("<f", body_bytes, 0xC8)[0],
            "z": struct.unpack_from("<f", body_bytes, 0xD0)[0],
        }
    except Exception as error:
        return {"available": False, "reason": str(error)}


def bounded_int(low: int, high: int):
    def parse(text: str) -> int:
        value = int(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value
    return parse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--process-id", type=bounded_int(1, 2**31 - 1), required=True,
                        help="PID of the running mm3_recomp process")
    parser.add_argument("--log", type=Path, required=True,
                        help="The game's stderr log (must contain the guest memory offset line)")
    parser.add_argument("--expected-manifest", "--manifest", dest="expected_manifest", type=Path,
                        help="Car manifest (cars.json) to verify against")
    parser.add_argument("--samples", type=bounded_int(1, 100000), default=1,
                        help="Number of telemetry samples (default: 1)")
    parser.add_argument("--interval-ms", type=bounded_int(10, 10000), default=150,
                        help="Milliseconds between samples (default: 150)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        memory = ProcessMemory(args.process_id)
    except (RuntimeError, OSError) as error:
        print(f"verify: {error}", file=sys.stderr)
        return 1
    try:
        if memory.name not in GAME_NAMES:
            raise RuntimeError("Expected an mm3_recomp process.")
        offset = read_log_offset(args.log)
        guest = Guest(memory, offset)
        manifest = None
        if args.expected_manifest:
            with open(args.expected_manifest, "r", encoding="utf-8-sig") as handle:
                manifest = json.load(handle)
            if not manifest.get("cars"):
                raise ValueError("Expected manifest must contain a nonempty cars array.")

        catalog, entries, issues = read_catalog(guest)
        catalog_ready = bool(entries) and not issues
        checks = check_catalog(manifest, entries) if manifest else []
        expected_verified = all(check["verified"] for check in checks) if manifest else None

        telemetry = []
        watch = time.perf_counter()
        for sample_index in range(args.samples):
            current = read_current_player(guest, entries, manifest)
            sample = {
                "sample_index": sample_index,
                "qpc_ms": time.perf_counter() * 1000.0,
                "elapsed_ms": (time.perf_counter() - watch) * 1000.0,
            }
            sample.update(current)
            telemetry.append(sample)
            if sample_index + 1 < args.samples:
                time.sleep(args.interval_ms / 1000.0)

        result = {
            "process_id": args.process_id,
            "guest_memory_offset": offset,
            "ready": catalog_ready and (expected_verified is None or expected_verified),
            "catalog_ready": catalog_ready,
            "catalog_manager": f"0x{catalog:08X}",
            "catalog_count": len(entries),
            "catalog_issues": issues,
            "expected_verified": expected_verified,
            "checks": checks,
            "catalog": entries,
            "current_player": current,
            "samples": telemetry,
        }
        print(json.dumps(result, indent=2))
        return 0
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(f"verify: {error}", file=sys.stderr)
        return 1
    finally:
        memory.close()


if __name__ == "__main__":
    raise SystemExit(main())
