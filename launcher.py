import os
import socket
import sys
import threading
import webbrowser
from app import app, init_db


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def start_browser():
    webbrowser.open("http://localhost:5000/teacher/dashboard")


if __name__ == "__main__":
    init_db()  # Kusa gawa ng database kung wala pa
    ip_address = get_local_ip()

    print("=" * 50)
    print(" AI CHECKER SYSTEM IS RUNNING!")
    print(f" Teacher Dashboard: http://localhost:5000")
    print(f" Student Portal IP : http://{ip_address}:5000/student")
    print("=" * 50)

    threading.Timer(1.5, start_browser).start()
    app.run(host="0.0.0.0", port=5000, debug=False)