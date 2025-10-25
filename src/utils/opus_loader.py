
import os, sys, discord

def load_opus_or_warn():
    try:
        discord.opus.load_opus("libopus.so.0")
        if discord.opus.is_loaded():
            print("Opus already loaded")
            return
    except Exception:
        pass

    candidates = []
    env = os.environ.get("OPUS_LIBRARY_PATH")
    if env:
        candidates.append(env)

    if sys.platform.startswith("win"):
        here = os.path.dirname(os.path.abspath(__file__))
        candidates += [
            os.path.join(here, "..", "bin", "libopus-0.dll"),
            os.path.join(here, "bin", "libopus-0.dll"),
            "libopus-0.dll",
            "opus.dll",
        ]
    elif sys.platform == "darwin":
        candidates += [

            "/usr/local/lib/libopus.0.dylib",
            "libopus.0.dylib",
        ]
    else:
        candidates += ["libopus.so.0", "libopus.so"]

    for cand in [c for c in candidates if c]:
        try:
            if sys.platform.startswith("win"):
                d = os.path.dirname(cand)
                if d and os.path.isdir(d):
                    try:
                        os.add_dll_directory(d)
                    except Exception:
                        pass
            discord.opus.load_opus(cand)
            if discord.opus.is_loaded():
                print(f"Opus loaded from: {cand}")
                return
        except Exception:
            continue

    print("Warning: Opus not found. Voice may fall back to raw PCM. "
          "Set OPUS_LIBRARY_PATH or place libopus-0.dll in ./src/bin/ on Windows.")
