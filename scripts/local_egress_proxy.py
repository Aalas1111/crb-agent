"""把本机出口借给服务器用的最小 HTTP CONNECT 代理（**只绑 127.0.0.1**）。

配合 SSH 反向隧道使用：

    ssh -N -R 18888:127.0.0.1:<本脚本端口> lihe@<服务器>

于是服务器上 `http://127.0.0.1:18888` 的出口就是**本机**的出口
（校园网 / 家宽），而学校拦的是机房 IP —— 这是绕开那条限制最省事的办法。

只做 CONNECT（HTTPS），不做普通 GET 转发：我们要代理的目标全是 HTTPS。
只绑 127.0.0.1：本机之外只能通过 SSH 隧道到达它。
"""

from __future__ import annotations

import socket
import sys
import threading

BUFFER = 65536


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(BUFFER)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _handle(client: socket.socket) -> None:
    try:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = client.recv(4096)
            if not chunk:
                return
            request += chunk
        head = request.split(b"\r\n", 1)[0].decode("latin-1")
        parts = head.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host, _, port = parts[1].rpartition(":")
        upstream = socket.create_connection((host, int(port or 443)), 20)
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        _pipe(upstream, client)
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8888
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(50)
    print(f"CONNECT 代理已就绪：127.0.0.1:{port}（只绑本机回环）", flush=True)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
