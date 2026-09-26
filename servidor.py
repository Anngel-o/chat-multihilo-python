"""
Servidor de chat multiusuario con hilos.

ARQUITECTURA
------------
    hilo principal  -> accept() en ciclo: solo acepta conexiones nuevas
    hilo por cliente -> recibe los mensajes de ESE cliente y los reparte

Asi el servidor nunca se bloquea: mientras un hilo espera un mensaje de
Juan, el hilo principal sigue aceptando a Maria y el hilo de Pedro sigue
difundiendo lo suyo.

SINCRONIZACION
--------------
Hay dos niveles de bloqueo, cada uno protege algo distinto:

1. self.lock          -> protege el diccionario self.clientes.
                         Varios hilos lo leen y escriben a la vez (entradas,
                         salidas, difusiones). Sin el, un hilo podria recorrer
                         el diccionario mientras otro lo modifica.

2. cliente.lock_envio -> protege la ESCRITURA en un socket concreto.
                         Si dos hilos le mandan mensajes al mismo cliente al
                         mismo tiempo, sus bytes se mezclarian.

Uso:
    python servidor.py                 (escucha en 0.0.0.0:5000)
    python servidor.py 0.0.0.0 6000
"""

import socket
import sys
import threading
from datetime import datetime

import protocolo as p


def log(texto):
    print(f"[{datetime.now():%H:%M:%S}] {texto}", flush=True)


class Cliente:
    """Estado de un cliente conectado."""

    def __init__(self, sock, direccion):
        self.sock = sock
        self.direccion = direccion
        self.usuario = None
        self.lock_envio = threading.Lock()   # ver nota 2 de arriba

    def enviar(self, obj, binario=b""):
        """Manda un mensaje a este cliente. Devuelve False si fallo."""
        try:
            p.enviar(self.sock, obj, binario, lock=self.lock_envio)
            return True
        except OSError:
            # El socket ya murio. No es un error fatal: el hilo de ese
            # cliente se encargara de darlo de baja.
            return False

    def cerrar(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class ServidorChat:

    def __init__(self, host=p.HOST_POR_DEFECTO, puerto=p.PUERTO_POR_DEFECTO):
        self.host = host
        self.puerto = puerto
        self.sock = None
        self.clientes = {}                   # usuario -> Cliente
        self.lock = threading.RLock()        # protege self.clientes
        self.activo = False

    # ---------------------------------------------------------------
    # Ciclo principal
    # ---------------------------------------------------------------

    def iniciar(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR evita el error "Address already in use" al reiniciar
        # el servidor mientras el puerto sigue en estado TIME_WAIT.
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.puerto))
        self.sock.listen(128)                # cola de conexiones pendientes
        self.activo = True

        log(f"Servidor escuchando en {self.host}:{self.puerto}")
        log("Ctrl+C para detener")

        try:
            while self.activo:
                try:
                    sock_cliente, direccion = self.sock.accept()
                except OSError:
                    break  # el socket se cerro (apagado del servidor)

                # TCP_NODELAY desactiva el algoritmo de Nagle: los mensajes
                # cortos de chat salen de inmediato en vez de esperar a
                # juntarse con otros.
                sock_cliente.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                hilo = threading.Thread(
                    target=self.atender_cliente,
                    args=(Cliente(sock_cliente, direccion),),
                    daemon=True,             # no impiden que el programa cierre
                )
                hilo.start()
        except KeyboardInterrupt:
            pass
        finally:
            self.detener()

    def detener(self):
        if not self.activo:
            return
        self.activo = False
        log("Cerrando servidor...")
        with self.lock:
            for cliente in list(self.clientes.values()):
                cliente.enviar({"tipo": p.SISTEMA, "texto": "El servidor se detuvo."})
                cliente.cerrar()
            self.clientes.clear()
        try:
            self.sock.close()
        except (OSError, AttributeError):
            pass

    # ---------------------------------------------------------------
    # Atencion de un cliente (esto corre en su propio hilo)
    # ---------------------------------------------------------------

    def atender_cliente(self, cliente):
        log(f"Conexion nueva desde {cliente.direccion[0]}:{cliente.direccion[1]}")
        try:
            if not self.hacer_login(cliente):
                return
            # Ciclo de mensajes: se bloquea aqui esperando cada mensaje.
            while self.activo:
                mensaje, binario = p.recibir(cliente.sock)
                if mensaje["tipo"] == p.SALIR:
                    break
                self.procesar(cliente, mensaje, binario)

        except p.ConexionCerrada:
            pass                                   # se desconecto sin avisar
        except p.ErrorProtocolo as e:
            log(f"Protocolo invalido de {cliente.usuario or cliente.direccion}: {e}")
        except OSError as e:
            log(f"Error de red con {cliente.usuario or cliente.direccion}: {e}")
        finally:
            self.dar_de_baja(cliente)

    def hacer_login(self, cliente):
        """El primer mensaje debe ser un login con un nombre libre."""
        # Timeout solo para el login: si alguien abre la conexion y no manda
        # nada, no se queda un hilo colgado para siempre.
        cliente.sock.settimeout(30)
        try:
            mensaje, _ = p.recibir(cliente.sock)
        except socket.timeout:
            cliente.enviar({"tipo": p.ERROR, "texto": "Tiempo agotado para iniciar sesion."})
            return False
        finally:
            cliente.sock.settimeout(None)          # a partir de aqui, sin limite

        usuario = str(mensaje.get("usuario", "")).strip()

        if mensaje["tipo"] != p.LOGIN or not usuario:
            cliente.enviar({"tipo": p.ERROR, "texto": "Debes iniciar sesion con un nombre."})
            return False
        if len(usuario) > 20 or not usuario.replace("_", "").replace(" ", "").isalnum():
            cliente.enviar({"tipo": p.ERROR,
                            "texto": "Nombre invalido (max 20 caracteres alfanumericos)."})
            return False

        # Seccion critica: revisar y reservar el nombre debe ser una sola
        # operacion indivisible. Si no, dos clientes podrian pasar la
        # revision al mismo tiempo con el mismo nombre.
        with self.lock:
            if usuario in self.clientes:
                cliente.enviar({"tipo": p.ERROR, "texto": f"El nombre '{usuario}' ya esta en uso."})
                return False
            cliente.usuario = usuario
            self.clientes[usuario] = cliente

        cliente.enviar({"tipo": p.LOGIN_OK, "usuario": usuario})
        log(f"{usuario} entro al chat")
        self.difundir({"tipo": p.SISTEMA, "texto": f"{usuario} se unio al chat."},
                      excepto=usuario)
        self.mandar_lista_usuarios()
        return True

    def dar_de_baja(self, cliente):
        with self.lock:
            # Se compara la instancia para no borrar a un cliente nuevo que
            # haya tomado el mismo nombre despues.
            if cliente.usuario and self.clientes.get(cliente.usuario) is cliente:
                del self.clientes[cliente.usuario]
                habia_entrado = True
            else:
                habia_entrado = False
        cliente.cerrar()

        if habia_entrado:
            log(f"{cliente.usuario} salio del chat")
            self.difundir({"tipo": p.SISTEMA, "texto": f"{cliente.usuario} salio del chat."})
            self.mandar_lista_usuarios()

    # ---------------------------------------------------------------
    # Ruteo de mensajes
    # ---------------------------------------------------------------

    def procesar(self, cliente, mensaje, binario):
        tipo = mensaje["tipo"]

        if tipo == p.MENSAJE:
            texto = str(mensaje.get("texto", "")).strip()
            destino = mensaje.get("destino", p.DIFUSION)
            if not texto:
                return
            salida = {"tipo": p.CHAT, "de": cliente.usuario, "texto": texto,
                      "privado": destino != p.DIFUSION}
            if destino == p.DIFUSION:
                self.difundir(salida)
            else:
                self.enviar_a(cliente, destino, salida)

        elif tipo == p.ARCHIVO:
            nombre = str(mensaje.get("nombre", "archivo"))
            destino = mensaje.get("destino", p.DIFUSION)
            if not binario:
                return
            # basename() evita que alguien mande "../../algo.txt" y escriba
            # fuera de la carpeta de descargas del receptor.
            nombre = nombre.replace("\\", "/").split("/")[-1] or "archivo"
            salida = {"tipo": p.ARCHIVO_ENTRANTE, "de": cliente.usuario,
                      "nombre": nombre, "tamanio": len(binario),
                      "privado": destino != p.DIFUSION}
            log(f"{cliente.usuario} envia archivo '{nombre}' ({len(binario)} bytes) a {destino}")
            if destino == p.DIFUSION:
                self.difundir(salida, binario, excepto=cliente.usuario)
                cliente.enviar({"tipo": p.SISTEMA,
                                "texto": f"Archivo '{nombre}' enviado a todos."})
            else:
                self.enviar_a(cliente, destino, salida, binario)

        else:
            cliente.enviar({"tipo": p.ERROR, "texto": f"Tipo de mensaje desconocido: {tipo}"})

    def difundir(self, mensaje, binario=b"", excepto=None):
        """Manda el mensaje a todos los conectados."""
        # Se copia la lista DENTRO del lock y se envia FUERA de el.
        # Si se enviara con el lock tomado, un cliente con red lenta
        # bloquearia a todo el servidor mientras se completa su sendall().
        with self.lock:
            destinatarios = [c for nombre, c in self.clientes.items() if nombre != excepto]
        for destinatario in destinatarios:
            destinatario.enviar(mensaje, binario)

    def enviar_a(self, remitente, nombre_destino, mensaje, binario=b""):
        """Manda el mensaje a un solo usuario (mensaje privado)."""
        with self.lock:
            destino = self.clientes.get(nombre_destino)
        if destino is None:
            remitente.enviar({"tipo": p.ERROR,
                              "texto": f"'{nombre_destino}' no esta conectado."})
            return
        destino.enviar(mensaje, binario)

        # Eco al remitente para que vea en su ventana lo que acaba de mandar.
        if mensaje["tipo"] == p.CHAT:
            eco = dict(mensaje)
            eco["para"] = nombre_destino
            remitente.enviar(eco)
        else:
            # En el caso de un archivo solo se confirma: no tiene caso
            # devolverle los bytes al que ya los tiene.
            remitente.enviar({"tipo": p.SISTEMA,
                              "texto": f"Archivo '{mensaje['nombre']}' enviado a {nombre_destino}."})

    def mandar_lista_usuarios(self):
        with self.lock:
            lista = sorted(self.clientes.keys())
        self.difundir({"tipo": p.USUARIOS, "lista": lista})


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
    puerto = int(sys.argv[2]) if len(sys.argv) > 2 else p.PUERTO_POR_DEFECTO
    ServidorChat(host, puerto).iniciar()
