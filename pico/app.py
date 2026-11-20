# Remote Wake-on-LAN via a Telegram bot  -  MicroPython
# Runs on: ESP32 / ESP32-S3 / ESP32-C3 boards, Raspberry Pi Pico 2 W / Pico W
#
# The board makes OUTBOUND HTTPS requests to Telegram asking for new messages.
# Send "/wake desktop" to its bot from anywhere and the board sends the magic
# packet on its LAN. No port forwarding, VPN or router changes are needed.
#
# Give EACH board its own bot (two boards can't share one bot's messages).
# Commands:  /wake [name]   /status   /list   /help   /version   /update   /rollback
#            /add name MAC [ip] [port]   /remove name   (PC list changes, saved on the board)
#
# Over-the-air updates: this file runs as app.py, started by main.py + ota.py.
# Your own settings go in config.py on the board (never on GitHub).

import network, socket, time, machine, ubinascii, json, gc, sys
try:
    import requests
except ImportError:
    import urequests as requests

# ---------------- CONFIG ----------------
WIFI_SSID = ""
WIFI_PASS = ""

BOT_TOKEN   = ""   # this board's own bot
DEVICE_NAME = "pico"            # shown on replies, e.g. "esp32" or "pico"

# Telegram user IDs allowed to give commands (works in private chats and groups).
# Leave empty at first, message the bot, and the board prints your ID in Thonny.
ALLOWED_USERS = ()               # e.g. (123456789,)  or for two people (111, 222)

# name -> settings. "ip"/"port" are optional; they let the board confirm the PC
# came up (3389 = Remote Desktop, 445 = Windows file sharing, 22 = SSH).
TARGETS = {
    "xxx": {"mac": "", "ip": "", "port": 22},
}

# "auto" = send to BOTH 255.255.255.255 and this network's own broadcast address
# (e.g. 192.168.1.255, worked out from the board's IP), like routers such as
# pfSense do. Or set one address, e.g. "192.168.1.255".
BROADCAST   = "255.255.255.255"
WOL_PORTS   = (9, 7)             # the two standard WoL ports; most PCs ignore the port
BOOT_WAIT_S = 120                # how long to wait for the PC after waking it
MAX_AGE_S   = 300                # ignore commands older than this (needs the clock, set via NTP)
LONG_POLL_S = 20                 # Telegram holds each request open up to this long
NTP_HOST    = ""                 # e.g. your router's IP if it serves time; "" = public servers

# Status LED. "auto" = "LED" on a Pico, GPIO 2 on an ESP32 (the blue LED on most
# ESP32 DevKit-style boards). Set a GPIO number for other boards, or None for no LED.
LED_PIN = "auto"

# Over-the-air updates (see the "OTA updates" tab in the guide)
OTA_URL     = ""                 # e.g. "https://raw.githubusercontent.com/you/wol-firmware/main"
OTA_CHANNEL = "auto"             # folder in that repo: "auto" = "esp32" or "pico"
# ----------------------------------------

# Settings in config.py (kept on the board) replace the ones above, so this file
# can live on GitHub without your Wi-Fi password or bot token.
try:
    from config import *
except ImportError:
    pass

try:
    import ota
except ImportError:
    ota = None

IS_PICO = sys.platform == "rp2"
API = "https://api.telegram.org/bot" + BOT_TOKEN + "/"

# Accept 123, (123), (123,), [123, 456] or "123" - (123) without a comma is a plain number.
if isinstance(ALLOWED_USERS, (int, str)):
    ALLOWED_USERS = (ALLOWED_USERS,)
ALLOWED_USERS = tuple(int(u) for u in ALLOWED_USERS)
print("Allowed user IDs:", ALLOWED_USERS or "none yet - message the bot to see your ID")


def show_error(label, e):
    print(label, e)
    if hasattr(sys, "print_exception"):   # MicroPython: show the line that failed
        sys.print_exception(e)


class Led:
    def __init__(self, pin):
        if pin == "auto":
            pin = "LED" if IS_PICO else 2
        try:
            self.p = machine.Pin(pin, machine.Pin.OUT) if pin is not None else None
        except Exception:
            self.p = None
    def on(self):
        if self.p: self.p.value(1)
    def off(self):
        if self.p: self.p.value(0)
    def toggle(self):
        if self.p: self.p.value(0 if self.p.value() else 1)


led = Led(LED_PIN)
wlan = network.WLAN(network.STA_IF)


# ---------- Wi-Fi + clock ----------
def connect_wifi():
    wlan.active(True)
    try:                            # disable Wi-Fi power saving for reliability
        wlan.config(pm=0xa11140 if IS_PICO else network.WLAN.PM_NONE)
    except Exception:
        pass
    if not wlan.isconnected():
        wlan.connect(WIFI_SSID, WIFI_PASS)
        for _ in range(30):
            if wlan.isconnected():
                break
            led.toggle()
            time.sleep(1)
    if not wlan.isconnected():
        print("Wi-Fi failed, rebooting in 10 s")
        time.sleep(10)
        machine.reset()
    led.on()
    print("Wi-Fi connected, IP", wlan.ifconfig()[0])


# Some ports count time from 2000 instead of 1970; Telegram uses 1970.
EPOCH_OFFSET = 946684800 if time.gmtime(0)[0] == 2000 else 0
clock_ok = False


def sync_clock():
    """Set the clock over NTP. Tries NTP_HOST first, then public servers."""
    global clock_ok, last_ntp_try
    last_ntp_try = time.ticks_ms()
    try:
        import ntptime
    except ImportError:
        return
    hosts = [h for h in (NTP_HOST, "pool.ntp.org", "time.google.com", "time.cloudflare.com") if h]
    for host in hosts:
        try:
            ntptime.host = host
            ntptime.timeout = 3
            ntptime.settime()
            clock_ok = True
            print("Clock set from", host)
            return
        except Exception as e:
            print("NTP via %s failed: %s" % (host, e))
    print("NTP failed everywhere (old commands won't be filtered by age); will retry hourly")


last_ntp_try = 0


def now_unix():
    return time.time() + EPOCH_OFFSET


# ---------- Wake-on-LAN ----------
def magic_packet(mac):
    mac_bytes = ubinascii.unhexlify(mac.replace(":", "").replace("-", ""))
    if len(mac_bytes) != 6:
        raise ValueError("bad MAC: " + mac)
    return b"\xff" * 6 + mac_bytes * 16


def broadcast_addrs():
    if BROADCAST != "auto":
        return [BROADCAST]
    ip, mask = wlan.ifconfig()[:2]
    subnet_bcast = ".".join(str(int(a) | (~int(m) & 255))
                            for a, m in zip(ip.split("."), mask.split(".")))
    out = ["255.255.255.255"]
    if subnet_bcast not in out:
        out.append(subnet_bcast)
    return out


def send_wol(mac):
    """Send the magic packet; returns the addresses it went out to."""
    pkt = magic_packet(mac)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except Exception:
        pass
    sent = []
    try:
        for addr in broadcast_addrs():
            ok = False
            for port in WOL_PORTS:
                for _ in range(3):      # send a few times; UDP can drop
                    try:
                        s.sendto(pkt, (addr, port))
                        ok = True
                    except OSError as e:
                        print("send to %s:%d failed: %s" % (addr, port, e))
                    time.sleep_ms(30)
            if ok:
                sent.append(addr)
    finally:
        s.close()
    print("Magic packet for %s sent to %s, ports %s (board IP %s)"
          % (mac, ", ".join(sent) or "NOWHERE", WOL_PORTS, wlan.ifconfig()[0]))
    if not sent:
        raise OSError("could not send magic packet")
    for _ in range(3):              # blink to confirm
        led.off(); time.sleep_ms(100); led.on(); time.sleep_ms(100)
    return sent


def is_up(t):
    """True/False if the target answers on its TCP port, None if no check set."""
    if not t.get("ip"):
        return None
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(socket.getaddrinfo(t["ip"], t.get("port", 3389))[0][-1])
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------- Telegram ----------
long_poll_ok = True     # turned off if this MicroPython's requests lacks timeout=


def tg(method, payload, wait=0):
    global long_poll_ok
    gc.collect()                    # TLS needs a big chunk of RAM on a plain ESP32
    if wait and long_poll_ok:
        try:
            r = requests.post(API + method, json=payload, timeout=wait + 15)
        except TypeError:
            print("requests has no timeout support; using short polling")
            long_poll_ok = False
            payload["timeout"] = 0
            r = requests.post(API + method, json=payload)
    else:
        r = requests.post(API + method, json=payload)
    try:
        return r.json()
    finally:
        r.close()


def reply(chat, text):
    print(text)
    try:
        tg("sendMessage", {"chat_id": chat, "text": "[%s] %s" % (DEVICE_NAME, text)})
    except Exception as e:
        print("send failed:", e)


# ---------- PC list, editable from Telegram ----------
# /add and /remove save the list to targets.json on the board. Once that file exists it
# replaces TARGETS from config.py / this file, and it survives restarts and updates.
TARGETS_FILE = "targets.json"


def load_targets():
    global TARGETS
    try:
        with open(TARGETS_FILE) as f:
            TARGETS = json.load(f)
        print("PC list from %s: %s" % (TARGETS_FILE, ", ".join(TARGETS) or "(empty)"))
    except (OSError, ValueError):
        pass


def save_targets():
    with open(TARGETS_FILE + ".tmp", "w") as f:
        json.dump(TARGETS, f)
    try:
        import os
        try:
            os.remove(TARGETS_FILE)
        except OSError:
            pass
        os.rename(TARGETS_FILE + ".tmp", TARGETS_FILE)
    except OSError as e:
        print("couldn't save PC list:", e)


def clean_mac(mac):
    hexs = mac.replace(":", "").replace("-", "").replace(".", "").upper()
    if len(hexs) != 12 or any(c not in "0123456789ABCDEF" for c in hexs):
        return None
    return ":".join(hexs[i:i + 2] for i in range(0, 12, 2))


def valid_ip(ip):
    p = ip.split(".")
    return len(p) == 4 and all(x.isdigit() and 0 <= int(x) <= 255 for x in p)


def add_target(chat, parts):
    usage = ("Usage: /add name MAC [ip] [port]\n"
             "e.g. /add desktop AA:BB:CC:DD:EE:FF 192.168.1.50 3389\n"
             "The IP and port are optional; they let me check the PC came up.")
    if len(parts) < 3:
        reply(chat, usage)
        return
    name, mac = parts[1].lower(), clean_mac(parts[2])
    if not mac:
        reply(chat, "That MAC doesn't look right: %s\n%s" % (parts[2], usage))
        return
    if name.startswith("/") or len(name) > 20:
        reply(chat, "Pick a short name without a slash.")
        return
    t = {"mac": mac}
    if len(parts) > 3:
        if not valid_ip(parts[3]):
            reply(chat, "That IP doesn't look right: %s" % parts[3])
            return
        t["ip"] = parts[3]
        port = parts[4] if len(parts) > 4 else "3389"
        if not port.isdigit() or not 0 < int(port) < 65536:
            reply(chat, "The port should be a number like 3389, 445 or 22.")
            return
        t["port"] = int(port)
    existed = name in TARGETS
    TARGETS[name] = t
    save_targets()
    where = " (%s port %d)" % (t["ip"], t["port"]) if "ip" in t else ""
    reply(chat, "%s %s: %s%s.\nPCs: %s" % ("Updated" if existed else "Added", name, mac, where,
                                          ", ".join(TARGETS)))


def remove_target(chat, name):
    if name not in TARGETS:
        reply(chat, "No PC called %s. PCs: %s" % (name, ", ".join(TARGETS) or "none"))
        return
    del TARGETS[name]
    save_targets()
    reply(chat, "Removed %s. PCs: %s" % (name, ", ".join(TARGETS) or "none - use /add"))


def describe(up):
    return {True: "ON", False: "off / not responding", None: "unknown (no IP set)"}[up]


def handle(chat, text):
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]        # handles "/wake@MyBot"
    arg = parts[1].lower() if len(parts) > 1 else None

    if cmd in ("/start", "/help"):
        reply(chat, "Commands:\n/wake [name]\n/status\n/list\n/add name MAC [ip] [port]\n/remove name\n"
                    "/version\n/update\n/rollback\n\nTargets: " + (", ".join(TARGETS) or "none yet"))

    elif cmd == "/list":
        reply(chat, "\n".join("%s  %s%s" % (n, t["mac"], "  %s:%s" % (t["ip"], t.get("port", 3389))
                                                   if t.get("ip") else "")
                              for n, t in TARGETS.items()) or "No PCs yet. Use /add name MAC [ip] [port]")

    elif cmd == "/add":
        add_target(chat, text.strip().split())

    elif cmd == "/remove":
        if arg is None:
            reply(chat, "Usage: /remove name")
        else:
            remove_target(chat, arg)

    elif cmd == "/status":
        lines = ["%s: %s" % (n, describe(is_up(t))) for n, t in TARGETS.items()]
        lines.append("Board up %d min, IP %s" % (time.ticks_ms() // 60000, wlan.ifconfig()[0]))
        reply(chat, "\n".join(lines))

    elif cmd == "/version":
        v = ota.version() if ota else 0
        reply(chat, "Firmware v%d%s" % (v, "" if OTA_URL else " (OTA_URL isn't set)"))

    elif cmd in ("/update", "/rollback"):
        if ota is None:
            reply(chat, "ota.py isn't on this board, so it can't update itself.")
            return
        if cmd == "/update":
            if not OTA_URL:
                reply(chat, "Set OTA_URL in config.py first.")
                return
            ota.request(chat, force=(arg == "force"))
            reply(chat, "Checking for an update and restarting. I'll report back in about a minute.")
        else:
            ota.request_rollback(chat)
            reply(chat, "Going back to the previous firmware. Restarting...")
        time.sleep(1)
        machine.reset()

    elif cmd == "/wake":
        if arg is None and len(TARGETS) == 1:
            arg = next(iter(TARGETS))
        if arg not in TARGETS:
            reply(chat, "Which one? Targets: " + ", ".join(TARGETS))
            return
        t = TARGETS[arg]
        if is_up(t):
            reply(chat, arg + " is already on.")
            return
        via = ", ".join(send_wol(t["mac"]))
        if not t.get("ip"):
            reply(chat, "Magic packet sent to %s (via %s)." % (arg, via))
            return
        reply(chat, "Magic packet sent to %s (via %s), waiting for it to boot..." % (arg, via))
        start = time.time()
        while time.time() - start < BOOT_WAIT_S:
            time.sleep(10)
            if is_up(t):
                reply(chat, "%s is up (%d s)." % (arg, time.time() - start))
                return
        reply(chat, "%s didn't respond within %d s. It may still be booting, or WoL "
                    "may be off. Try /status in a minute." % (arg, BOOT_WAIT_S))
    # anything else (ordinary chat in a group) is ignored silently


def ota_report():
    """Telegram works: confirm a trial update and report any update result."""
    if ota is None:
        return
    try:
        chat, msg = ota.confirm()
    except Exception as e:
        show_error("ota confirm:", e)
        return
    if msg:
        chat = chat or (ALLOWED_USERS[0] if ALLOWED_USERS else None)
        if chat is not None:
            reply(chat, msg)
        else:
            print(msg)


# ---------- main loop ----------
def main():
    load_targets()
    connect_wifi()
    sync_clock()

    # Skip any messages sent while the board was off, so an old /wake
    # doesn't fire unexpectedly after a power cut.
    offset = None
    while offset is None:
        try:
            res = tg("getUpdates", {"offset": -1, "timeout": 0})
            if not res.get("ok"):
                raise RuntimeError(res.get("description", "Telegram error"))
            offset = res["result"][-1]["update_id"] + 1 if res["result"] else 0
        except Exception as e:
            show_error("initial check failed:", e)
            time.sleep(10)
    print("[%s] Listening for Telegram commands..." % DEVICE_NAME)
    ota_report()

    fails = 0
    while True:
        try:
            if not wlan.isconnected():
                connect_wifi()
            if not clock_ok and time.ticks_diff(time.ticks_ms(), last_ntp_try) > 3600000:
                sync_clock()
            res = tg("getUpdates", {"offset": offset, "timeout": LONG_POLL_S,
                                    "allowed_updates": ["message"]}, wait=LONG_POLL_S)
            if not res.get("ok"):
                raise RuntimeError(res.get("description", "Telegram error"))
            for u in res.get("result", []):
                offset = u["update_id"] + 1          # mark handled first
                msg = u.get("message") or {}
                text = msg.get("text", "")
                chat = msg.get("chat", {}).get("id")
                user = msg.get("from", {}).get("id")
                if not text.startswith("/"):
                    continue
                if user not in ALLOWED_USERS:
                    print("Ignored %r from user id %s (chat id %s)" % (text, user, chat))
                    continue
                if clock_ok and now_unix() - msg.get("date", 0) > MAX_AGE_S:
                    print("Skipping old command:", text)
                    continue
                handle(chat, text)
            fails = 0
            if not long_poll_ok:
                time.sleep(3)
        except Exception as e:
            fails += 1
            show_error("poll error (%d):" % fails, e)
            if fails >= 10:                          # e.g. internet down for a while
                machine.reset()
            time.sleep(min(60, 5 * fails))


main()
