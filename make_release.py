"""Publish helper: run on your PC inside the firmware repo.

    python make_release.py pico          # after editing pico/app.py: record checksum, bump version
    python make_release.py esp32

    python make_release.py check pico https://raw.githubusercontent.com/YOU/wol-firmware/main
        compares what GitHub is actually serving with version.json, and says what's wrong.

Line endings are normalised to LF (Unix style) before hashing, so a file edited on
Windows hashes the same as the copy GitHub serves.
"""
import hashlib, json, os, re, sys, time, urllib.request

PROTECTED = {"config.py", "ota.py", "boot.py", "main.py"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def release(folder):
    manifest_path = os.path.join(folder, "version.json")
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except FileNotFoundError:
        manifest = {"version": 0, "files": {"app.py": ""}}

    files = {}
    for name in manifest.get("files") or {"app.py": ""}:
        if name in PROTECTED or "/" in name:
            sys.exit("Refusing to publish %s: boards never overwrite it." % name)
        path = os.path.join(folder, name)
        with open(path, "rb") as f:
            data = f.read()
        if b"\r\n" in data:                       # Windows line endings -> LF, in place
            data = data.replace(b"\r\n", b"\n")
            with open(path, "wb") as f:
                f.write(data)
            print("  converted %s to LF line endings" % path)
        for key in ("WIFI_PASS", "BOT_TOKEN"):
            m = re.search(r'^%s\s*=\s*"([^"]*)"' % key, data.decode(errors="ignore"), re.M)
            if m and m.group(1) and not m.group(1).startswith(("Your", "123456789:ABC")):
                sys.exit("Stopped: %s in %s looks real. Move it to config.py on the board." % (key, path))
        compile(data, path, "exec")             # refuse to publish a file with a syntax error
        files[name] = sha(data)
        print("  %-10s %6d bytes  sha256 %s..." % (name, len(data), files[name][:12]))

    manifest = {"version": int(manifest.get("version", 0)) + 1, "files": files}
    with open(manifest_path, "w", newline="\n") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print("%s is now v%d. Upload/push BOTH app.py and version.json, then send /update."
          % (manifest_path, manifest["version"]))


def fetch(url):
    req = urllib.request.Request(url + "?t=%d" % time.time(), headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def check(folder, base):
    base = base.rstrip("/") + "/" + folder + "/"
    remote = json.loads(fetch(base + "version.json"))
    print("GitHub serves %sversion.json: v%s" % (base, remote.get("version")))
    ok = True
    for name, want in remote["files"].items():
        data = fetch(base + name)
        got = sha(data)
        local_path = os.path.join(folder, name)
        local = open(local_path, "rb").read() if os.path.exists(local_path) else None
        print("  %s: GitHub's copy %s..., version.json expects %s..." % (name, got[:12], want[:12]))
        if got == want:
            print("    OK - the board will accept this.")
            continue
        ok = False
        if sha(data.replace(b"\r\n", b"\n")) == want:
            print("    GitHub's copy has Windows (CRLF) line endings. Upload your local app.py again"
                  " (make_release.py has converted it to LF).")
        elif local is not None and sha(local) == got:
            print("    GitHub's app.py matches your local file, but version.json is stale: upload version.json.")
        elif local is not None and sha(local) == want:
            print("    Your local app.py is right but GitHub still serves an older one: upload app.py,"
                  " or wait ~5 minutes if you just did (GitHub caches raw files).")
        else:
            print("    Neither matches your local copy. Run 'python make_release.py %s', then upload"
                  " both files." % folder)
    print("All good." if ok else "Fix the above, then run this check again before sending /update.")


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 1:
        release(a[0])
    elif len(a) == 3 and a[0] == "check":
        check(a[1], a[2])
    else:
        sys.exit(__doc__)
