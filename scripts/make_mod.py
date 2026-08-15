"""Build the whole Belarusian mod from localization.json + subtitles.json.

    python scripts/make_mod.py --check   list dependencies, say what is missing
    python scripts/make_mod.py          build into ./mod/

The output mirrors the game folder, so installing is a plain copy:

    mod/
      Stalker2/Content/Paks/bel_stalker.{pak,ucas,utoc}
      Stalker2/Content/Movies/TempCutscenes/Cutscenes/*.srt

Copy everything inside mod/ into the game's root folder, then pick "Русский"
as the text language in game.

WHAT YOU NEED
-------------
  1. Windows x64                 -- the three tools below are Win64 binaries
  2. Python 3.9+                 -- standard library only, no pip install
  3. S.T.A.L.K.E.R. 2 installed  -- the build reads the game's own
                                    LocalizationDB and repacks against it, so
                                    the pak always matches your game version
  4. tools/S2HOC_LocEditor.exe   -- LocalizationDB .ubulk <-> .json
  5. tools/UnrealReZen.exe       -- builds the .utoc/.ucas/.pak
  6. tools/oo2core_9_win64.dll   -- Oodle codec, used to read the game's
                                    compressed containers and by UnrealReZen

Put items 4-6 in a `tools/` folder at the repo root (or pass --tools).
The game is found automatically in the usual Steam locations; override with
--game-dir or the STALKER2_DIR environment variable.

Only `bel` is read from the json files -- `ua` and `ru` are reference text.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

LOC_JSON = os.path.join(ROOT, "localization.json")
SUBS_JSON = os.path.join(ROOT, "subtitles.json")

PACKAGE = "Stalker2/Content/_Stalker_2/Localization/LocalizationDB"
PAKS_REL = os.path.join("Stalker2", "Content", "Paks")
SUBS_REL = os.path.join("Stalker2", "Content", "Movies", "TempCutscenes", "Cutscenes")

LANG_RU = 17            # the slot the mod overwrites
PAK_NAME = "bel_stalker"

TOOL_FILES = ["S2HOC_LocEditor.exe", "UnrealReZen.exe", "oo2core_9_win64.dll"]

STEAM_GUESSES = [
    r"C:\Games\Steam\steamapps\common\S.T.A.L.K.E.R. 2 Heart of Chornobyl",
    r"C:\Program Files (x86)\Steam\steamapps\common\S.T.A.L.K.E.R. 2 Heart of Chornobyl",
    r"D:\Steam\steamapps\common\S.T.A.L.K.E.R. 2 Heart of Chornobyl",
    r"D:\SteamLibrary\steamapps\common\S.T.A.L.K.E.R. 2 Heart of Chornobyl",
    r"E:\SteamLibrary\steamapps\common\S.T.A.L.K.E.R. 2 Heart of Chornobyl",
]


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------

def find_game(explicit=None):
    for candidate in filter(None, [explicit, os.environ.get("STALKER2_DIR")] + STEAM_GUESSES):
        if os.path.isdir(os.path.join(candidate, "Stalker2", "Content", "Paks")):
            return candidate
    return None


def find_tools(explicit=None):
    return explicit or os.path.join(ROOT, "tools")


def check(game_dir, tools_dir):
    """-> list of human-readable problems, empty when everything is present."""
    problems = []

    if sys.platform != "win32":
        problems.append("this build needs Windows (the tools are Win64 binaries)")
    if sys.version_info < (3, 9):
        problems.append("Python 3.9+ required, found %d.%d" % sys.version_info[:2])

    for name, path in (("localization.json", LOC_JSON), ("subtitles.json", SUBS_JSON)):
        if not os.path.isfile(path):
            problems.append("missing %s at the repo root" % name)

    if game_dir:
        print("  game    : %s" % game_dir)
    else:
        problems.append("S.T.A.L.K.E.R. 2 not found -- pass --game-dir or set STALKER2_DIR")

    print("  tools   : %s" % tools_dir)
    for name in TOOL_FILES:
        path = os.path.join(tools_dir, name)
        print("    %-24s %s" % (name, "OK" if os.path.isfile(path) else "MISSING"))
        if not os.path.isfile(path):
            problems.append("missing tools/%s" % name)

    return problems


# --------------------------------------------------------------------------
# build steps
# --------------------------------------------------------------------------

def loc_editor(tools_dir, mode, path):
    exe = os.path.join(tools_dir, "S2HOC_LocEditor.exe")
    subprocess.run([exe, mode, path], check=True, cwd=os.path.dirname(path))


def extract_localization_db(game_dir, out_dir):
    """Pull LocalizationDB.uasset/.ubulk out of the installed game."""
    sys.path.insert(0, HERE)
    from iostore import CHUNK_TYPE_EXT, IoStoreContainer

    paks = os.path.join(game_dir, "Stalker2", "Content", "Paks")
    found = None
    for name in sorted(os.listdir(paks)):
        if not name.endswith(".utoc") or name.startswith("global"):
            continue
        try:
            container = IoStoreContainer(os.path.join(paks, name))
        except Exception as exc:                       # noqa: BLE001
            print("    skipping %s: %s" % (name, exc))
            continue
        hit = next((p for p in container.paths if p.rsplit(".", 1)[0] == PACKAGE), None)
        if hit:
            if found:
                found[0].close()
            found = (container, hit, name)
        else:
            container.close()

    if not found:
        sys.exit("LocalizationDB not found in %s" % paks)

    container, path, name = found
    print("  found LocalizationDB in %s" % name)
    with container:
        for chunk_type, index in sorted(container.package_chunks(path).items()):
            ext = CHUNK_TYPE_EXT.get(chunk_type)
            if ext is None:
                continue
            target = os.path.join(out_dir, "LocalizationDB" + ext)
            print("    extracting %s (%.1f MB)"
                  % (os.path.basename(target), container.offsets[index][1] / 1e6))
            with open(target, "wb") as f:
                f.write(container.read_entry(index))


def apply_translation(db, localization):
    russian = db[LANG_RU]
    applied = skipped = 0
    for key, value in localization.items():
        text = value.get("bel", "")
        if not text.strip():
            continue
        if key not in russian:
            skipped += 1
            continue
        russian[key] = text
        applied += 1
    return applied, skipped


def repack(tools_dir, game_dir, staging, out_dir):
    exe = os.path.join(tools_dir, "UnrealReZen.exe")
    utoc = os.path.join(out_dir, PAK_NAME + ".utoc")
    subprocess.run(
        [exe,
         "--content-path", staging,
         "--compression-format", "Zlib",
         "--engine-version", "GAME_UE5_1",
         "--game-dir", game_dir,
         "--output-path", utoc],
        check=True,
        cwd=tools_dir)        # UnrealReZen looks for the Oodle dll in the cwd
    if not os.path.isfile(utoc):
        sys.exit("UnrealReZen did not produce a .utoc")
    return utoc


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="only report whether the dependencies are in place")
    ap.add_argument("--game-dir", help="S.T.A.L.K.E.R. 2 install folder")
    ap.add_argument("--tools", help="folder holding the three tool files")
    ap.add_argument("--out", default=os.path.join(ROOT, "mod"),
                    help="output folder (default: <repo>/mod)")
    args = ap.parse_args()

    game_dir = find_game(args.game_dir)
    tools_dir = find_tools(args.tools)

    print("checking dependencies ...")
    problems = check(game_dir, tools_dir)
    if problems:
        print("\nnot ready to build:")
        for p in problems:
            print("  - %s" % p)
        sys.exit(0 if args.check else 1)
    print("  all dependencies present")
    if args.check:
        return

    paks_out = os.path.join(args.out, PAKS_REL)
    subs_out = os.path.join(args.out, SUBS_REL)
    os.makedirs(paks_out, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="s2bel-") as tmp:
        # content/ holds only what goes into the pak; build/ takes the output,
        # so UnrealReZen never sees its own artefacts in the content path.
        content = os.path.join(tmp, "content")
        build = os.path.join(tmp, "build")
        staging = os.path.join(content, "Stalker2", "Content", "_Stalker_2", "Localization")
        os.makedirs(staging)
        os.makedirs(build)

        print("\nreading the game's LocalizationDB ...")
        extract_localization_db(game_dir, staging)

        print("  converting to json ...")
        loc_editor(tools_dir, "-to-json", os.path.join(staging, "LocalizationDB.ubulk"))

        db_path = os.path.join(staging, "LocalizationDB.json")
        with open(db_path, "r", encoding="utf-8-sig") as f:
            db = json.load(f)
        with open(LOC_JSON, "r", encoding="utf-8-sig") as f:
            localization = json.load(f)

        applied, skipped = apply_translation(db, localization)
        print("  translated %d strings into slot %d (%d keys are not in this "
              "game build)" % (applied, LANG_RU, skipped))

        with open(db_path, "w", encoding="utf-8-sig") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)

        print("  converting back to .ubulk ...")
        loc_editor(tools_dir, "-to-bulk", db_path)
        os.remove(db_path)                 # must not end up inside the pak

        print("  repacking ...")
        utoc = repack(tools_dir, game_dir, content, build)
        for ext in (".pak", ".ucas", ".utoc"):
            src = os.path.splitext(utoc)[0] + ext
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(paks_out, os.path.basename(src)))
                print("    %s (%.1f MB)" % (os.path.basename(src),
                                            os.path.getsize(src) / 1e6))

    print("\nwriting subtitles ...")
    subprocess.run([sys.executable, os.path.join(HERE, "make_subtitles.py"),
                    "--out", subs_out], check=True)

    print("\nBuilt: %s" % args.out)
    print("Copy everything inside it into your game folder:")
    print("  %s" % game_dir)
    print('Then set the text language to "Русский" in game.')


if __name__ == "__main__":
    main()
