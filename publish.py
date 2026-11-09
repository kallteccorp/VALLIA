"""Publica a VALLIA: envia o código ao GitHub e cria/atualiza o serviço no Render.

Uso:
    python -B publish.py            # push + deploy + acompanhamento
    python -B publish.py --check    # só mostra o estado, sem escrever nada

Os segredos vêm de arquivos ignorados pelo git (gh_token.txt, render_token.txt,
token.txt) e nunca são impressos: qualquer saída passa por scrub().
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GH_FILE = ROOT / "gh_token.txt"
RENDER_FILE = ROOT / "render_token.txt"
VALLIA_FILE = ROOT / "token.txt"
TUNNEL_LOG = ROOT / "cloudflared.log"
REPO = "https://github.com/kallteccorp/VALLIA"
BRANCH = "main"
SERVICE = "vallia"
API = "https://api.render.com/v1"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
SECRETS: list[str] = []


def scrub(text):
    for value in SECRETS:
        text = text.replace(value, "<segredo>")
    return text


def read(path, label):
    if not path.is_file():
        sys.exit(f"Falta {label} em {path.name}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        sys.exit(f"{path.name} está vazio.")
    SECRETS.append(value)
    return value


def tunnel_url():
    if TUNNEL_LOG.is_file():
        found = URL_RE.search(TUNNEL_LOG.read_text(encoding="utf-8", errors="replace"))
        if found:
            return found.group(0)
    sys.exit("Nenhuma URL de túnel em cloudflared.log — rode `python -B tunnel.py` antes.")


def api(token, method, path, body=None, query=""):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        API + path + query, data,
        {"Authorization": f"Bearer {token}", "Accept": "application/json",
         "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"message": scrub(raw[:400])}
    except OSError as error:
        return 0, {"message": scrub(str(error))}


def push():
    """Helper inline: o Gerenciador do Windows guarda outra conta e ignoraria o arquivo."""
    token = read(GH_FILE, "Personal Access Token do GitHub")
    helper = 'credential.helper=!f() { echo username=x-access-token; echo "password=$VALLIA_GH_TOKEN"; }; f'
    env = {**os.environ, "VALLIA_GH_TOKEN": token, "GCM_INTERACTIVE": "never",
           "GIT_TERMINAL_PROMPT": "0"}
    done = subprocess.run(
        ["git", "-c", "credential.helper=", "-c", helper, "push", "-u", "origin", BRANCH],
        env=env, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    print(scrub((done.stdout or "").strip()), flush=True)
    if done.returncode:
        sys.exit("O push falhou; nada foi publicado no Render.")


def find_service(token):
    status, data = api(token, "GET", "/services", query="?limit=100")
    if status != 200:
        sys.exit(f"Não consegui listar seus serviços no Render: {scrub(json.dumps(data))}")
    for item in data or []:
        if item.get("service", {}).get("name") == SERVICE:
            return item["service"]["id"]
    return None


def create_service(token, owner, engine, vallia_token):
    payload = {"service": {
        "name": SERVICE,
        "type": "web",
        "runtime": "python",
        "plan": "free",
        "region": "oregon",
        "repo": {"url": REPO},
        "branch": BRANCH,
        "startCommand": "python -B server.py",
        "healthCheckPath": "/health",
        "autoDeploy": "on",
        "envVars": [
            {"key": "VALLIA_ENGINE", "value": engine},
            {"key": "VALLIA_TOKEN", "value": vallia_token},
        ],
    }}
    status, data = api(token, "POST", "/services", payload, query=f"?ownerId={owner}")
    if status not in (200, 201):
        sys.exit(f"Criação do serviço falhou ({status}): {scrub(json.dumps(data))}")
    return (data.get("service") or data)["id"]


def set_env(token, service_id, engine, vallia_token):
    body = {"envVars": {"VALLIA_ENGINE": engine, "VALLIA_TOKEN": vallia_token}}
    status, data = api(token, "PUT", f"/services/{service_id}/env-vars", body)
    if status != 200:
        print(f"  aviso: PUT env-vars respondeu {status}: {scrub(json.dumps(data))}")
    return status


def trigger(token, service_id):
    status, data = api(token, "POST", f"/services/{service_id}/trigger-deploy")
    if status not in (200, 201, 202, 204):
        print(f"  aviso: trigger-deploy respondeu {status}: {scrub(json.dumps(data))}")


def watch(token, service_id, public):
    """Aguarda build e health check, com o máximo de ~6 minutos."""
    deadline = time.monotonic() + 360
    last = None
    while time.monotonic() < deadline:
        status, data = api(token, "GET", f"/services/{service_id}/deploys", query="?limit=1")
        job = (data or [{}])[0].get("deploy") or {}
        state = job.get("status")
        if state != last:
            print(f"  deploy: {state} ({job.get('commit', {}).get('message', '')[:40]})", flush=True)
            last = state
        if state in ("live", "deactivated", "canceled", "build_error"):
            break
        time.sleep(10)
    if public:
        probe = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                                "--max-time", "40", public + "/health"],
                               text=True, capture_output=True)
        print(f"  health do site: {probe.stdout or 'sem resposta'}")


def main():
    check = "--check" in sys.argv
    engine = tunnel_url()
    vallia_token = read(VALLIA_FILE, "segredo interno da VALLIA")
    render_token = read(RENDER_FILE, "API key do Render")
    print(f"túnel: {engine}")
    if check:
        status, data = api(render_token, "GET", "/services", query="?limit=100")
        print(f"  serviços no Render ({status}):")
        for item in data or []:
            service = item.get("service", {})
            print(f"   - {service.get('name')} {service.get('serviceType')} "
                  f"{service.get('suspended')} {service.get('url')}")
        return 0

    if "--no-push" not in sys.argv:
        push()

    status, owners = api(render_token, "GET", "/owners")
    if status != 200 or not owners:
        sys.exit(f"Não consegui ler o dono da conta Render ({status}).")
    owner = owners[0]["id"]

    service_id = find_service(render_token)
    if service_id:
        print(f"serviço já existe ({service_id}); atualizando as variáveis.")
        set_env(render_token, service_id, engine, vallia_token)
        trigger(render_token, service_id)
    else:
        service_id = create_service(render_token, owner, engine, vallia_token)
        print(f"serviço criado: {service_id}")

    status, data = api(render_token, "GET", f"/services/{service_id}")
    public = (data or {}).get("service", {}).get("url") or ""
    print(f"url pública: {public}")
    print(f"cole no Render depois: VALLIA_ENGINE = {engine}")
    print("               VALLIA_TOKEN = (o conteúdo de token.txt)")
    watch(render_token, service_id, public)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
