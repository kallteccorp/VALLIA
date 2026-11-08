"""Abre um túnel Cloudflare para a VALLIA local e diz o que colar no Render."""
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / "token.txt"
LOG_FILE = ROOT / "cloudflared.log"
PORT = 8093
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def token():
    if TOKEN_FILE.exists():
        value = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(value, encoding="utf-8")
    return value


def binary():
    found = shutil.which("cloudflared")
    if found:
        return found
    local = ROOT / "cloudflared.exe"
    return str(local) if local.is_file() else None


def main():
    cloudflared = binary()
    secret = token()
    if not cloudflared:
        print("cloudflared não está instalado.")
        print("Baixe em https://github.com/cloudflare/cloudflared/releases (cloudflared-windows-amd64.exe)")
        print("e coloque na pasta VALLIA ou em uma pasta do PATH.")
        return 1
    print(f"túnel: {cloudflared} -> http://127.0.0.1:{PORT}", flush=True)
    # O cloudflared bloqueia o stdout quando não é terminal, então a URL é lida do log.
    LOG_FILE.write_text("", encoding="utf-8")
    process = subprocess.Popen(
        [cloudflared, "tunnel", "--no-autoupdate", "--logfile", str(LOG_FILE),
         "--url", f"http://127.0.0.1:{PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    public = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and process.poll() is None:
        public = URL_RE.search(LOG_FILE.read_text(encoding="utf-8", errors="replace"))
        if public:
            public = public.group(0)
            break
        time.sleep(0.5)
    if not public:
        print("O túnel não publicou uma URL em 60 s. Consulte cloudflared.log.")
        process.terminate()
        return 1
    print(
        "\n=== cole isto nas variáveis de ambiente do serviço no Render ===\n"
        f"VALLIA_ENGINE = {public}\n"
        "VALLIA_TOKEN  = (o conteúdo de token.txt, propositalmente não impresso aqui)\n"
        "===================================================================\n"
        f"testar agora: curl -H \"x-vallia-token: <token>\" {public}/api/status\n\n"
        "O túnel encerra quando este processo for morto e a URL muda a cada execução.",
        flush=True,
    )
    try:
        while process.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        process.terminate()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
