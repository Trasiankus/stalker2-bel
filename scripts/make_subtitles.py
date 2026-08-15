"""Generate the cutscene subtitle files from subtitles.json.

    python make_subtitles.py [--lang bel] [--out FOLDER]

Writes one .srt per cutscene, named the way the game expects
(<cutscene>_ru.srt -- the mod replaces the Russian track), into

    mod/Stalker2/Content/Movies/TempCutscenes/Cutscenes/

which mirrors the game folder, so installing is a plain copy of everything
inside mod/ into the game's root directory.

Standard library only, Python 3.8+. Nothing else from this repo is needed --
run make_mod.py instead if you also want the .pak rebuilt.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCE = os.path.join(ROOT, "subtitles.json")
DEFAULT_OUT = os.path.join(ROOT, "mod", "Stalker2", "Content", "Movies",
                           "TempCutscenes", "Cutscenes")

# The game's own files are inconsistent about the BOM (8 of 13 carry one) and
# it parses both, so we always write it and stay deterministic.
BOM = "﻿"


def crlf(text):
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def render(subtitles, lang):
    """-> {filename: srt text}, cues grouped by cutscene, in key order."""
    files = {}
    for key, cue in subtitles.items():
        files.setdefault(key.split("#")[0], []).append(cue)

    out = {}
    for base, cues in files.items():
        body = "".join(
            "{}\r\n{}\r\n{}\r\n\r\n".format(i, c["time"], crlf(c.get(lang, "")))
            for i, c in enumerate(cues, start=1))
        out[base + "_ru.srt"] = BOM + body
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lang", default="bel", choices=["bel", "ua", "ru"],
                    help="which column to write (default: bel)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="output folder (default: ./mod/<game path>/Cutscenes)")
    args = ap.parse_args()

    if not os.path.isfile(SOURCE):
        sys.exit("subtitles.json not found at %s" % SOURCE)

    with open(SOURCE, "r", encoding="utf-8-sig") as f:
        subtitles = json.load(f)

    files = render(subtitles, args.lang)
    os.makedirs(args.out, exist_ok=True)

    empty = 0
    for name, text in sorted(files.items()):
        with open(os.path.join(args.out, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)
        print("  {}".format(name))
    empty = sum(1 for c in subtitles.values() if not c.get(args.lang, "").strip())

    print("\n{} cues -> {} file(s) in {}".format(len(subtitles), len(files), args.out))
    if empty:
        print("warning: {} cue(s) have no '{}' text and were written blank"
              .format(empty, args.lang))


if __name__ == "__main__":
    main()
