"""Audit a folder of Forge mod jars for missing deps and duplicate modIds.

Reads META-INF/mods.toml from each jar, builds a modId -> (file, version)
map, parses dependency sections (mandatory + versionRange), and reports
missing deps plus duplicate modIds.

Usage:
  python jar_audit.py --mods DIR

Platform notes: cross-platform (Windows + Linux), stdlib only
(zipfile/re/pathlib). Exit code nonzero if any problem is found.
Unparseable version ranges produce warnings, never crashes.
"""
import argparse
import re
import sys
import zipfile
from pathlib import Path

# Mods provided by the loader itself; never reported as missing.
SKIP_DEPS = {"forge", "minecraft", "java", "mcp"}

RE_MOD_BLOCK = re.compile(r"\[\[mods\]\](.*?)(?=\n\[\[|\n\[|\Z)", re.S)
RE_MODID = re.compile(r'modId\s*=\s*"([^"]+)"')
RE_VERSION = re.compile(r'version\s*=\s*"([^"]+)"')
# Matches both [[dependencies.<id>]] and [dependencies.<id>] styles.
RE_DEP_BLOCK = re.compile(r"\[{1,2}dependencies\.([^\]\s]+)\]{1,2}(.*?)(?=\n\[|\Z)", re.S)
RE_MANDATORY = re.compile(r"mandatory\s*=\s*(true|false)")
RE_RANGE = re.compile(r'versionRange\s*=\s*"([^"]+)"')
RE_NUM = re.compile(r"\d+")


def default_mods_dir():
    return str(Path.home() / "mods")


def ver_tuple(s):
    """Extract leading numeric version tuple from a string, or None."""
    nums = RE_NUM.findall(s or "")
    if not nums:
        return None
    return tuple(int(x) for x in nums[:4])


def range_satisfied(installed: str, range_str: str):
    """Check installed version against a Forge versionRange string.

    Returns (ok: bool, warning: str|None). Unparseable ranges -> (True, warn)
    so they never fail the audit, just warn.
    Handles: exact "1.20.1", "[a,b]", "(a,b)", "[a,)", "(,b]", "a+", "".
    """
    r = (range_str or "").strip()
    if r in ("", "?", "*", "UNBOUNDED"):
        return True, None
    iv = ver_tuple(installed)
    if iv is None:
        return True, "unparseable installed version %r" % installed
    # Trailing-plus form: "1.18+".
    if r.endswith("+"):
        lo = ver_tuple(r[:-1])
        if lo is None:
            return True, "unparseable range %r" % range_str
        width = max(len(iv), len(lo))
        return (iv + (0,) * (width - len(iv))) >= (lo + (0,) * (width - len(lo))), None
    # Interval form with brackets/parens.
    if r[0] in "[(" and r[-1] in "])" and "," in r:
        lo_inc = r[0] == "["
        hi_inc = r[-1] == "]"
        lo_s, hi_s = r[1:-1].split(",", 1)
        lo_s, hi_s = lo_s.strip(), hi_s.strip()
        try:
            if lo_s:
                lo = ver_tuple(lo_s)
                if lo is None:
                    raise ValueError("bad lower bound")
                w = max(len(iv), len(lo))
                ivp, lop = iv + (0,) * (w - len(iv)), lo + (0,) * (w - len(lo))
                if ivp < lop or (ivp == lop and not lo_inc):
                    return False, None
            if hi_s:
                hi = ver_tuple(hi_s)
                if hi is None:
                    raise ValueError("bad upper bound")
                w = max(len(iv), len(hi))
                ivp, hip = iv + (0,) * (w - len(iv)), hi + (0,) * (w - len(hi))
                if ivp > hip or (ivp == hip and not hi_inc):
                    return False, None
            return True, None
        except ValueError:
            return True, "unparseable range %r" % range_str
    # Exact version.
    want = ver_tuple(r)
    if want is None:
        return True, "unparseable range %r" % range_str
    w = max(len(iv), len(want))
    return iv + (0,) * (w - len(iv)) == want + (0,) * (w - len(want)), None


def read_toml(jar_path: Path):
    """Return mods.toml text, or None if the jar has none."""
    try:
        with zipfile.ZipFile(jar_path) as z:
            try:
                return z.read("META-INF/mods.toml").decode("utf-8", "ignore")
            except KeyError:
                return None
    except zipfile.BadZipFile as e:
        raise ValueError("unreadable jar: %s" % e)


def parse_jar(jar_path: Path):
    """Return (mods, deps): mods=[(modId, version)], deps=[(modId, range)]."""
    text = read_toml(jar_path)
    if text is None:
        return None  # signals "no mods.toml"
    mods = []
    for block in RE_MOD_BLOCK.finditer(text):
        body = block.group(1)
        mid = RE_MODID.search(body)
        ver = RE_VERSION.search(body)
        if mid:
            mods.append((mid.group(1), ver.group(1) if ver else "?"))
    deps = []
    for block in RE_DEP_BLOCK.finditer(text):
        _owner, body = block.group(1), block.group(2)
        dm = re.search(r'modId\s*=\s*"([^"]+)"', body)
        if not dm:
            continue
        mm = RE_MANDATORY.search(body)
        if mm and mm.group(1) != "true":
            continue  # optional dependency: not audited
        vm = RE_RANGE.search(body)
        deps.append((dm.group(1), vm.group(1) if vm else ""))
    return mods, deps


def main(argv=None):
    ap = argparse.ArgumentParser(description="Audit Forge mod jars for missing "
                                 "dependencies and duplicate modIds.")
    ap.add_argument("--mods", default=default_mods_dir(),
                    help="folder containing .jar files")
    args = ap.parse_args(argv)
    mods_dir = Path(args.mods)
    if not mods_dir.is_dir():
        print("error: not a directory: %s" % mods_dir, file=sys.stderr)
        return 2

    have = {}       # modId -> list of (file, version)
    reqs = {}       # file -> [(modId, range)]
    no_toml, unreadable = [], []
    warnings = []

    jars = sorted(mods_dir.glob("*.jar"))
    if not jars:
        print("no jars found in %s" % mods_dir)
        return 2

    print("=== MOD LIST ===")
    for jp in jars:
        fn = jp.name
        try:
            parsed = parse_jar(jp)
        except ValueError as e:
            print("%-50s %s" % (fn, e))
            unreadable.append(fn)
            continue
        except Exception as e:  # never crash on one bad jar
            print("%-50s unreadable (%s)" % (fn, e))
            unreadable.append(fn)
            continue
        if parsed is None:
            print("%-50s NO mods.toml" % fn)
            no_toml.append(fn)
            continue
        mods, deps = parsed
        if not mods:
            print("%-50s NO [[mods]] entry (modId unknown)" % fn)
            warnings.append("%s: no modId found" % fn)
        for mid, ver in mods:
            have.setdefault(mid, []).append((fn, ver))
        reqs[fn] = deps
        tag = "; ".join("%s %s" % d if d[1] else d[0] for d in deps) or "(none)"
        print("%-50s modId=%-22s deps=[%s]" %
              (fn, ", ".join("%s@%s" % m for m in mods) or "?", tag))

    problems = 0
    print("\n=== DUPLICATE MODIDS ===")
    dupes = {m: v for m, v in have.items() if len(v) > 1}
    if dupes:
        for mid, owners in sorted(dupes.items()):
            print("DUPLICATE: %s provided by %s" %
                  (mid, ", ".join("%s@%s" % o for o in owners)))
            problems += len(owners) - 1
    else:
        print("(none)")

    print("\n=== DEPENDENCY CHECK ===")
    for fn, deps in sorted(reqs.items()):
        for mid, rng in deps:
            if mid in SKIP_DEPS:
                continue
            if mid not in have:
                print("MISSING: %s needs %s %s" % (fn, mid, rng or "(any)"))
                problems += 1
                continue
            for owner_fn, owner_ver in have[mid]:
                ok, warn = range_satisfied(owner_ver, rng)
                if warn:
                    print("WARN: %s needs %s %r but installed %s@%s is %s" %
                          (fn, mid, rng, owner_fn, owner_ver, warn))
                    warnings.append("%s: %s" % (fn, warn))
                elif not ok:
                    print("MISMATCH: %s needs %s %s but have %s@%s" %
                          (fn, mid, rng, owner_fn, owner_ver))
                    problems += 1

    print("\n=== SUMMARY ===")
    print("jars=%d modIds=%d no-toml=%d unreadable=%d duplicates=%d "
          "missing/mismatch=%d warnings=%d" %
          (len(jars), len(have), len(no_toml), len(unreadable),
           len(dupes), problems, len(warnings)))
    if no_toml:
        print("no mods.toml:", ", ".join(no_toml))
        problems += len(no_toml)
    if unreadable:
        print("unreadable:", ", ".join(unreadable))
        problems += len(unreadable)
    print("DONE.", "OK - no problems." if problems == 0 else
          "PROBLEMS FOUND: %d" % problems)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
