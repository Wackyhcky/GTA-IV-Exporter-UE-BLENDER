"""Builds "GTA IV Exporter.exe" and the Blender add-on zip.

Usage: double-click build_exe.bat (or run: python build_exe.py)

Everything (virtual env, temp files, pip downloads) stays inside the .build
folder next to this script, so your main Python install is not touched.
Output:
    dist/GTA IV Exporter/GTA IV Exporter.exe   (keep with its _internal folder)
    dist/gtaiv_exporter_addon.zip                   (Blender: Install from Disk)
"""

import os
import shutil
import subprocess
import sys
import venv
import zipfile

SRC = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(SRC, ".build")
VENV = os.path.join(BUILD, "venv")
DIST = os.path.join(SRC, "dist")
APP = "GTA IV Exporter"
PACKAGES = ["pyinstaller", "numpy", "pycryptodome", "sv-ttk"]  # sv-ttk: Windows 11 look for Tk


def run(cmd, env):
    print(">", " ".join('"%s"' % c if " " in c else c for c in cmd), flush=True)
    subprocess.run(cmd, check=True, env=env)


def main():
    tmp = os.path.join(BUILD, "tmp")
    os.makedirs(tmp, exist_ok=True)
    env = dict(os.environ, TEMP=tmp, TMP=tmp, PIP_NO_CACHE_DIR="1")

    py = os.path.join(VENV, "Scripts", "python.exe")
    if not os.path.isfile(py):
        print("Creating build environment...", flush=True)
        venv.create(VENV, with_pip=True)
        run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"], env)
    run([py, "-m", "pip", "install", "-q"] + PACKAGES, env)

    for d in (os.path.join(SRC, "gtaiv_exporter", "__pycache__"),):
        shutil.rmtree(d, ignore_errors=True)
    sep = os.pathsep  # ';' on Windows
    run([py, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
         "--name", APP,
         "--collect-data", "sv_ttk",
         "--add-data", os.path.join(SRC, "gtaiv_exporter") + sep + "gtaiv_exporter",
         "--add-data", os.path.join(SRC, "blender_runner.py") + sep + ".",
         "--add-data", os.path.join(SRC, "unreal_runner.py") + sep + ".",
         "--add-data", os.path.join(SRC, "unreal_lights.py") + sep + ".",
         "--add-data", os.path.join(SRC, "unreal_peds.py") + sep + ".",
         "--add-data", os.path.join(SRC, "UnrealPlugin", "GTAIVTraffic_Built") + sep + os.path.join("UnrealPlugin", "GTAIVTraffic"),
         "--distpath", DIST,
         "--workpath", os.path.join(BUILD, "work"),
         "--specpath", BUILD,
         os.path.join(SRC, "gtaiv_exporter_gui.py")], env)

    shutil.copy(os.path.join(SRC, "app_README.txt"), os.path.join(DIST, APP, "README.txt"))

    addon_zip = os.path.join(DIST, "gtaiv_exporter_addon.zip")
    pkg = os.path.join(SRC, "gtaiv_exporter")
    with zipfile.ZipFile(addon_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(pkg)):
            p = os.path.join(pkg, f)
            if os.path.isfile(p) and not f.endswith((".pyc", ".bin")):
                z.write(p, "gtaiv_exporter/" + f)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nDone:")
    print("  " + os.path.join(DIST, APP, APP + ".exe"))
    print("  " + addon_zip)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print("\nBuild failed (%s)" % e)
        sys.exit(1)
