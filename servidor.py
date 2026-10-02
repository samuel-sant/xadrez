import json
import os
import queue
import random
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


PORT = 8080
HOST = "0.0.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

clients = {}
clients_lock = threading.Lock()

game_turn = "white"


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def send_to_client(client_id, message):
    with clients_lock:
        client = clients.get(client_id)

        if client:
            client["queue"].put(message)
            client["last_seen"] = time.time()


def broadcast(message, except_client=None):
    with clients_lock:
        for client_id, client in clients.items():
            if client_id != except_client:
                client["queue"].put(message)


def cleanup_clients():
    while True:
        time.sleep(5)

        now = time.time()
        disconnected = []

        with clients_lock:
            for client_id, client in list(clients.items()):
                if now - client["last_seen"] > 40:
                    disconnected.append(client_id)
                    del clients[client_id]

        if disconnected:
            print("Cliente desconectado:", disconnected)

            broadcast({
                "type": "player_left"
            })


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print("[HTTP]", format % args)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))

        if length == 0:
            return {}

        data = self.rfile.read(length)

        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):

        parsed = urlparse(self.path)
        path = parsed.path

        # Página principal
        if path == "/" or path == "/index.html":

            if not os.path.exists(INDEX_FILE):
                self.send_error(404, "index.html não encontrado")
                return

            try:
                with open(INDEX_FILE, "rb") as f:
                    content = f.read()

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()

                self.wfile.write(content)

            except Exception as e:
                self.send_error(500, str(e))

            return

        # Polling
        if path == "/poll":

            params = parse_qs(parsed.query)
            client_id = params.get("client_id", [None])[0]

            if not client_id:
                self.send_json({
                    "error": "client_id ausente"
                }, 400)
                return

            with clients_lock:

                client = clients.get(client_id)

                if not client:
                    self.send_json({
                        "error": "Cliente não encontrado"
                    }, 404)
                    return

                client["last_seen"] = time.time()
                client_queue = client["queue"]

            try:
                message = client_queue.get(timeout=25)

            except queue.Empty:

                message = {
                    "type": "keepalive"
                }

            with clients_lock:

                if client_id in clients:
                    clients[client_id]["last_seen"] = time.time()

            self.send_json(message)
            return

        # favicon
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        self.send_error(404, "Página não encontrada")

    def do_POST(self):

        global game_turn
        parsed = urlparse(self.path)
        path = parsed.path

        # Registrar jogador
        if path == "/register":

            with clients_lock:

                if len(clients) >= 2:
                    self.send_json({
                        "success": False,
                        "error": "A sala já possui dois jogadores."
                    }, 409)
                    return

                used_colors = {
                    client["color"]
                    for client in clients.values()
                }

                if "white" not in used_colors:
                    color = "white"
                else:
                    color = "black"

                client_id = uuid.uuid4().hex

                clients[client_id] = {
                    "color": color,
                    "queue": queue.Queue(),
                    "last_seen": time.time()
                }

                total_players = len(clients)

            print(
                "Novo jogador:",
                color,
                client_id,
                "| Jogadores:",
                total_players
            )

            self.send_json({
                "success": True,
                "client_id": client_id,
                "color": color,
                "players": total_players
            })

            # Quando entrar o segundo jogador,
            # avisar os dois que a partida começou.
            if total_players == 2:

              
                game_turn = "white"

                broadcast({
                    "type": "game_start",
                    "turn": "white"
                })

                print("Partida iniciada!")

            return

        # Movimento
        if path == "/move":

            data = self.read_json()

            client_id = data.get("client_id")
            move = data.get("move")

            if not client_id or not move:
                self.send_json({
                    "success": False,
                    "error": "Dados incompletos."
                }, 400)
                return

            with clients_lock:

                client = clients.get(client_id)

                if not client:
                    self.send_json({
                        "success": False,
                        "error": "Cliente não registrado."
                    }, 404)
                    return

                color = client["color"]

                # Verifica de quem é a vez
                if color != game_turn:

                    self.send_json({
                        "success": False,
                        "error": "Não é a vez deste jogador."
                    }, 400)
                    return

                # Alterna turno
                game_turn = "black" if game_turn == "white" else "white"

                client["last_seen"] = time.time()

            # Envia a jogada para o OUTRO jogador.
            # O jogador que fez a jogada já atualizou o próprio tabuleiro.
            broadcast({
                "type": "move",
                "move": move
            }, except_client=client_id)

            self.send_json({
                "success": True,
                "turn": game_turn
            })

            return

        # Novo jogo
        if path == "/newgame":

            data = self.read_json()
            client_id = data.get("client_id")

            with clients_lock:
                if client_id not in clients:
                    self.send_json({
                        "success": False,
                        "error": "Cliente não registrado."
                    }, 404)
                    return

                if len(clients) != 2:
                    self.send_json({
                        "success": False,
                        "error": "São necessários dois jogadores para iniciar uma partida."
                    }, 409)
                    return

                player_ids = list(clients)
                random.shuffle(player_ids)

                clients[player_ids[0]]["color"] = "white"
                clients[player_ids[1]]["color"] = "black"
                clients[client_id]["last_seen"] = time.time()
                game_turn = "white"

                colors = {
                    player_id: clients[player_id]["color"]
                    for player_id in player_ids
                }

            broadcast({
                "type": "new_game",
                "turn": "white",
                "colors": colors
            })

            print("Novo jogo solicitado.")

            self.send_json({
                "success": True
            })

            return

        # Heartbeat
        if path == "/heartbeat":

            data = self.read_json()
            client_id = data.get("client_id")

            if client_id:

                with clients_lock:

                    if client_id in clients:
                        clients[client_id]["last_seen"] = time.time()

            self.send_json({
                "success": True
            })

            return

        # Sair
        if path == "/leave":

            data = self.read_json()
            client_id = data.get("client_id")

            if client_id:

                with clients_lock:

                    if client_id in clients:
                        del clients[client_id]

                print("Jogador saiu:", client_id)

                broadcast({
                    "type": "player_left"
                })

            self.send_json({
                "success": True
            })

            return

        self.send_error(404, "Endpoint não encontrado")


def main():

    print()
    print("==============================")
    print("          XADREZ LAN")
    print("==============================")
    print()
    print("HTTP : porta", PORT)
    print()
    print("Abra no servidor:")
    print("http://localhost:8080")
    print()
    print("No outro computador:")
    print("http://IP_DO_SERVIDOR:8080")
    print()
    print("[HTTP] Servidor iniciado em http://0.0.0.0:8080")
    print()

    server = Server((HOST, PORT), Handler)

    cleanup_thread = threading.Thread(
        target=cleanup_clients,
        daemon=True
    )

    cleanup_thread.start()

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print()
        print("Servidor encerrado.")

    finally:
        server.server_close()


if __name__ == "__main__":
    main()