"""
Cliente de chat con interfaz grafica (Tkinter).

POR QUE HACEN FALTA HILOS AQUI TAMBIEN
--------------------------------------
Tkinter tiene su propio ciclo infinito (mainloop) atendiendo clics y teclas.
Si en ese mismo hilo llamaramos a recv(), la ventana se congelaria hasta que
llegara un mensaje. Por eso:

    hilo principal -> mainloop() de Tkinter (dibuja y responde a la persona)
    hilo receptor  -> ciclo de recibir() del socket
    hilo por envio de archivo -> para que la ventana no se trabe al subirlo

COMO SE COMUNICAN ESOS HILOS
----------------------------
Tkinter NO es seguro para multiples hilos: tocar los widgets desde el hilo
receptor puede tronar la aplicacion de forma aleatoria. La solucion estandar
es una cola (queue.Queue, que ya es segura entre hilos):

    hilo receptor -> cola.put(mensaje)
    hilo de la GUI -> revisa la cola cada 50 ms con after() y dibuja

Uso:
    python cliente.py
"""

import os
import queue
import socket
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, scrolledtext, ttk

import protocolo as p

CARPETA_DESCARGAS = "descargas"


class ClienteChat:

    def __init__(self, raiz):
        self.raiz = raiz
        self.raiz.title("Chat - desconectado")
        self.raiz.geometry("760x520")
        self.raiz.minsize(620, 420)

        self.sock = None
        self.usuario = None
        self.conectado = False
        self.lock_envio = threading.Lock()   # protege el socket al escribir
        self.cola = queue.Queue()            # puente hilo receptor -> GUI

        self.construir_interfaz()
        self.raiz.protocol("WM_DELETE_WINDOW", self.al_cerrar)
        self.raiz.after(50, self.revisar_cola)

    # ---------------------------------------------------------------
    # Interfaz
    # ---------------------------------------------------------------

    def construir_interfaz(self):
        # --- Barra de conexion -------------------------------------
        barra = ttk.Frame(self.raiz, padding=8)
        barra.pack(fill=tk.X)

        ttk.Label(barra, text="Servidor:").pack(side=tk.LEFT)
        self.campo_host = ttk.Entry(barra, width=14)
        self.campo_host.insert(0, p.HOST_POR_DEFECTO)
        self.campo_host.pack(side=tk.LEFT, padx=(4, 10))

        ttk.Label(barra, text="Puerto:").pack(side=tk.LEFT)
        self.campo_puerto = ttk.Entry(barra, width=7)
        self.campo_puerto.insert(0, str(p.PUERTO_POR_DEFECTO))
        self.campo_puerto.pack(side=tk.LEFT, padx=(4, 10))

        ttk.Label(barra, text="Usuario:").pack(side=tk.LEFT)
        self.campo_usuario = ttk.Entry(barra, width=14)
        self.campo_usuario.pack(side=tk.LEFT, padx=(4, 10))

        self.boton_conectar = ttk.Button(barra, text="Conectar", command=self.conectar)
        self.boton_conectar.pack(side=tk.LEFT)

        # --- Zona central: chat + lista de usuarios ----------------
        centro = ttk.Frame(self.raiz, padding=(8, 0))
        centro.pack(fill=tk.BOTH, expand=True)

        self.chat = scrolledtext.ScrolledText(centro, state=tk.DISABLED, wrap=tk.WORD)
        self.chat.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Colores por tipo de mensaje
        self.chat.tag_config("sistema", foreground="#777777")
        self.chat.tag_config("privado", foreground="#8800aa")
        self.chat.tag_config("error", foreground="#cc0000")
        self.chat.tag_config("archivo", foreground="#006622")
        self.chat.tag_config("yo", foreground="#0055aa")

        panel = ttk.Frame(centro, padding=(8, 0, 0, 0))
        panel.pack(side=tk.RIGHT, fill=tk.Y)
        ttk.Label(panel, text="Conectados").pack(anchor=tk.W)
        self.lista_usuarios = tk.Listbox(panel, width=18)
        self.lista_usuarios.pack(fill=tk.Y, expand=True)

        # --- Barra inferior: destinatario, texto, botones ----------
        abajo = ttk.Frame(self.raiz, padding=8)
        abajo.pack(fill=tk.X)

        ttk.Label(abajo, text="Para:").pack(side=tk.LEFT)
        self.destino = ttk.Combobox(abajo, width=14, state="readonly", values=["Todos"])
        self.destino.set("Todos")
        self.destino.pack(side=tk.LEFT, padx=(4, 8))

        self.campo_mensaje = ttk.Entry(abajo)
        self.campo_mensaje.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.campo_mensaje.bind("<Return>", lambda evento: self.enviar_mensaje())

        self.boton_enviar = ttk.Button(abajo, text="Enviar", command=self.enviar_mensaje)
        self.boton_enviar.pack(side=tk.LEFT, padx=4)
        self.boton_archivo = ttk.Button(abajo, text="Archivo...", command=self.enviar_archivo)
        self.boton_archivo.pack(side=tk.LEFT)

        self.habilitar_chat(False)

    def habilitar_chat(self, activo):
        estado = tk.NORMAL if activo else tk.DISABLED
        for widget in (self.campo_mensaje, self.boton_enviar, self.boton_archivo):
            widget.config(state=estado)
        self.destino.config(state="readonly" if activo else tk.DISABLED)
        for widget in (self.campo_host, self.campo_puerto, self.campo_usuario):
            widget.config(state=tk.DISABLED if activo else tk.NORMAL)
        self.boton_conectar.config(text="Desconectar" if activo else "Conectar")

    def escribir(self, texto, etiqueta=None):
        """Agrega una linea al area de chat. Solo se llama desde el hilo de la GUI."""
        self.chat.config(state=tk.NORMAL)
        self.chat.insert(tk.END, f"[{datetime.now():%H:%M}] {texto}\n",
                         etiqueta if etiqueta else "")
        self.chat.see(tk.END)                 # auto-scroll
        self.chat.config(state=tk.DISABLED)

    # ---------------------------------------------------------------
    # Conexion
    # ---------------------------------------------------------------

    def conectar(self):
        if self.conectado:
            self.desconectar()
            return

        host = self.campo_host.get().strip()
        usuario = self.campo_usuario.get().strip()
        if not usuario:
            messagebox.showwarning("Falta el usuario", "Escribe un nombre de usuario.")
            return
        try:
            puerto = int(self.campo_puerto.get().strip())
        except ValueError:
            messagebox.showwarning("Puerto invalido", "El puerto debe ser un numero.")
            return

        try:
            self.sock = socket.create_connection((host, puerto), timeout=10)
            self.sock.settimeout(None)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            p.enviar(self.sock, {"tipo": p.LOGIN, "usuario": usuario}, lock=self.lock_envio)
        except OSError as e:
            messagebox.showerror("No se pudo conectar", f"{e}")
            self.sock = None
            return

        self.usuario = usuario
        self.conectado = True
        self.habilitar_chat(True)
        self.raiz.title(f"Chat - {usuario}@{host}:{puerto}")
        self.escribir(f"Conectado a {host}:{puerto} como {usuario}.", "sistema")
        self.campo_mensaje.focus()

        # Hilo receptor: su unico trabajo es leer del socket y encolar.
        threading.Thread(target=self.ciclo_receptor, daemon=True).start()

    def desconectar(self, aviso=None):
        if not self.conectado:
            return
        self.conectado = False
        try:
            p.enviar(self.sock, {"tipo": p.SALIR}, lock=self.lock_envio)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None
        self.habilitar_chat(False)
        self.lista_usuarios.delete(0, tk.END)
        self.destino.config(values=["Todos"])
        self.destino.set("Todos")
        self.raiz.title("Chat - desconectado")
        self.escribir(aviso or "Desconectado.", "sistema")

    def al_cerrar(self):
        self.desconectar()
        self.raiz.destroy()

    # ---------------------------------------------------------------
    # Hilo receptor
    # ---------------------------------------------------------------

    def ciclo_receptor(self):
        """Corre en su propio hilo. NO toca widgets: solo pone en la cola."""
        try:
            while self.conectado:
                mensaje, binario = p.recibir(self.sock)
                self.cola.put((mensaje, binario))
        except (p.ConexionCerrada, p.ErrorProtocolo, OSError):
            if self.conectado:
                self.cola.put(({"tipo": "__caida__"}, b""))

    def revisar_cola(self):
        """Corre en el hilo de la GUI cada 50 ms y dibuja lo que llego."""
        try:
            while True:
                mensaje, binario = self.cola.get_nowait()
                self.mostrar(mensaje, binario)
        except queue.Empty:
            pass
        self.raiz.after(50, self.revisar_cola)

    def mostrar(self, mensaje, binario):
        tipo = mensaje["tipo"]

        if tipo == p.CHAT:
            de = mensaje["de"]
            if mensaje.get("privado"):
                if "para" in mensaje:      # eco de algo que yo mande
                    self.escribir(f"(privado para {mensaje['para']}) {mensaje['texto']}", "privado")
                else:
                    self.escribir(f"(privado de {de}) {mensaje['texto']}", "privado")
            else:
                etiqueta = "yo" if de == self.usuario else None
                self.escribir(f"{de}: {mensaje['texto']}", etiqueta)

        elif tipo == p.ARCHIVO_ENTRANTE:
            self.guardar_archivo(mensaje, binario)

        elif tipo == p.USUARIOS:
            self.actualizar_usuarios(mensaje["lista"])

        elif tipo == p.SISTEMA:
            self.escribir(f"* {mensaje['texto']}", "sistema")

        elif tipo == p.LOGIN_OK:
            self.escribir("* Sesion iniciada correctamente.", "sistema")

        elif tipo == p.ERROR:
            self.escribir(f"! {mensaje['texto']}", "error")

        elif tipo == "__caida__":
            self.desconectar("Se perdio la conexion con el servidor.")

    def actualizar_usuarios(self, lista):
        self.lista_usuarios.delete(0, tk.END)
        for nombre in lista:
            self.lista_usuarios.insert(tk.END, nombre + (" (yo)" if nombre == self.usuario else ""))

        anterior = self.destino.get()
        opciones = ["Todos"] + [n for n in lista if n != self.usuario]
        self.destino.config(values=opciones)
        self.destino.set(anterior if anterior in opciones else "Todos")

    def guardar_archivo(self, mensaje, binario):
        os.makedirs(CARPETA_DESCARGAS, exist_ok=True)
        ruta = os.path.join(CARPETA_DESCARGAS, mensaje["nombre"])

        # Si ya existe un archivo con ese nombre se le agrega un numero
        # en vez de sobrescribirlo.
        base, extension = os.path.splitext(ruta)
        contador = 1
        while os.path.exists(ruta):
            ruta = f"{base}({contador}){extension}"
            contador += 1

        try:
            with open(ruta, "wb") as archivo:
                archivo.write(binario)
        except OSError as e:
            self.escribir(f"! No se pudo guardar el archivo: {e}", "error")
            return

        kb = len(binario) / 1024
        marca = "privado " if mensaje.get("privado") else ""
        self.escribir(f"[archivo {marca}de {mensaje['de']}] {os.path.basename(ruta)} "
                      f"({kb:.1f} KB) guardado en '{ruta}'", "archivo")

    # ---------------------------------------------------------------
    # Envio
    # ---------------------------------------------------------------

    def destinatario(self):
        elegido = self.destino.get()
        return p.DIFUSION if elegido == "Todos" else elegido

    def enviar_mensaje(self):
        texto = self.campo_mensaje.get().strip()
        if not texto or not self.conectado:
            return
        try:
            p.enviar(self.sock,
                     {"tipo": p.MENSAJE, "texto": texto, "destino": self.destinatario()},
                     lock=self.lock_envio)
            self.campo_mensaje.delete(0, tk.END)
        except OSError as e:
            self.desconectar(f"Error al enviar: {e}")

    def enviar_archivo(self):
        if not self.conectado:
            return
        ruta = filedialog.askopenfilename(title="Selecciona un archivo")
        if not ruta:
            return

        tamanio = os.path.getsize(ruta)
        if tamanio > p.MAX_ARCHIVO:
            messagebox.showwarning(
                "Archivo muy grande",
                f"El limite es {p.MAX_ARCHIVO // (1024*1024)} MB y este pesa "
                f"{tamanio / (1024*1024):.1f} MB.")
            return

        destino = self.destinatario()
        self.escribir(f"Enviando '{os.path.basename(ruta)}' ({tamanio/1024:.1f} KB)...", "archivo")

        # Se manda en un hilo aparte: leer el archivo y hacer sendall() de
        # varios MB tarda, y en el hilo de la GUI dejaria la ventana congelada.
        threading.Thread(target=self._subir_archivo, args=(ruta, destino), daemon=True).start()

    def _subir_archivo(self, ruta, destino):
        try:
            with open(ruta, "rb") as archivo:
                contenido = archivo.read()
            p.enviar(self.sock,
                     {"tipo": p.ARCHIVO, "nombre": os.path.basename(ruta), "destino": destino},
                     contenido, lock=self.lock_envio)
        except OSError as e:
            # No se toca la GUI desde este hilo: se avisa por la cola.
            self.cola.put(({"tipo": p.ERROR, "texto": f"No se pudo enviar el archivo: {e}"}, b""))


if __name__ == "__main__":
    raiz = tk.Tk()
    ClienteChat(raiz)
    raiz.mainloop()
