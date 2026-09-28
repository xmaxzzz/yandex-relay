"""Build yandex_relay.c4z and check that the driver versions agree.

A .c4z is a zip with driver.lua, driver.xml and www/icons/*.png at the root
(no wrapper folder, forward-slash paths). Run: python c4-driver/build.py
"""

import os
import re
import sys
import zipfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ["driver.lua", "driver.xml", "www/icons/device_lg.png", "www/icons/device_sm.png"]
OUT = os.path.join(HERE, "yandex_relay.c4z")


def read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def main():
    lua_version = re.search(r'local DRIVER_VERSION\s*=\s*"([^"]+)"', read("driver.lua")).group(1)
    xml = read("driver.xml")
    prop_version = re.search(
        r"<name>Driver Version</name>\s*<type>STRING</type>\s*<default>([^<]*)</default>", xml
    ).group(1)
    if lua_version != prop_version:
        sys.exit(f"version mismatch: driver.lua {lua_version} vs driver.xml property {prop_version}")

    # Stamp <modified> with the build time: identical dates on every build are
    # a suspect for Composer's "Update Driver" not replacing the driver.
    stamp = datetime.now().strftime("%m/%d/%Y %H:%M")
    xml = re.sub(r"<modified>[^<]*</modified>", f"<modified>{stamp}</modified>", xml, count=1)
    with open(os.path.join(HERE, "driver.xml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(xml)

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            z.write(os.path.join(HERE, name), name)
    build = re.search(r"<version>(\d+)</version>", xml).group(1)
    print(f"{OUT}  v{lua_version} (package build {build}, modified {stamp})")


if __name__ == "__main__":
    main()
