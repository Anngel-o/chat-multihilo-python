"""
Prueba de carga y rendimiento del servidor (requisito 6).

Levanta N clientes simulados (sin interfaz grafica), cada uno en su hilo,
hace que todos manden mensajes de difusion y mide:

    - cuanto tarda en conectarse todo el mundo
    - la latencia de ida y vuelta de cada mensaje propio
    - cuantos mensajes por segundo procesa el servidor en total

Uso:
    python prueba_carga.py                      (20 clientes, 20 mensajes c/u)
    python prueba_carga.py 50 10
    python prueba_carga.py 50 10 127.0.0.1 5000
"""

import socket
import statistics
import sys
import threading
import time

import protocolo as p


class ClientePrueba(threading.Thread):

    def __init__(self, indice, host, puerto, mensajes):
        super().__init__(daemon=True)
        self.usuario = f"bot{indice:03d}"
        self.host = host
        self.puerto = puerto
        self.mensajes = mensajes
        self.latencias = []
        self.recibidos = 0
        self.error = None
        self.listo = threading.Event()   # avisa que ya inicio sesion
        self.arrancar = threading.Event()  # espera la senial de inicio
        self.sock = None

    def run(self):
        try:
            self.sock = socket.create_connection((self.host, self.puerto), timeout=10)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            p.enviar(self.sock, {"tipo": p.LOGIN, "usuario": self.usuario})

            respuesta, _ = p.recibir(self.sock)
            if respuesta["tipo"] != p.LOGIN_OK:
                raise RuntimeError(respuesta.get("texto", "login rechazado"))
            self.listo.set()
            self.arrancar.wait()

            for i in range(self.mensajes):
                marca = f"ping {i}"
                inicio = time.perf_counter()
                # Mensaje privado a uno mismo: el servidor lo rutea y lo
                # devuelve, asi se mide el viaje completo de ida y vuelta.
                p.enviar(self.sock, {"tipo": p.MENSAJE, "destino": self.usuario,
                                     "texto": marca})
                while True:
                    mensaje, _ = p.recibir(self.sock)
                    self.recibidos += 1
                    # Se descartan los mensajes de los demas bots (altas,
                    # bajas, listas de usuarios) hasta encontrar el propio.
                    if mensaje["tipo"] == p.CHAT and mensaje.get("texto") == marca:
                        break
                self.latencias.append((time.perf_counter() - inicio) * 1000)
                time.sleep(0.01)

        except Exception as e:                      # noqa: BLE001 (prueba)
            self.error = e
            self.listo.set()

    def cerrar(self):
        try:
            if self.sock:
                p.enviar(self.sock, {"tipo": p.SALIR})
                self.sock.close()
        except OSError:
            pass


def main():
    n_clientes = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    n_mensajes = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    host = sys.argv[3] if len(sys.argv) > 3 else p.HOST_POR_DEFECTO
    puerto = int(sys.argv[4]) if len(sys.argv) > 4 else p.PUERTO_POR_DEFECTO

    print(f"Conectando {n_clientes} clientes a {host}:{puerto}...")
    inicio_conexion = time.perf_counter()

    bots = [ClientePrueba(i, host, puerto, n_mensajes) for i in range(n_clientes)]
    for bot in bots:
        bot.start()
    for bot in bots:
        bot.listo.wait(timeout=20)

    conectados = [b for b in bots if b.error is None]
    fallidos = [b for b in bots if b.error is not None]
    tiempo_conexion = time.perf_counter() - inicio_conexion
    print(f"Conectados: {len(conectados)}/{n_clientes} en {tiempo_conexion:.2f} s")
    if fallidos:
        print(f"  fallaron {len(fallidos)}, primer error: {fallidos[0].error}")
    if not conectados:
        return

    print(f"Enviando {n_mensajes} mensajes por cliente...")
    inicio_envio = time.perf_counter()
    for bot in bots:
        bot.arrancar.set()
    for bot in bots:
        bot.join(timeout=120)
    duracion = time.perf_counter() - inicio_envio

    latencias = [x for bot in conectados for x in bot.latencias]
    total = len(latencias)

    print("\n--- Resultados ---")
    print(f"Mensajes enviados       : {total}")
    print(f"Duracion                : {duracion:.2f} s")
    print(f"Rendimiento             : {total / duracion:.0f} mensajes/s")
    if latencias:
        latencias.sort()
        print(f"Latencia promedio       : {statistics.mean(latencias):.2f} ms")
        print(f"Latencia mediana        : {statistics.median(latencias):.2f} ms")
        print(f"Latencia percentil 95   : {latencias[int(len(latencias) * 0.95) - 1]:.2f} ms")
        print(f"Latencia maxima         : {latencias[-1]:.2f} ms")
    print("\nNota: el envio incluye una pausa de 10 ms por mensaje para simular")
    print("personas escribiendo; el rendimiento real del servidor es mayor.")

    for bot in bots:
        bot.cerrar()


if __name__ == "__main__":
    main()
