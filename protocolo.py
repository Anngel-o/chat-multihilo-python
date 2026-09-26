"""
Protocolo de comunicacion del chat.
Compartido por el servidor y el cliente.

PROBLEMA QUE RESUELVE ESTE MODULO
---------------------------------
TCP es un flujo continuo de bytes, NO de mensajes. Si un cliente hace tres
sendall() seguidos, el otro lado puede recibirlos pegados en un solo recv(),
o partidos a la mitad. Por eso cada mensaje se manda con un encabezado que
dice cuantos bytes vienen ("framing").

FORMATO DE CADA MENSAJE
-----------------------
    [ 4 bytes ][ 4 bytes ][  N bytes  ][  M bytes  ]
       N          M         JSON UTF-8    binario

    N = tamanio del bloque JSON (siempre presente)
    M = tamanio del bloque binario (0 si el mensaje no lleva archivo)

El JSON lleva los metadatos (tipo de mensaje, usuario, texto, nombre del
archivo...) y el bloque binario lleva el contenido crudo del archivo.
Se manda aparte y no dentro del JSON porque meterlo en base64 inflaria
el archivo un 33% y obligaria a codificar/decodificar todo en memoria.
"""

import json
import struct

# -------------------------------------------------------------------
# Configuracion general
# -------------------------------------------------------------------

PUERTO_POR_DEFECTO = 5000
HOST_POR_DEFECTO = "127.0.0.1"

# Limite de tamanio por archivo transferido (10 MB).
# Sirve para que un cliente no tumbe al servidor mandando un archivo enorme.
MAX_ARCHIVO = 10 * 1024 * 1024

# Limite del bloque JSON (64 KB). Un mensaje de texto normal pesa bytes.
MAX_JSON = 64 * 1024

# "!II" = dos enteros sin signo de 4 bytes en orden de red (big-endian).
# Se fija el orden de bytes para que funcione aunque cliente y servidor
# esten en arquitecturas distintas.
ENCABEZADO = struct.Struct("!II")

# -------------------------------------------------------------------
# Tipos de mensaje (el campo "tipo" del JSON)
# -------------------------------------------------------------------

# Cliente -> Servidor
LOGIN = "login"              # {usuario}
MENSAJE = "mensaje"          # {texto, destino}   destino "*" = difusion
ARCHIVO = "archivo"          # {nombre, destino} + bloque binario
SALIR = "salir"              # {}

# Servidor -> Cliente
LOGIN_OK = "login_ok"        # {usuario}
ERROR = "error"              # {texto}
CHAT = "chat"                # {de, texto, privado}
ARCHIVO_ENTRANTE = "archivo_entrante"   # {de, nombre, tamanio, privado} + binario
SISTEMA = "sistema"          # {texto}
USUARIOS = "usuarios"        # {lista}

DIFUSION = "*"               # destinatario especial: todos


class ErrorProtocolo(Exception):
    """El otro extremo mando algo que no respeta el formato."""


class ConexionCerrada(Exception):
    """El otro extremo cerro la conexion (recv devolvio vacio)."""


# -------------------------------------------------------------------
# Envio
# -------------------------------------------------------------------

def empaquetar(obj, binario=b""):
    """Convierte un diccionario (+ datos binarios opcionales) en bytes listos
    para mandar por el socket."""
    cuerpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return ENCABEZADO.pack(len(cuerpo), len(binario)) + cuerpo + binario


def enviar(sock, obj, binario=b"", lock=None):
    """Manda un mensaje completo por el socket.

    'lock' es importante: si dos hilos distintos escriben en el MISMO socket
    al mismo tiempo, sus bytes se intercalan y el mensaje llega corrupto
    (esto es una race condition clasica). Pasando el lock del socket,
    sendall() queda serializado.
    """
    datos = empaquetar(obj, binario)
    if lock is None:
        sock.sendall(datos)
    else:
        with lock:
            sock.sendall(datos)


# -------------------------------------------------------------------
# Recepcion
# -------------------------------------------------------------------

def _recibir_exacto(sock, cantidad):
    """Lee exactamente 'cantidad' bytes del socket.

    recv() puede devolver MENOS bytes de los pedidos, asi que hay que
    insistir en un ciclo hasta completar. Si devuelve vacio, el otro
    extremo cerro la conexion.
    """
    buffer = bytearray()
    while len(buffer) < cantidad:
        parte = sock.recv(min(65536, cantidad - len(buffer)))
        if not parte:
            raise ConexionCerrada()
        buffer.extend(parte)
    return bytes(buffer)


def recibir(sock):
    """Lee un mensaje completo. Devuelve (diccionario, bytes_binarios).

    Se bloquea hasta que llegue un mensaje entero. Por eso cada cliente
    necesita su propio hilo: si no, un cliente lento congelaria a todos.
    """
    n_json, n_bin = ENCABEZADO.unpack(_recibir_exacto(sock, ENCABEZADO.size))

    if n_json == 0 or n_json > MAX_JSON:
        raise ErrorProtocolo(f"Bloque JSON invalido ({n_json} bytes)")
    if n_bin > MAX_ARCHIVO:
        raise ErrorProtocolo(f"Bloque binario demasiado grande ({n_bin} bytes)")

    try:
        obj = json.loads(_recibir_exacto(sock, n_json).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ErrorProtocolo(f"JSON malformado: {e}")

    if not isinstance(obj, dict) or "tipo" not in obj:
        raise ErrorProtocolo("Mensaje sin campo 'tipo'")

    binario = _recibir_exacto(sock, n_bin) if n_bin else b""
    return obj, binario
