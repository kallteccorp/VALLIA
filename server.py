import http.client
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8093"))
LOCAL_ORIGIN = f"http://{HOST}:{PORT}"
# O motor é o llama-server; em produção é o túnel que aponta para o server.py do PC.
ENGINE = (os.environ.get("VALLIA_ENGINE") or "http://127.0.0.1:8091").rstrip("/")
TOKEN = os.environ.get("VALLIA_TOKEN", "").strip()
LOCAL_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}", HOST, "localhost"}
# Cada chamada através do túnel paga um ida-e-volta, então as 2 s locais apertam demais.
ENGINE_TIMEOUT = 2 if urlparse(ENGINE).hostname in {"127.0.0.1", "localhost"} else 12


def env_list(name):
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def origins():
    values = {LOCAL_ORIGIN}
    for raw in [os.environ.get("RENDER_EXTERNAL_URL", ""), *env_list("VALLIA_ALLOWED_ORIGINS")]:
        raw = raw.strip().rstrip("/")
        if raw.startswith("http"):
            values.add(raw)
    return values


ALLOWED_ORIGINS = origins()
ALLOWED_HOSTS = {urlparse(origin).netloc for origin in ALLOWED_ORIGINS} | LOCAL_HOSTS
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/vallia.svg": ("vallia.svg", "image/svg+xml"),
}
ROLES = ("user", "assistant")
SYSTEM = (
    "Você é VALLIA, uma assistente que roda no computador do usuário. "
    "Responda sempre no idioma da mensagem do usuário, apenas com texto. "
    "Seja direta e útil; se não souber, diga que não sabe."
)
_model_cache = {"name": None, "at": 0.0}


def engine_json(path, timeout=ENGINE_TIMEOUT):
    # Sem o segredo no cabeçalho, o proxy do outro lado do túnel devolve 403.
    request = Request(ENGINE + path, headers=engine_headers())
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def engine_headers():
    headers = {}
    if TOKEN:
        headers["x-vallia-token"] = TOKEN
    return headers


def engine_alive(timeout=ENGINE_TIMEOUT):
    try:
        return engine_json("/health", timeout).get("status") == "ok"
    except (OSError, ValueError):
        return False


def model_name():
    now = time.monotonic()
    if not _model_cache["name"] or now - _model_cache["at"] > 60:
        try:
            data = engine_json("/v1/models").get("data", [])
            _model_cache["name"] = data[0]["id"]
            _model_cache["at"] = now
        except (OSError, ValueError, KeyError, IndexError):
            return None
    return _model_cache["name"]


def status_payload():
    payload = {"ready": False, "model": None, "n_ctx": 2048, "ftype": None}
    if not engine_alive():
        return payload
    name = model_name()
    if not name:
        return payload
    payload.update(ready=True, model=name)
    try:
        props = engine_json("/props", 3)
        payload["n_ctx"] = int(props.get("n_ctx") or 2048)
        payload["ftype"] = props.get("model_ftype")
    except (OSError, ValueError, TypeError):
        pass
    return payload


def read_messages(body):
    messages = json.loads(body)["messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= 12:
        raise ValueError
    total = 0
    for turn in messages:
        if not isinstance(turn, dict) or turn.get("role") not in ROLES:
            raise ValueError
        if not isinstance(turn.get("content"), str) or not turn["content"].strip():
            raise ValueError
        total += len(turn["content"])
    if messages[-1]["role"] != "user" or not 1 <= total <= 6000:
        raise ValueError
    return messages, total


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 de propósito: sem keep-alive, o navegador nunca reutiliza
    # uma conexão que o servidor já encerrou (causa do ECONNRESET intermitente).
    server_version = "VALLIA"

    def log_message(self, *_args):
        pass

    def headers_for(self, status, mime, length=None, closing=False):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        if closing:
            self.send_header("Connection", "close")
            self.close_connection = True
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def send_json(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.headers_for(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def authorized(self):
        """O segredo identifica o pulo Render->túnel; o navegador passa por Host+Origin."""
        if self.engine_client():
            return True
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in ALLOWED_ORIGINS

    def engine_client(self):
        return bool(TOKEN) and hmac.compare_digest(self.headers.get("x-vallia-token", ""), TOKEN)

    def do_GET(self):
        if not self.authorized():
            return self.send_json(403, {"erro": "Endereço inválido."})
        if self.path == "/health":
            return self.send_json(200, {"status": "ok", "app": "vallia"})
        if self.path == "/api/status":
            return self.send_json(200, status_payload())
        if self.path == "/v1/models" and self.engine_client():
            # O Render usa as rotas /v1/* quando o motor é este servidor do outro lado do túnel.
            try:
                return self.send_json(200, engine_json("/v1/models"))
            except (OSError, ValueError):
                return self.send_json(503, {"erro": "O modelo não respondeu a tempo."})
        if self.path not in ASSETS:
            return self.send_json(404, {"erro": "Página não encontrada."})
        name, mime = ASSETS[self.path]
        try:
            data = (ROOT / name).read_bytes()
        except OSError:
            return self.send_json(500, {"erro": "Arquivo ausente no disco."})
        self.headers_for(200, mime, len(data))
        self.wfile.write(data)

    def pipe_engine(self):
        """Repassa o corpo cru ao llama-server; quem chama já validou os campos."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 64_000:
                raise ValueError
            body = self.rfile.read(length)
        except (TypeError, ValueError):
            return self.send_json(400, {"erro": "Corpo inválido."})
        except OSError:
            return
        headers = {"Content-Type": "application/json", **engine_headers()}
        try:
            upstream = urlopen(Request(ENGINE + "/v1/chat/completions", body, headers), timeout=240)
        except (HTTPError, OSError) as exc:
            print(f"pipe_engine falhou: {type(exc).__name__}: {exc}", flush=True)
            return self.send_json(503, {"erro": "O modelo não respondeu a tempo."})
        with upstream:
            self.headers_for(200, upstream.headers.get("Content-Type") or "text/event-stream", closing=True)
            try:
                for line in upstream:
                    self.wfile.write(line)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException):
                pass

    def do_POST(self):
        if not self.authorized():
            return self.send_json(403, {"erro": "Origem inválida."})
        if self.path == "/v1/chat/completions":
            if not self.engine_client():
                return self.send_json(404, {"erro": "Rota não encontrada."})
            return self.pipe_engine()
        if self.path != "/api/chat":
            return self.send_json(404, {"erro": "Rota não encontrada."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get_content_type() != "application/json" or not 0 < length <= 32_000:
                raise ValueError
            raw = self.rfile.read(length)
            payload = json.loads(raw)
            messages, used = read_messages(raw)
            max_tokens = int(payload.get("max_tokens") or 260)
            if not 48 <= max_tokens <= 512:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return self.send_json(400, {"erro": "Mensagem ou contexto acima do limite da janela."})
        except OSError:
            return

        if not engine_alive():
            return self.send_json(503, {"erro": "O modelo está desligado. Abra o atalho da VALLIA para iniciá-lo."})

        budget = max(48, min(512, 2048 - used // 3 - 320))
        request_body = {
            "model": model_name(),
            "messages": [{"role": "system", "content": SYSTEM}, *messages],
            "stream": True,
            "max_tokens": min(max_tokens, budget),
            "temperature": 0.6,
        }
        headers = {"Content-Type": "application/json", **engine_headers()}
        try:
            upstream = urlopen(
                Request(ENGINE + "/v1/chat/completions", json.dumps(request_body).encode(), headers),
                timeout=240,
            )
        except HTTPError as exc:
            exc.close()
            return self.send_json(502, {"erro": "O modelo recusou a solicitação. Tente uma mensagem mais curta."})
        except OSError:
            return self.send_json(503, {"erro": "O modelo não respondeu a tempo."})

        with upstream:
            self.headers_for(200, "text/event-stream; charset=utf-8", closing=True)
            try:
                for line in upstream:
                    self.wfile.write(line)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException):
                pass


if __name__ == "__main__":
    with ThreadingHTTPServer(("0.0.0.0" if os.environ.get("PORT") else HOST, PORT), Handler) as server:
        print(f"VALLIA em http://{HOST}:{PORT} -> motor {ENGINE}", flush=True)
        server.serve_forever()
