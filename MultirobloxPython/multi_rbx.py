"""
MultiRBX - multi-instance Roblox launcher, per-instance CPU/RAM limiter, and alt account manager.
Windows only.   pip install psutil requests pywebview
Run this BEFORE opening any Roblox window (the multi-instance lock must be held by this app first).
Keep it running while your Roblox windows are open.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.parse
from tkinter import messagebox, simpledialog, ttk

import psutil
import requests

# ---------------------------------------------------------------- paths / config
APP_DIR = os.path.join(os.environ.get("APPDATA", "."), "MultiRBX")
os.makedirs(APP_DIR, exist_ok=True)
CFG_PATH = os.path.join(APP_DIR, "config.json")
ROBLOX_EXE = "robloxplayerbeta.exe"


def load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"place_id": "", "link_code": "", "launch_delay": 8, "accounts": []}


def save_cfg(cfg):
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------- win32 helpers
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CreateJobObjectW.restype = wt.HANDLE
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateEventW.restype = wt.HANDLE

PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED = 0x1000
ERROR_ALREADY_EXISTS = 183


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _from_blob(blob: DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


def protect(text: str) -> str:
    """Encrypt with Windows DPAPI: only your Windows user account can decrypt it."""
    blob_in, _keep = _to_blob(text.encode())
    blob_out = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _from_blob(blob_out).hex()


def unprotect(hex_text: str) -> str:
    blob_in, _keep = _to_blob(bytes.fromhex(hex_text))
    blob_out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _from_blob(blob_out).decode()


# ---------------------------------------------------------------- multi-instance lock
class SingletonLock:
    """Holds Roblox's single-instance mutex/event so later launches don't see it as taken."""

    def __init__(self):
        self.mutex = kernel32.CreateMutexW(None, True, "ROBLOX_singletonMutex")
        self.mutex_existed = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        self.event = kernel32.CreateEventW(None, False, False, "ROBLOX_singletonEvent")

    @property
    def ok(self):
        # If the mutex already existed, Roblox (or another tool) created it first.
        return bool(self.mutex) and not self.mutex_existed


# ---------------------------------------------------------------- resource limiter
class CPU_RATE(ctypes.Structure):
    _fields_ = [("ControlFlags", wt.DWORD), ("CpuRate", wt.DWORD)]


JobObjectCpuRateControlInformation = 15
CPU_ENABLE = 0x1
CPU_HARD_CAP = 0x4
WS_MAX_ENABLE, WS_MIN_DISABLE, WS_MAX_DISABLE = 0x4, 0x2, 0x8


class Limiter:
    def __init__(self):
        self.jobs = {}  # pid -> job handle

    def _open(self, pid, access):
        h = kernel32.OpenProcess(access, False, pid)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        return h

    def set_cpu(self, pid, percent):
        """Hard-cap total CPU use of the process to `percent` of the whole CPU (1-100)."""
        percent = max(1, min(100, int(percent)))
        job = self.jobs.get(pid)
        if not job:
            job = kernel32.CreateJobObjectW(None, None)
            h = self._open(pid, PROCESS_SET_QUOTA | PROCESS_TERMINATE)
            try:
                if not kernel32.AssignProcessToJobObject(job, h):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                kernel32.CloseHandle(h)
            self.jobs[pid] = job
        info = CPU_RATE(CPU_ENABLE | CPU_HARD_CAP, percent * 100)  # rate is in 1/100 of a percent
        if not kernel32.SetInformationJobObject(job, JobObjectCpuRateControlInformation,
                                                ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

    def set_ram(self, pid, mb):
        """Hard-limit the process's working set (physical RAM) to `mb` megabytes."""
        h = self._open(pid, PROCESS_SET_QUOTA | PROCESS_QUERY_LIMITED)
        try:
            ok = kernel32.SetProcessWorkingSetSizeEx(
                h, ctypes.c_size_t(32 * 1024 * 1024), ctypes.c_size_t(int(mb) * 1024 * 1024),
                WS_MAX_ENABLE | WS_MIN_DISABLE)
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(h)

    def clear(self, pid):
        if pid in self.jobs:
            info = CPU_RATE(CPU_ENABLE | CPU_HARD_CAP, 10000)
            kernel32.SetInformationJobObject(self.jobs[pid], JobObjectCpuRateControlInformation,
                                             ctypes.byref(info), ctypes.sizeof(info))
        h = self._open(pid, PROCESS_SET_QUOTA | PROCESS_QUERY_LIMITED)
        try:
            kernel32.SetProcessWorkingSetSizeEx(h, ctypes.c_size_t(-1), ctypes.c_size_t(-1),
                                                WS_MIN_DISABLE | WS_MAX_DISABLE)
        finally:
            kernel32.CloseHandle(h)


# ---------------------------------------------------------------- Roblox launching
UA = {"User-Agent": "Mozilla/5.0"}


def validate_cookie(cookie):
    r = requests.get("https://users.roblox.com/v1/users/authenticated",
                     headers={**UA, "Cookie": f".ROBLOSECURITY={cookie}"}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError("Cookie rejected (expired or invalid).")
    j = r.json()
    return j["name"], j["id"]


def get_ticket(cookie, place_id):
    url = "https://auth.roblox.com/v1/authentication-ticket"
    h = {**UA, "Referer": f"https://www.roblox.com/games/{place_id}",
         "Content-Type": "application/json", "Cookie": f".ROBLOSECURITY={cookie}"}
    r = requests.post(url, headers=h, timeout=15)
    if r.status_code == 403 and "x-csrf-token" in r.headers:
        h["X-CSRF-TOKEN"] = r.headers["x-csrf-token"]
        r = requests.post(url, headers=h, timeout=15)
    ticket = r.headers.get("rbx-authentication-ticket")
    if not ticket:
        raise RuntimeError(f"No auth ticket (HTTP {r.status_code}). The saved login probably expired - use \"Login / add account\" again for this account.")
    return ticket


def launch_uri(ticket, place_id, link_code):
    tracker = random.randint(10_000_000_000, 99_999_999_999)
    if link_code:
        req = (f"request=RequestPrivateGame&placeId={place_id}&linkCode={link_code}")
    else:
        req = f"request=RequestGame&placeId={place_id}"
    launcher = ("https://assetgame.roblox.com/game/PlaceLauncher.ashx?"
                f"{req}&browserTrackerId={tracker}&isPlayTogetherGame=false")
    return ("roblox-player:1+launchmode:play"
            f"+gameinfo:{ticket}+launchtime:{int(time.time() * 1000)}"
            f"+placelauncherurl:{urllib.parse.quote(launcher, safe='')}"
            f"+browsertrackerid:{tracker}+robloxLocale:en_us+gameLocale:en_us+channel:+LaunchExp:InApp")


def roblox_pids():
    out = set()
    for p in psutil.process_iter(["pid", "name"]):
        if (p.info["name"] or "").lower() == ROBLOX_EXE:
            out.add(p.info["pid"])
    return out


# ---------------------------------------------------------------- browser login (own process)
def run_login_window(out_path):
    """Opens a real Roblox login page in an embedded browser (WebView2). When you finish logging in,
    the session cookie is encrypted with DPAPI and written to out_path, then the window closes.
    Runs in a separate process so every login starts from a completely clean browser session."""
    try:
        import webview
    except ImportError:
        sys.exit(2)
    tmp = tempfile.mkdtemp(prefix="multirbx_")
    closed = threading.Event()
    win = webview.create_window("Log in to Roblox - this window closes by itself when you're done",
                                "https://www.roblox.com/login", width=520, height=760)
    win.events.closed += closed.set

    def watch(window):
        while not closed.is_set():
            time.sleep(1)
            try:
                jar = window.get_cookies()
            except Exception:
                continue
            for c in jar or []:
                try:
                    m = c.get(".ROBLOSECURITY")
                except Exception:
                    m = None
                if m is not None and m.value:
                    with open(out_path, "w", encoding="ascii") as f:
                        f.write(protect(m.value))
                    time.sleep(1)
                    window.destroy()
                    return

    try:
        webview.start(watch, win, private_mode=False, storage_path=tmp)
    except Exception:
        sys.exit(3)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def browser_login():
    """Parent side: spawn the login window, return the cookie once the user has logged in."""
    out = os.path.join(tempfile.gettempdir(), f"multirbx_{os.getpid()}_{int(time.time())}.tmp")
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--login", out]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--login", out]
    proc = subprocess.run(cmd)
    if proc.returncode == 2:
        raise RuntimeError("The 'pywebview' package is missing.\nRun:  python -m pip install pywebview")
    if proc.returncode == 3 or not os.path.exists(out):
        raise RuntimeError("Login window was closed before the login finished "
                           "(or WebView2 isn't installed).")
    try:
        with open(out, "r", encoding="ascii") as f:
            return unprotect(f.read().strip())
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


# ---------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MultiRBX")
        self.geometry("760x520")
        self.cfg = load_cfg()
        self.lock = SingletonLock()
        self.limiter = Limiter()
        self.labels = {}      # pid -> account name
        self.limits = {}      # pid -> (cpu, ram) text
        self.procs = {}       # pid -> psutil.Process (cached for cpu_percent)

        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, padding=6).pack(fill="x")
        self._update_status()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.acc_tab, self.inst_tab = ttk.Frame(nb), ttk.Frame(nb)
        nb.add(self.acc_tab, text="Accounts")
        nb.add(self.inst_tab, text="Instances / Limits")
        self._build_accounts()
        self._build_instances()
        self.after(1500, self._tick)

    def _update_status(self, extra=""):
        if self.lock.ok:
            msg = "Multi-instance lock: ACTIVE"
        else:
            msg = ("Multi-instance lock: NOT held - Roblox was already running. "
                   "Close all Roblox windows, restart this app, then launch.")
        self.status.set(msg + (f"   |   {extra}" if extra else ""))

    # ---- accounts tab
    def _build_accounts(self):
        f = self.acc_tab
        top = ttk.Frame(f)
        top.pack(fill="x", pady=6)
        self.place_var = tk.StringVar(value=self.cfg.get("place_id", ""))
        self.code_var = tk.StringVar(value=self.cfg.get("link_code", ""))
        self.delay_var = tk.StringVar(value=str(self.cfg.get("launch_delay", 8)))
        for i, (label, var, w) in enumerate([("Place ID", self.place_var, 16),
                                              ("Private server link code (optional)", self.code_var, 30),
                                              ("Delay between launches (s)", self.delay_var, 4)]):
            ttk.Label(top, text=label).grid(row=0, column=i, sticky="w", padx=4)
            ttk.Entry(top, textvariable=var, width=w).grid(row=1, column=i, sticky="w", padx=4)

        self.acc_tree = ttk.Treeview(f, columns=("name", "id"), show="headings", selectmode="extended")
        self.acc_tree.heading("name", text="Account")
        self.acc_tree.heading("id", text="User ID")
        self.acc_tree.pack(fill="both", expand=True, pady=6)
        self.acc_tree.bind("<Double-1>", lambda e: self.launch_selected())

        btns = ttk.Frame(f)
        btns.pack(fill="x")
        ttk.Button(btns, text="Login / add account", command=self.login_add_account).pack(side="left", padx=4)
        ttk.Button(btns, text="Add via cookie", command=self.add_account).pack(side="left", padx=4)
        ttk.Button(btns, text="Remove", command=self.remove_account).pack(side="left", padx=4)
        ttk.Button(btns, text="Launch selected", command=self.launch_selected).pack(side="right", padx=4)
        self._fill_accounts()

    def _fill_accounts(self):
        self.acc_tree.delete(*self.acc_tree.get_children())
        for i, a in enumerate(self.cfg["accounts"]):
            self.acc_tree.insert("", "end", iid=str(i), values=(a["name"], a["user_id"]))

    def _save_inputs(self):
        self.cfg["place_id"] = self.place_var.get().strip()
        self.cfg["link_code"] = self.code_var.get().strip()
        try:
            self.cfg["launch_delay"] = max(0, int(self.delay_var.get()))
        except ValueError:
            self.cfg["launch_delay"] = 8
        save_cfg(self.cfg)

    def _store_account(self, cookie):
        name, uid = validate_cookie(cookie)
        self.cfg["accounts"] = [a for a in self.cfg["accounts"] if a["user_id"] != uid]
        self.cfg["accounts"].append({"name": name, "user_id": uid, "cookie": protect(cookie)})
        save_cfg(self.cfg)
        self.after(0, self._fill_accounts)

    def login_add_account(self):
        """Log in normally in a browser window; the session is saved automatically.
        Logging in again to an existing account refreshes its saved login."""
        self._update_status("Waiting for you to log in...")

        def work():
            try:
                self._store_account(browser_login())
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Login", str(e)))
            finally:
                self.after(0, lambda: self._update_status())

        threading.Thread(target=work, daemon=True).start()

    def add_account(self):
        cookie = simpledialog.askstring(
            "Add account",
            "Paste the .ROBLOSECURITY cookie of an account YOU own.\n"
            "It is stored encrypted for your Windows user only.\nNever share it with anyone.",
            show="*", parent=self)
        if not cookie:
            return
        cookie = cookie.strip().strip('"')

        def work():
            try:
                self._store_account(cookie)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Add account", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def remove_account(self):
        idx = sorted((int(i) for i in self.acc_tree.selection()), reverse=True)
        for i in idx:
            del self.cfg["accounts"][i]
        save_cfg(self.cfg)
        self._fill_accounts()

    def launch_selected(self):
        self._save_inputs()
        place = self.cfg["place_id"]
        if not place.isdigit():
            messagebox.showwarning("Launch", "Enter a numeric Place ID first.")
            return
        if not self.lock.ok:
            messagebox.showwarning("Launch", "Multi-instance lock isn't held. Close Roblox and restart this app.")
            return
        accounts = [self.cfg["accounts"][int(i)] for i in self.acc_tree.selection()]
        if not accounts:
            return
        threading.Thread(target=self._launch_worker, args=(accounts, place), daemon=True).start()

    def _launch_worker(self, accounts, place):
        for n, acc in enumerate(accounts):
            self.after(0, lambda a=acc: self._update_status(f"Launching {a['name']}..."))
            try:
                before = roblox_pids()
                ticket = get_ticket(unprotect(acc["cookie"]), place)
                os.startfile(launch_uri(ticket, place, self.cfg["link_code"]))
                deadline = time.time() + 90
                while time.time() < deadline:   # tag the new window with this account's name
                    new = roblox_pids() - before
                    if new:
                        for pid in new:
                            self.labels[pid] = acc["name"]
                        break
                    time.sleep(1)
            except Exception as e:
                self.after(0, lambda a=acc, e=e: messagebox.showerror("Launch", f"{a['name']}: {e}"))
            if n < len(accounts) - 1:
                time.sleep(self.cfg.get("launch_delay", 8))
        self.after(0, lambda: self._update_status())

    # ---- instances tab
    def _build_instances(self):
        f = self.inst_tab
        cols = ("pid", "account", "cpu", "ram", "limits")
        self.inst_tree = ttk.Treeview(f, columns=cols, show="headings", selectmode="extended")
        for c, t, w in [("pid", "PID", 70), ("account", "Account", 160), ("cpu", "CPU %", 70),
                        ("ram", "RAM (MB)", 90), ("limits", "Active limits", 200)]:
            self.inst_tree.heading(c, text=t)
            self.inst_tree.column(c, width=w)
        self.inst_tree.pack(fill="both", expand=True, pady=6)

        row = ttk.Frame(f)
        row.pack(fill="x", pady=4)
        self.cpu_lim = tk.StringVar(value="25")
        self.ram_lim = tk.StringVar(value="1500")
        ttk.Label(row, text="CPU cap %").pack(side="left", padx=4)
        ttk.Entry(row, textvariable=self.cpu_lim, width=6).pack(side="left")
        ttk.Label(row, text="RAM cap MB").pack(side="left", padx=4)
        ttk.Entry(row, textvariable=self.ram_lim, width=7).pack(side="left")
        ttk.Button(row, text="Apply to selected", command=self.apply_limits).pack(side="left", padx=8)
        ttk.Button(row, text="Clear limits", command=self.clear_limits).pack(side="left")
        ttk.Button(row, text="Close selected", command=self.kill_selected).pack(side="right", padx=4)
        ttk.Label(f, text="RAM cap is a hard working-set limit: very low values (<1000 MB) make Roblox "
                          "page heavily and stutter.", foreground="gray").pack(anchor="w", padx=4)

    def _selected_pids(self):
        return [int(self.inst_tree.item(i)["values"][0]) for i in self.inst_tree.selection()]

    def apply_limits(self):
        try:
            cpu, ram = int(self.cpu_lim.get()), int(self.ram_lim.get())
        except ValueError:
            messagebox.showwarning("Limits", "CPU and RAM must be whole numbers.")
            return
        errors = []
        for pid in self._selected_pids():
            try:
                self.limiter.set_cpu(pid, cpu)
                self.limiter.set_ram(pid, ram)
                self.limits[pid] = f"CPU {cpu}% / RAM {ram} MB"
            except Exception as e:
                errors.append(f"PID {pid}: {e}")
        if errors:
            messagebox.showerror("Limits", "\n".join(errors))

    def clear_limits(self):
        for pid in self._selected_pids():
            try:
                self.limiter.clear(pid)
                self.limits.pop(pid, None)
            except Exception as e:
                messagebox.showerror("Limits", f"PID {pid}: {e}")

    def kill_selected(self):
        for pid in self._selected_pids():
            try:
                psutil.Process(pid).terminate()
            except Exception:
                pass

    def _tick(self):
        pids = roblox_pids()
        selected = set(self._selected_pids())
        self.inst_tree.delete(*self.inst_tree.get_children())
        for pid in sorted(pids):
            try:
                p = self.procs.setdefault(pid, psutil.Process(pid))
                cpu = p.cpu_percent(None) / (psutil.cpu_count() or 1)
                ram = p.memory_info().rss / 1024 / 1024
            except psutil.Error:
                continue
            iid = self.inst_tree.insert("", "end", iid=str(pid), values=(
                pid, self.labels.get(pid, "?"), f"{cpu:.0f}", f"{ram:.0f}", self.limits.get(pid, "")))
            if pid in selected:
                self.inst_tree.selection_add(iid)
        for pid in list(self.procs):
            if pid not in pids:
                self.procs.pop(pid, None)
                self.limits.pop(pid, None)
                self.limiter.jobs.pop(pid, None)
        self.after(2000, self._tick)


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("MultiRBX only runs on Windows.")
    if "--login" in sys.argv:
        run_login_window(sys.argv[sys.argv.index("--login") + 1])
        raise SystemExit(0)
    App().mainloop()
