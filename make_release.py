"""Publish helper: run on your PC inside the firmware repo after changing a board's files.

    python make_release.py esp32        # bumps esp32/version.json and records checksums
    python make_release.py pico

It hashes every file listed in that folder's version.json (app.py by default), raises the
version by one, and saves it. Then commit and push; the board picks it up on /update.
"""
import hashlib, json, os, re, sys

PROTECTED = {"config.py", "ota.py", "boot.py", "main.py"}


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    folder = sys.argv[1]
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
        for key in ("WIFI_PASS", "BOT_TOKEN"):
            m = re.search(r'^%s\s*=\s*"([^"]*)"' % key, data.decode(errors="ignore"), re.M)
            if m and m.group(1) and not m.group(1).startswith(("Your", "123456789:ABC")):
                sys.exit("Stopped: %s in %s looks real. Move it to config.py on the board." % (key, path))
        compile(data, path, "exec")             # refuse to publish a file with a syntax error
        files[name] = hashlib.sha256(data).hexdigest()
        print("  %-10s %6d bytes  sha256 %s..." % (name, len(data), files[name][:16]))

    manifest = {"version": int(manifest.get("version", 0)) + 1, "files": files}
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print("%s is now v%d. Commit and push, then send /update to the board." % (manifest_path, manifest["version"]))


if __name__ == "__main__":
    main()
