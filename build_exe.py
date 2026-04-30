"""
Build a standalone portable executable for amazon_reviews_browser.py.
Works on Windows, macOS, and Linux.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Names
SCRIPT_NAME = "amazon_reviews_browser.py"
EXE_NAME = "amazon_reviews_browser"  # .exe added automatically on Windows


def run(cmd, **kwargs):
    print(f"Running: {' '.join(cmd)}")
    subprocess.check_call(cmd, **kwargs)


def main():
    # 1. Ensure we are in the script's directory
    script_dir = Path(__file__).parent.resolve()
    os.chdir(script_dir)

    # 2. Create a virtual environment if it doesn't exist
    venv_dir = script_dir / "venv"
    if not venv_dir.exists():
        run([sys.executable, "-m", "venv", str(venv_dir)])

    # Paths to the Python and pip executables inside the venv
    if sys.platform == "win32":
        python = str(venv_dir / "Scripts" / "python.exe")
        pip = str(venv_dir / "Scripts" / "pip.exe")
    else:
        python = str(venv_dir / "bin" / "python")
        pip = str(venv_dir / "bin" / "pip")

    # 3. Upgrade pip inside the venv (using python -m pip to avoid file lock)
    run([python, "-m", "pip", "install", "--upgrade", "pip"])

    # 4. Install the required packages
    run(
        [
            pip,
            "install",
            "pyinstaller",
            "playwright",
            "pandas",
            "requests",
            "beautifulsoup4",
        ]
    )

    # 5. Install Playwright browsers (Chromium) using the venv
    run([python, "-m", "playwright", "install", "chromium"])

    # 6. Locate the Playwright cache folder
    if sys.platform == "win32":
        cache_dir = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
    elif sys.platform == "darwin":
        cache_dir = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        cache_dir = Path.home() / ".cache" / "ms-playwright"

    print(f"Playwright cache: {cache_dir}")

    # 7. Copy the whole ms-playwright folder into a local 'browsers' directory
    local_browsers = script_dir / "browsers"
    if local_browsers.exists():
        shutil.rmtree(local_browsers)
    shutil.copytree(str(cache_dir), str(local_browsers))
    print(f"Chromium copied to {local_browsers}")

    # 8. Run PyInstaller to build the executable
    # PyInstaller is installed inside the venv, so use its executable
    if sys.platform == "win32":
        pyinstaller = str(venv_dir / "Scripts" / "pyinstaller.exe")
    else:
        pyinstaller = str(venv_dir / "bin" / "pyinstaller")

    cmd = [
        pyinstaller,
        "--onefile",
        "--name",
        EXE_NAME,
        # Include the local browsers folder
        "--add-data",
        f"browsers{os.pathsep}browsers",
        # Collect all playwright data (drivers, etc.)
        "--collect-all",
        "playwright",
        # Clean previous build files
        "--clean",
        SCRIPT_NAME,
    ]
    run(cmd)

    print(
        f"\n✅ Build complete! Executable is in {script_dir / 'dist' / (EXE_NAME + ('.exe' if sys.platform == 'win32' else ''))}"
    )


if __name__ == "__main__":
    main()
