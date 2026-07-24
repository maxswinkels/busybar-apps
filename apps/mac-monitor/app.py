#!/usr/bin/env python3
"""Mac system monitor: CPU, memory and network as live bars.

    python app.py                        # BUSY Bar over USB (always 10.0.4.20)
    python app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP = "demo.sysmon"

# ---------------------------------------------------------------------------
# BUSY Bar HTTP API — self-contained, stdlib only.
# Over USB the bar is always at 10.0.4.20; --host targets a Wi-Fi bar or the
# emulator. Full API docs are served by the device: http://10.0.4.20/docs
# ---------------------------------------------------------------------------

def _host(default="10.0.4.20"):
    if "--host" in sys.argv:
        i = sys.argv.index("--host")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default

BASE = "http://" + _host().replace("http://", "").rstrip("/")

def draw(elements, **extra):
    body = {"application_name": APP, "elements": elements, **extra}
    req = urllib.request.Request(BASE + "/api/display/draw",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5):
        pass

def text(txt, x=0, y=0, font="normal", color="0xFFFFFFFF", **kw):
    return {"type": "text", "text": str(txt), "x": x, "y": y, "font": font, "color": color, **kw}

def rectangle(x, y, width, height, **kw):
    return {"type": "rectangle", "x": x, "y": y, "width": width, "height": height, **kw}

# ---------------------------------------------------------------------------
# System metrics
# ---------------------------------------------------------------------------

# State carried between ticks for network rate calculation.
_prev_bytes = 0
_prev_time = 0.0
_last = {"cpu": 0.0, "mem": 0.0, "net_rate": 0.0}


def _run(*args):
    """Run a command and return stdout, or '' on failure."""
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=3)
        return r.stdout
    except Exception:
        return ""


def _cpu_pct():
    """Return CPU % (0-100) as a float, capped at 100."""
    try:
        ps_out = _run("ps", "-A", "-o", "%cpu")
        total = sum(float(l) for l in ps_out.splitlines() if l.strip() not in ("", "%CPU"))
        ncpu_out = _run("sysctl", "-n", "hw.ncpu").strip()
        ncpu = int(ncpu_out) if ncpu_out else 1
        return min(100.0, total / max(ncpu, 1))
    except Exception:
        return _last["cpu"]


def _mem_pct():
    """Return RAM % (0-100) as a float."""
    try:
        vm = _run("vm_stat")
        # Parse page size from header: "Mach Virtual Memory Statistics: (page size of 16384 bytes)"
        page_size = 4096
        for line in vm.splitlines():
            if "page size of" in line:
                parts = line.split()
                idx = parts.index("of") + 1
                page_size = int(parts[idx])
                break

        def _pages(key):
            for line in vm.splitlines():
                if line.startswith(key):
                    val = line.split(":")[1].strip().rstrip(".")
                    return int(val)
            return 0

        active = _pages("Pages active")
        wired = _pages("Pages wired down")
        compressed = _pages("Pages occupied by compressor")
        used_bytes = (active + wired + compressed) * page_size

        memsize_out = _run("sysctl", "-n", "hw.memsize").strip()
        total_bytes = int(memsize_out) if memsize_out else 1
        return min(100.0, used_bytes / total_bytes * 100.0)
    except Exception:
        return _last["mem"]


def _net_bytes():
    """Return total ibytes+obytes across all physical interfaces."""
    try:
        out = _run("netstat", "-ib")
        seen = set()
        total = 0
        for line in out.splitlines():
            parts = line.split()
            # Header line or too-short lines: skip.
            if len(parts) < 10 or parts[0] == "Name":
                continue
            iface = parts[0]
            # Skip loopback and duplicate interface rows (keep first occurrence only).
            if iface == "lo0" or iface in seen:
                continue
            # netstat -ib columns: Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Drop
            # Ibytes = index 6, Obytes = index 9
            try:
                ibytes = int(parts[6])
                obytes = int(parts[9])
                total += ibytes + obytes
                seen.add(iface)
            except (ValueError, IndexError):
                continue
        return total
    except Exception:
        return 0


def _color(pct_or_frac_x100):
    """Green / orange / red based on percentage."""
    if pct_or_frac_x100 >= 90:
        return "0xE60022FF"
    if pct_or_frac_x100 >= 70:
        return "0xF59E0BFF"
    return "0x22C55EFF"


def _fmt_net(rate):
    """Auto-scale bytes/s to B/K/M/G with one decimal under 10."""
    for unit, threshold in (("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if rate >= threshold:
            val = rate / threshold
            return f"{val:.1f}{unit}" if val < 10 else f"{val:.0f}{unit}"
    return f"{rate:.0f}B"


def tick():
    global _prev_bytes, _prev_time

    cpu = _cpu_pct()
    mem = _mem_pct()
    _last["cpu"] = cpu
    _last["mem"] = mem

    now = time.monotonic()
    nb = _net_bytes()
    if _prev_time == 0.0:
        net_rate = 0.0
    else:
        elapsed = now - _prev_time
        delta = nb - _prev_bytes
        net_rate = max(0.0, delta / elapsed) if elapsed > 0 else 0.0
    _prev_bytes = nb
    _prev_time = now
    _last["net_rate"] = net_rate

    net_frac = min(1.0, net_rate / (10 * 1024 * 1024))

    elements = []

    for row_y, label, pct, frac, value_str in (
        (0,  "CPU", cpu, cpu / 100.0, f"{round(cpu)}%"),
        (5,  "MEM", mem, mem / 100.0, f"{round(mem)}%"),
        (10, "NET", net_frac * 100, net_frac, _fmt_net(net_rate)),
    ):
        col = _color(pct)

        # Label
        elements.append(text(label, x=0, y=row_y, font="tiny", color="0xFFFFFFFF"))

        # Bar outline (no fill, subtle border)
        elements.append(rectangle(
            x=15, y=row_y, width=41, height=5,
            border_width=1, border_color="0x505050FF",
        ))

        # Bar fill (solid, no border)
        fill_w = round(39 * frac)
        if fill_w >= 1:
            elements.append(rectangle(
                x=16, y=row_y + 1, width=fill_w, height=3,
                border_width=0, fill="solid", fill_colors=[col],
            ))

        # Value text, right-aligned at x=71
        elements.append(text(value_str, x=71, y=row_y, font="tiny",
                             align="top_right", color=col))

    try:
        draw(elements)
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = a higher-priority app owns the display
            raise


if __name__ == "__main__":
    print(f"mac_monitor → {BASE}  (Ctrl-C to stop)")
    try:
        while True:
            tick()
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\nstopped.")
    except urllib.error.HTTPError as e:
        sys.exit(f"error: HTTP {e.code} — {e.read().decode('utf-8', 'ignore')}")
    except urllib.error.URLError as e:
        sys.exit(f"error: cannot reach {BASE} — {e.reason}")
