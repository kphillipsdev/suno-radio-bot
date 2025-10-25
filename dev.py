import asyncio
from pathlib import Path
import subprocess
import sys
from watchfiles import awatch, Change

async def run_dev():
    while True:
        print("Starting bot...")
        proc = subprocess.Popen(
            [sys.executable, 'run.py'],
            cwd=Path.cwd(),
        )
        print("Bot started. Watching for file changes in src/...")

        async for changes in awatch(Path('src')):
            for change_type, path in changes:
                if change_type in (Change.modified, Change.added, Change.deleted) and path.suffix == '.py':
                    print(f"Detected {change_type.name}: {path.relative_to(Path.cwd())}")
                    # Terminate the process
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    print("Restarting bot due to file changes...")
                    break
            else:
                continue
            break

if __name__ == '__main__':
    asyncio.run(run_dev())