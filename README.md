# Proyecto I — Aplicación de chat en tiempo real (Python)

Chat multiusuario sobre sockets TCP con interfaz gráfica, mensajes de difusión,
mensajes privados y transferencia de archivos. Solo librería estándar de Python:
**no hay que instalar nada**.

---

## 1. Cómo ejecutarlo

Requiere Python 3.8 o superior.

**Terminal 1 — servidor:**
```bash
python servidor.py                  # escucha en 0.0.0.0:5000
python servidor.py 0.0.0.0 6000     # host y puerto personalizados
```

**Terminal 2, 3, 4... — un cliente por cada persona:**
```bash
python cliente.py
```
En la ventana: escribe el servidor (`127.0.0.1` si es la misma computadora, o la
IP local del servidor si están en red), el puerto, un nombre de usuario, y pulsa
**Conectar**.

**Prueba de rendimiento (opcional):**
```bash
python prueba_carga.py 50 20        # 50 clientes simulados, 20 mensajes c/u
```

> En Linux, si `import tkinter` falla: `sudo apt install python3-tk`.
> En Windows y macOS Tkinter ya viene incluido.

---

## 2. Decisiones de diseño (lo que hay que justificar en la exposición)

### 2.1 ¿Hilos o procesos? → **Hilos** (`threading`)

| Criterio | Hilos | Procesos |
|---|---|---|
| Tipo de trabajo | E/S: esperar en `recv()` | CPU: cálculos pesados |
| El GIL estorba | No: se libera durante la E/S | No aplica |
| Memoria compartida | Sí: el diccionario de clientes es directo | No: hay que montar colas IPC |
| Costo por conexión | ~8 KB de stack | ~10 MB por proceso |
| Difundir un mensaje | Recorrer un `dict` con un `Lock` | Serializar y mandar por tuberías |

Un chat pasa el 99% del tiempo **esperando** datos de la red, no calculando. Ahí
el GIL no es un cuello de botella porque Python lo libera mientras el hilo está
bloqueado en el socket. Lo decisivo es que todos los hilos comparten la misma
memoria: el servidor necesita una sola tabla de "quién está conectado" para poder
rutear mensajes, y con hilos eso es un diccionario normal protegido con un
`Lock`. Con `multiprocessing` cada proceso tendría su copia y haría falta memoria
compartida o colas IPC solo para difundir un mensaje de texto: mucha complejidad
a cambio de nada.

Los procesos convendrían si el servidor tuviera que hacer trabajo pesado de CPU
por mensaje (cifrado fuerte, compresión de video, etc.). No es el caso.

### 2.2 ¿Interfaz en Python o en HTML? → **Tkinter**

- Viene en la librería estándar: cero dependencias, cero instalación.
- El requisito 4 pide **sockets TCP**. Una interfaz en HTML obligaría a meter un
  servidor HTTP y WebSockets en medio, y entonces ya no estaríamos programando
  los sockets: estaríamos usando una librería que los programa por nosotros.
- `filedialog` resuelve el requisito de transferencia de archivos en tres líneas;
  en el navegador habría que lidiar con la File API y codificar en base64.
- Un solo lenguaje, un solo proceso por cliente: más fácil de explicar y depurar.

---

## 3. Arquitectura

```
                    ┌──────────────────── SERVIDOR ────────────────────┐
                    │  hilo principal: accept() en ciclo               │
  Cliente A ────────┼─► hilo A: recibir() ─┐                           │
  Cliente B ────────┼─► hilo B: recibir() ─┼─► rutear ─► difundir /    │
  Cliente C ────────┼─► hilo C: recibir() ─┘             privado       │
                    │                                                   │
                    │  clientes {usuario: Cliente}  ← protegido por lock│
                    └───────────────────────────────────────────────────┘
```

Cada cliente GUI usa tres hilos:

| Hilo | Función |
|---|---|
| principal | `mainloop()` de Tkinter: dibuja la ventana y atiende clics |
| receptor | `recibir()` en ciclo; deposita lo que llega en una `queue.Queue` |
| subida | uno temporal por cada archivo que se envía (para no congelar la ventana) |

### Archivos del proyecto

| Archivo | Qué contiene |
|---|---|
| `protocolo.py` | Formato de los mensajes y funciones `enviar` / `recibir`. Lo usan los dos extremos |
| `servidor.py` | Aceptación de conexiones, hilo por cliente, ruteo, sincronización |
| `cliente.py` | Interfaz gráfica Tkinter, hilo receptor, envío de texto y archivos |
| `prueba_carga.py` | Clientes simulados para medir rendimiento (requisito 6) |

---

## 4. El protocolo

TCP es un **flujo de bytes**, no de mensajes: tres `sendall()` pueden llegar
pegados en un solo `recv()`, o cortados a la mitad. Por eso cada mensaje lleva un
encabezado con las longitudes ("framing"):

```
[ 4 bytes ][ 4 bytes ][   N bytes   ][   M bytes   ]
    N          M        JSON UTF-8      binario
```

El JSON lleva los metadatos (tipo, usuario, texto, nombre del archivo) y el bloque
binario lleva el archivo crudo. Se manda aparte y no dentro del JSON porque
base64 inflaría el archivo un 33%.

| Tipo | Dirección | Contenido |
|---|---|---|
| `login` | cliente → servidor | `{usuario}` |
| `mensaje` | cliente → servidor | `{texto, destino}` — `destino: "*"` = a todos |
| `archivo` | cliente → servidor | `{nombre, destino}` + bloque binario |
| `salir` | cliente → servidor | — |
| `login_ok` / `error` | servidor → cliente | confirmación o motivo del rechazo |
| `chat` | servidor → cliente | `{de, texto, privado}` |
| `archivo_entrante` | servidor → cliente | `{de, nombre, tamanio}` + binario |
| `usuarios` | servidor → cliente | `{lista}` de conectados |
| `sistema` | servidor → cliente | avisos de entrada/salida |

---

## 5. Sincronización: dónde están los locks y por qué

Hay tres condiciones de carrera reales en este programa y cada una tiene su
mecanismo:

**1. El diccionario de clientes** (`servidor.py`, `self.lock`)
Varios hilos lo leen y lo modifican a la vez. Sin el lock, un hilo podría estar
recorriéndolo para difundir mientras otro le borra una entrada → `RuntimeError`.
Además, comprobar si un nombre está libre y reservarlo debe ser **una sola**
operación indivisible; si no, dos personas podrían registrarse con el mismo
nombre al mismo tiempo.

**2. La escritura en un socket** (`cliente.lock_envio`)
Si dos hilos hacen `sendall()` sobre el mismo socket a la vez, sus bytes se
intercalan y el mensaje llega corrupto. Cada socket tiene su propio lock de
envío, así que los mensajes salen completos y en orden.

**3. Tkinter y el hilo receptor** (`cliente.py`, `queue.Queue`)
Tkinter **no** es seguro entre hilos: tocar un widget desde el hilo receptor
puede tronar la aplicación de forma aleatoria. Por eso el hilo receptor nunca
toca la ventana: solo encola, y la interfaz revisa la cola cada 50 ms con
`after()`. Una `Queue` ya trae su sincronización interna.

**Detalle importante de rendimiento:** en `difundir()` la lista de destinatarios
se copia *dentro* del lock pero los envíos se hacen *fuera* de él. Si se enviara
con el lock tomado, un cliente con la red lenta bloquearía a todo el servidor
durante su `sendall()`.

---

## 6. Manejo de errores

- Desconexión abrupta (se cierra la ventana, se cae el WiFi): `recv()` devuelve
  vacío → `ConexionCerrada` → el cliente se da de baja y se avisa a los demás.
- Mensaje mal formado o encabezado absurdo → `ErrorProtocolo`, se cierra esa
  conexión sin afectar a nadie más.
- Login sin responder → timeout de 30 s para que no quede un hilo colgado.
- Nombre repetido, nombre inválido o destinatario inexistente → mensaje de error
  al cliente, la sesión continúa.
- Archivos: límite de 10 MB y el nombre se limpia con `basename()` para que nadie
  pueda mandar `../../algo.exe` y escribir fuera de la carpeta de descargas.
- El servidor se apaga con Ctrl+C avisando a todos los conectados.

---

## 7. Resultados de la prueba de rendimiento

Medido con `prueba_carga.py` sobre localhost (mensaje de ida y vuelta completo:
cliente → servidor → cliente):

| Clientes | Mensajes | Conexión de todos | Rendimiento | Latencia mediana | Percentil 95 |
|---|---|---|---|---|---|
| 15 | 150 | 0.01 s | 1,365 msg/s | 0.17 ms | 3.77 ms |
| 60 | 900 | 0.08 s | 3,263 msg/s | 0.51 ms | 30.2 ms |

60 clientes simultáneos = 61 hilos en el servidor, sin errores ni conexiones
perdidas. Para un chat de salón de clases sobra. El modelo de "un hilo por
cliente" empieza a pesar alrededor de los 500–1,000 hilos; de ahí en adelante
habría que cambiar a `selectors` o `asyncio`, pero eso no es lo que pide el
proyecto.

---

## 8. Cumplimiento de los requisitos

| # | Requisito | Dónde está |
|---|---|---|
| 1a | Múltiples clientes simultáneos | `servidor.py` → `iniciar()`, un hilo por `accept()` |
| 1b | Envío y recepción entre clientes | `servidor.py` → `procesar()` |
| 1c | Difusión y destinatario correcto | `difundir()` y `enviar_a()` |
| 2a | Conectarse y chatear en tiempo real | `cliente.py` → `conectar()`, `ciclo_receptor()` |
| 2b | Interfaz para escribir y ver | Ventana Tkinter con área de chat y lista de conectados |
| 2c | Transferencia de archivos | Botón "Archivo...", `enviar_archivo()` / `guardar_archivo()` |
| 3a | Hilos por conexión | `threading.Thread` por cliente en ambos extremos |
| 3b | Servidor no bloqueante | `accept()` en el hilo principal, E/S en los hilos hijos |
| 4a | Sockets | Módulo `socket` directo, sin frameworks |
| 4b | TCP | `SOCK_STREAM` + framing propio |
| 5a | Sin condiciones de carrera | Sección 5 de este documento |
| 5b | Locks | `RLock` del diccionario, `Lock` por socket, `Queue` en la GUI |
| 6a | Uso eficiente de recursos | Hilos daemon, envío fuera del lock, `TCP_NODELAY` |
| 6b | Evaluación de rendimiento | `prueba_carga.py` + sección 7 |
| Extra | Manejo de errores | Sección 6 |
| Extra | Extensibilidad | Protocolo por tipos: agregar salas o autenticación es agregar un tipo nuevo |
| Extra | Interfaz gráfica | Tkinter |

---

## 9. Cómo crecería (por si preguntan)

- **Salas de chat:** agregar `sala` al diccionario del cliente y un tipo
  `unirse_sala`; `difundir()` filtraría por sala en vez de mandar a todos.
- **Autenticación:** el mensaje `login` ya existe; solo habría que agregarle un
  campo de contraseña y comparar contra un archivo o una base de datos.
- **Historial:** guardar cada mensaje ruteado en un archivo o SQLite y mandarlo
  al conectarse.
- **Cifrado:** envolver el socket con `ssl.wrap_socket()`; el resto del código no
  cambia porque todo pasa por `protocolo.py`.
