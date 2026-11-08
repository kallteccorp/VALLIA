import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
ENGINE = Path(r"C:\Users\valle\Documents\Codex\2026-11-08\f\work\deepseek-local\launch.py")
ORIGIN = "http://127.0.0.1:8093"
LOG = ROOT / "webapp.log"
TOKEN_FILE = ROOT / "token.txt"


def server_env():
    env = dict(os.environ)
    secret = TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else ""
    if secret:
        env["VALLIA_TOKEN"] = secret
    return env


def running():
    try:
        with urlopen(ORIGIN + "/health", timeout=1) as response:
            return json.load(response).get("app") == "vallia"
    except (OSError, ValueError):
        return False


def main():
    if not ENGINE.is_file():
        raise RuntimeError(f"O inicializador do modelo não existe mais em {ENGINE}.")
    subprocess.run([sys.executable, "-B", str(ENGINE), "--no-browser"], check=True)
    if running():
        return
    with LOG.open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "server.py")],
            cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
            env=server_env(),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    for _ in range(60):
        if running():
            return
        if process.poll() is not None:
            raise RuntimeError(f"O servidor da VALLIA não iniciou. Consulte {LOG}.")
        time.sleep(0.25)
    raise RuntimeError("O servidor da VALLIA demorou para iniciar.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
    print(ORIGIN + "/")
    if "--no-browser" not in sys.argv[1:]:
        webbrowser.open(ORIGIN + "/")
